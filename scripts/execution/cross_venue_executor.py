#!/usr/bin/env python3
"""Cross-exchange delta-neutral executor — atomic execution of futures leg and spot leg at different venues.

Semantics aligned with delta_neutral_executor (single-venue):
  Open forward:  spot_venue spot buy → futures_venue open short; on futures leg failure, sell back spot to rollback
  Open reverse:  spot_venue margin borrow-sell → futures_venue open long; on futures leg failure, buy back and repay to rollback
  Close forward: futures_venue close short → spot_venue sell spot; on spot leg failure, reopen short to rollback
  Close reverse: futures_venue close long → spot_venue buy back and repay; on spot leg failure, reopen long to rollback

Cross-exchange execution has no inter-venue atomicity: on rollback failure the position is naked (state=naked),
which triggers an alert and requires manual handling.
Position records are stored in scripts/data/cross-venue/positions.json for closing and reconciliation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.file_lock import lock_exclusive, unlock  # noqa: E402
from core.notify import send_notification  # noqa: E402
from execution.funding_recheck import cfg_lookup, recheck_carry_funding  # noqa: E402
from venues import get_venue  # noqa: E402
from venues.base import make_pair  # noqa: E402

Direction = Literal["forward", "reverse"]

POSITIONS_PATH = SCRIPTS_DIR / "data" / "cross-venue" / "positions.json"
# Reject opens when cross-venue spot price spread exceeds this threshold (indicates bad data / un-arbitrageable)
MAX_VENUE_PRICE_SPREAD_PCT = 1.0
MARGIN_BUFFER = 1.05


@dataclass
class CrossVenueResult:
    ok: bool
    state: str  # simulated | filled | rolled_back | naked | aborted
    position_id: str = ""
    executed: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "state": self.state,
            "position_id": self.position_id,
            "executed": self.executed,
            "logs": self.logs,
        }


# ── Position records ──────────────────────────────────────────────────────────


def quarantine_corrupt_positions(path: Path) -> Path | None:
    """Rename a corrupt positions ledger aside so it is preserved + visible.

    A silently-returned [] would orphan live legs (open positions untracked,
    watcher/runner never close them). Quarantine keeps the data for recovery
    and makes the incident loud. Best-effort: a failed rename must not crash.
    Returns the backup path, or None when the rename failed.
    """
    backup: Path | None = None
    try:
        backup = path.with_name(
            f"{path.stem}.corrupt-{int(time.time() * 1000)}{path.suffix}"
        )
        os.rename(str(path), str(backup))
    except Exception as e:
        print(
            f"[POSITIONS] corrupt ledger quarantine FAILED for {path} ({e}) — "
            f"live positions may be untracked — investigate!",
            file=sys.stderr,
            flush=True,
        )
        return None
    print(
        f"[POSITIONS] corrupt ledger quarantined: {path} -> {backup} "
        f"(live positions may be untracked — investigate!)",
        file=sys.stderr,
        flush=True,
    )
    return backup


def load_positions(path: Path = POSITIONS_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # Corrupt (e.g. truncated mid-write) ledger: quarantine instead of
        # silently returning [] so live positions are not orphaned invisibly.
        quarantine_corrupt_positions(path)
        return []
    return data if isinstance(data, list) else []


def _save_positions(
    positions: list[dict[str, Any]], path: Path = POSITIONS_PATH
) -> None:
    """Atomic write: write to temp file then rename (crash-safe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(positions, ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".positions-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _with_position_lock(path: Path):
    """Acquire exclusive lock on position file for safe concurrent access."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    lock_fd = open(lock_path, "w")
    try:
        lock_exclusive(lock_fd)
    except BaseException:
        lock_fd.close()
        raise
    return lock_fd


def _record_position(record: dict[str, Any], path: Path = POSITIONS_PATH) -> None:
    lock_fd = _with_position_lock(path)
    try:
        positions = load_positions(path)
        positions.append(record)
        _save_positions(positions, path)
    finally:
        unlock(lock_fd)
        lock_fd.close()


def _mark_closed(
    position_id: str, close_info: dict[str, Any], path: Path = POSITIONS_PATH
) -> bool:
    lock_fd = _with_position_lock(path)
    try:
        positions = load_positions(path)
        for p in positions:
            if p.get("id") == position_id and p.get("status") == "open":
                p["status"] = "closed"
                p["closed_at"] = int(time.time() * 1000)
                p["close_info"] = close_info
                _save_positions(positions, path)
                return True
        return False
    finally:
        unlock(lock_fd)
        lock_fd.close()


# ── Market / rules snapshot ────────────────────────────────────────────────────


def _leg_market(venue: Any, base: str, quote: str, *, futures: bool) -> dict[str, Any]:
    """Single-leg minimal market snapshot: price + precision/min-quantity rules."""
    default_rules = {
        "quantity_precision": 6,
        "quote_precision": 2,
        "min_trade_usdt": 5.0,
        "min_trade_base": 0.0,
    }
    if futures:
        pair = make_pair(base, quote)
        rules = (
            venue.fetch_futures_symbol_rules(pair)
            or venue.fetch_symbol_rules(pair)
            or default_rules
        )
        price = 0.0
        try:
            if hasattr(venue, "get_futures_ticker"):
                price = float(venue.get_futures_ticker(pair) or 0.0)
            else:
                price = float(venue.get_ticker(pair) or 0.0)
        except Exception:
            price = 0.0
        # Fallback: spot ticker if futures ticker returned 0
        if price <= 0:
            try:
                price = float(venue.get_ticker(pair) or 0.0)
            except Exception:
                pass
    else:
        # Spot leg: prefer fetch_asset_market which handles OKX's ETH-USDT format differences;
        # fall back to generic pair query for test fakes / older venue adapters without that method.
        if hasattr(venue, "fetch_asset_market"):
            am = venue.fetch_asset_market(base, quote)
            pair = str(am.get("pair") or make_pair(base, quote))
            rules = (
                am.get("symbol_rules")
                or venue.fetch_symbol_rules(pair)
                or default_rules
            )
            price = float(am.get("price") or 0.0)
        else:
            pair = make_pair(base, quote)
            rules = venue.fetch_symbol_rules(pair) or default_rules
            try:
                price = float(venue.get_ticker(pair) or 0.0)
            except Exception:
                price = 0.0
    return {
        "pair": pair,
        "price": price,
        "quantity_precision": int(rules.get("quantity_precision", 6)),
        "quote_precision": int(rules.get("quote_precision", 2)),
        "min_trade_usdt": float(rules.get("min_trade_usdt", 0) or 0),
        "min_trade_base": float(rules.get("min_trade_base", 0) or 0),
    }


def _floor_qty(qty: float, precision: int) -> float:
    scale = 10**precision
    return int(qty * scale) / scale


def _filled(results: list[dict[str, Any]]) -> bool:
    return bool(results) and results[0].get("status") in ("filled", "simulated")


def _exec_qty(results: list[dict[str, Any]], fallback: float) -> float:
    if results and results[0].get("exec_qty"):
        return float(results[0]["exec_qty"])
    return fallback


def _rollback_spot_leg(
    sv: Any,
    spot_market: dict[str, Any],
    base: str,
    direction: Direction,
    exec_qty: float,
    spot_px: float,
    *,
    executed: list[dict[str, Any]],
    logs: list[str],
    config: dict[str, Any] | None,
    reason: str,
) -> CrossVenueResult:
    """Undo an already-filled spot leg (sell back / buy back + repay).

    Used when the futures leg cannot be safely submitted (leg failure or
    fail-closed margin gate). Returns rolled_back on success, naked otherwise.
    """
    rollback: dict[str, Any] = {
        "symbol": base,
        "type": "sell" if direction == "forward" else "buy",
        "amount_base": exec_qty,
        "amount_usdt": round(exec_qty * spot_px, 4),
        "reason": reason,
    }
    if direction == "reverse":
        rollback["account"] = "margin"
        rollback["side_effect"] = "auto_repay"
    send_notification(
        "Cross-Venue Leg Failure",
        f"rolling back spot leg {rollback['type']} {exec_qty} {base}: {reason}",
        config,
    )
    res_rb = sv.execute_trades([rollback], spot_market, dry_run=False)
    executed.extend(res_rb)
    if _filled(res_rb):
        logs.append("Rollback succeeded, no naked position")
        return CrossVenueResult(False, "rolled_back", "", executed, logs)

    logs.append("Rollback failed! Spot leg naked, requires manual handling")
    send_notification(
        "NAKED POSITION",
        f"Cross-venue rollback failed: spot venue holds {exec_qty} {base} unhedged",
        config,
    )
    return CrossVenueResult(False, "naked", "", executed, logs)


# ── Open position ──────────────────────────────────────────────────────────────


def open_cross_venue_position(
    base: str,
    direction: Direction,
    futures_venue_id: str,
    spot_venue_id: str,
    trade_usd: float,
    *,
    dry_run: bool = True,
    quote: str = "USDT",
    config: dict[str, Any] | None = None,
    futures_venue: Any = None,
    spot_venue: Any = None,
    positions_path: Path = POSITIONS_PATH,
) -> CrossVenueResult:
    """Cross-venue open. forward: spot buy + perp short; reverse: margin borrow-sell + perp long."""
    logs: list[str] = []
    executed: list[dict[str, Any]] = []
    fv = futures_venue or get_venue({"venue": {"type": futures_venue_id}})
    sv = spot_venue or get_venue({"venue": {"type": spot_venue_id}})

    spot_mkt = _leg_market(sv, base, quote, futures=False)
    fut_mkt = _leg_market(fv, base, quote, futures=True)
    spot_px = spot_mkt["price"]
    fut_px = fut_mkt["price"] or spot_px
    if spot_px <= 0:
        return CrossVenueResult(
            False, "aborted", logs=[f"{spot_venue_id} spot price unavailable"]
        )

    # Spread gate: if cross-venue price deviation is too large, data is likely bad or un-arbitrageable
    if fut_px > 0:
        spread_pct = abs(fut_px - spot_px) / spot_px * 100.0
        if spread_pct > MAX_VENUE_PRICE_SPREAD_PCT:
            return CrossVenueResult(
                False,
                "aborted",
                logs=[
                    f"Cross-venue spread {spread_pct:.2f}% > {MAX_VENUE_PRICE_SPREAD_PCT}%, rejecting open"
                ],
            )

    if direction == "reverse" and not sv.supports_reverse_arbitrage():
        return CrossVenueResult(
            False,
            "aborted",
            logs=[
                f"{spot_venue_id} does not support margin borrow-sell, cannot execute reverse spot leg"
            ],
        )

    # Unify quantity across both legs: use the coarser precision of the two to ensure both can place orders
    qty_prec = min(spot_mkt["quantity_precision"], fut_mkt["quantity_precision"])
    base_amount = _floor_qty(trade_usd / spot_px, qty_prec)
    if base_amount <= 0:
        return CrossVenueResult(
            False, "aborted", logs=["Quantity floored to 0, trade_usd too small"]
        )
    for leg_name, mkt in (("spot", spot_mkt), ("futures", fut_mkt)):
        if trade_usd < mkt["min_trade_usdt"] or base_amount < mkt["min_trade_base"]:
            return CrossVenueResult(
                False,
                "aborted",
                logs=[
                    f"{leg_name} leg below minimum: trade_usd={trade_usd} "
                    f"(min {mkt['min_trade_usdt']}), base={base_amount} (min {mkt['min_trade_base']})"
                ],
            )

    spot_trade: dict[str, Any] = {
        "symbol": base,
        "type": "buy" if direction == "forward" else "sell",
        "amount_base": base_amount,
        "amount_usdt": round(base_amount * spot_px, 4),
        "reason": f"Cross-venue {direction} open: fut@{futures_venue_id} spot@{spot_venue_id}",
    }
    if direction == "reverse":
        spot_trade["account"] = "margin"
        spot_trade["side_effect"] = "auto_borrow"
    fut_trade: dict[str, Any] = {
        "symbol": base,
        "type": "open_short" if direction == "forward" else "open_long",
        "amount_base": base_amount,
        "amount_usdt": round(base_amount * fut_px, 4),
        "quantity_precision": fut_mkt["quantity_precision"],
        "reason": spot_trade["reason"],
    }
    spot_market = {base: spot_mkt}
    fut_market = {base: fut_mkt}
    position_id = f"xv-{int(time.time())}-{uuid.uuid4().hex[:6]}"

    if dry_run:
        executed.extend(sv.execute_trades([spot_trade], spot_market, dry_run=True))
        executed.extend(fv.execute_trades([fut_trade], fut_market, dry_run=True))
        logs.append(
            f"[DRY-RUN] {direction} {base} qty={base_amount} "
            f"spot@{spot_venue_id}({spot_px:.6g}) fut@{futures_venue_id}({fut_px:.6g})"
        )
        _record_position(
            {
                "id": position_id,
                "status": "open",
                "dry_run": True,
                "base": base,
                "direction": direction,
                "futures_venue": futures_venue_id,
                "spot_venue": spot_venue_id,
                "qty": base_amount,
                "spot_price": spot_px,
                "futures_price": fut_px,
                "trade_usd": trade_usd,
                "opened_at": int(time.time() * 1000),
            },
            positions_path,
        )
        return CrossVenueResult(True, "simulated", position_id, executed, logs)

    # ── Pre-submit funding re-check (carry semantics) ────────────────────────
    # Only the futures leg earns/pays funding (the spot leg has none); the
    # scanner decision may be based on funding data 30s+ stale by fill time.
    # Re-fetch the futures venue's current rate and verify it still clears the
    # floor and has not flipped sign against the position direction
    # (forward: rate >= +floor; reverse: rate <= -floor).
    if config and bool(cfg_lookup(config, "fundingRecheck", True)):
        fr = recheck_carry_funding(
            futures_venue_id,
            base,
            direction,
            quote,
            min_rate_pct=float(
                cfg_lookup(config, "fundingRecheckMinSpreadPct", 0.02)
            ),
            fail_open=bool(cfg_lookup(config, "fundingRecheckFailOpen", dry_run)),
        )
        logs.append(f"funding re-check: {fr.get('reason')}")
        if not fr.get("ok"):
            return CrossVenueResult(False, "aborted", "", executed, logs)

    # ── live: spot leg first (uses own/borrowed assets, lower risk) ────────────────
    if direction == "reverse":
        # Margin borrow-sell requires collateral to be ready (Bitget crossed margin is separate account; UTA/Binance each handles internally)
        collateral = trade_usd * 0.6
        try:
            if sv.transfer_asset(quote, collateral, "spot", "margin"):
                logs.append(
                    f"{spot_venue_id}: spot→margin collateral transfer {collateral:.2f} {quote}"
                )
        except Exception as e:
            logs.append(f"{spot_venue_id}: collateral transfer skipped ({e})")

    res_spot = sv.execute_trades([spot_trade], spot_market, dry_run=False)
    executed.extend(res_spot)
    if not _filled(res_spot):
        logs.append(
            f"Spot leg failed: {res_spot[0].get('error') if res_spot else 'no result'}"
        )
        return CrossVenueResult(False, "aborted", "", executed, logs)
    exec_qty = _exec_qty(res_spot, base_amount)
    logs.append(
        f"Spot leg filled {spot_venue_id} {spot_trade['type']} {exec_qty} {base}"
    )

    # Futures margin readiness (intra-venue spot→futures transfer, best effort).
    # Fail-closed by default: a balance-API failure aborts the open (rolling
    # back the already-filled spot leg) instead of submitting the futures leg
    # with unverified margin. Set marginCheckFailOpen=true to restore the old
    # best-effort skip on API failures.
    margin_fail_open = bool(cfg_lookup(config, "marginCheckFailOpen", False))
    margin_usd = exec_qty * fut_px * MARGIN_BUFFER
    margin_ok = True
    try:
        fut_bal = fv.fetch_usdt_account_balances()
        if float(fut_bal.get("futures", 0)) < margin_usd:
            if fv.transfer_asset(quote, margin_usd, "spot", "futures"):
                logs.append(
                    f"{futures_venue_id}: spot→futures transfer {margin_usd:.2f} {quote}"
                )
    except Exception as e:
        if margin_fail_open:
            logs.append(f"{futures_venue_id}: margin check/transfer skipped ({e})")
        else:
            logs.append(
                f"{futures_venue_id}: margin query failed ({e}), aborting "
                f"(fail-closed; set marginCheckFailOpen=true to override)"
            )
            margin_ok = False

    if not margin_ok:
        return _rollback_spot_leg(
            sv,
            spot_market,
            base,
            direction,
            exec_qty,
            spot_px,
            executed=executed,
            logs=logs,
            config=config,
            reason="ROLLBACK: futures margin readiness unverified (fail-closed)",
        )

    try:
        fv.initialize_futures_symbol(fut_mkt["pair"])
    except Exception:
        pass
    fut_trade["amount_base"] = _floor_qty(exec_qty, fut_mkt["quantity_precision"])
    res_fut = fv.execute_trades([fut_trade], fut_market, dry_run=False)
    executed.extend(res_fut)

    if _filled(res_fut):
        logs.append(
            f"Futures leg filled {futures_venue_id} {fut_trade['type']} {fut_trade['amount_base']} {base}"
        )
        _record_position(
            {
                "id": position_id,
                "status": "open",
                "dry_run": False,
                "base": base,
                "direction": direction,
                "futures_venue": futures_venue_id,
                "spot_venue": spot_venue_id,
                "qty": exec_qty,
                "futures_qty": fut_trade["amount_base"],
                "spot_price": res_spot[0].get("exec_price", spot_px),
                "futures_price": res_fut[0].get("exec_price", fut_px),
                "trade_usd": trade_usd,
                "opened_at": int(time.time() * 1000),
            },
            positions_path,
        )
        return CrossVenueResult(True, "filled", position_id, executed, logs)

    # ── Futures leg failed → roll back spot leg ─────────────────────────────────
    logs.append(
        f"Futures leg failed: {res_fut[0].get('error') if res_fut else 'no result'}, rolling back spot leg"
    )
    return _rollback_spot_leg(
        sv,
        spot_market,
        base,
        direction,
        exec_qty,
        spot_px,
        executed=executed,
        logs=logs,
        config=config,
        reason="ROLLBACK: cross-venue futures leg failed",
    )


# ── Close position ────────────────────────────────────────────────────────────


def close_cross_venue_position(
    position_id: str,
    *,
    dry_run: bool | None = None,
    quote: str = "USDT",
    config: dict[str, Any] | None = None,
    futures_venue: Any = None,
    spot_venue: Any = None,
    positions_path: Path = POSITIONS_PATH,
) -> CrossVenueResult:
    """Close position per position record. Futures leg closed first (eliminates funding exposure), then spot leg."""
    pos = next(
        (
            p
            for p in load_positions(positions_path)
            if p.get("id") == position_id and p.get("status") == "open"
        ),
        None,
    )
    if pos is None:
        return CrossVenueResult(
            False, "aborted", logs=[f"Open position not found: {position_id}"]
        )

    base = pos["base"]
    direction: Direction = pos["direction"]
    fv_id, sv_id = pos["futures_venue"], pos["spot_venue"]
    qty = float(pos.get("futures_qty") or pos["qty"])
    spot_qty = float(pos["qty"])
    if dry_run is None:
        dry_run = bool(pos.get("dry_run", True))

    logs: list[str] = []
    executed: list[dict[str, Any]] = []
    fv = futures_venue or get_venue({"venue": {"type": fv_id}})
    sv = spot_venue or get_venue({"venue": {"type": sv_id}})
    spot_mkt = _leg_market(sv, base, quote, futures=False)
    fut_mkt = _leg_market(fv, base, quote, futures=True)
    spot_px = spot_mkt["price"]

    fut_trade: dict[str, Any] = {
        "symbol": base,
        "type": "close_short" if direction == "forward" else "close_long",
        "amount_base": qty,
        "amount_usdt": round(qty * (fut_mkt["price"] or spot_px), 4),
        "quantity_precision": fut_mkt["quantity_precision"],
        "reason": f"Cross-venue {direction} close {position_id}",
    }
    spot_trade: dict[str, Any] = {
        "symbol": base,
        "type": "sell" if direction == "forward" else "buy",
        "amount_base": spot_qty,
        "amount_usdt": round(spot_qty * spot_px, 4),
        "reason": fut_trade["reason"],
    }
    if direction == "reverse":
        spot_trade["account"] = "margin"
        spot_trade["side_effect"] = "auto_repay"
    fut_market = {base: fut_mkt}
    spot_market = {base: spot_mkt}

    if dry_run:
        executed.extend(fv.execute_trades([fut_trade], fut_market, dry_run=True))
        executed.extend(sv.execute_trades([spot_trade], spot_market, dry_run=True))
        _mark_closed(position_id, {"dry_run": True}, positions_path)
        logs.append(f"[DRY-RUN] close {direction} {base} qty={qty}")
        return CrossVenueResult(True, "simulated", position_id, executed, logs)

    # Close futures leg first
    res_fut = fv.execute_trades([fut_trade], fut_market, dry_run=False)
    executed.extend(res_fut)
    if not _filled(res_fut):
        logs.append(
            f"Futures close failed: {res_fut[0].get('error') if res_fut else 'no result'}"
        )
        return CrossVenueResult(False, "aborted", position_id, executed, logs)
    closed_qty = _exec_qty(res_fut, qty)
    logs.append(f"Futures leg closed {fv_id} {fut_trade['type']} {closed_qty} {base}")

    # Reverse close: align buyback quantity with actual margin debt (including interest)
    if direction == "reverse" and hasattr(sv, "fetch_margin_debt"):
        try:
            debt = float(sv.fetch_margin_debt([base]).get(base, 0.0))
            if spot_qty < debt <= spot_qty * 1.02:
                spot_trade["amount_base"] = debt
                spot_trade["amount_usdt"] = round(debt * spot_px, 4)
        except Exception:
            pass

    res_spot = sv.execute_trades([spot_trade], spot_market, dry_run=False)
    executed.extend(res_spot)
    if _filled(res_spot):
        logs.append(
            f"Spot leg closed {sv_id} {spot_trade['type']} {spot_trade['amount_base']} {base}"
        )
        _mark_closed(
            position_id,
            {
                "futures_price": res_fut[0].get("exec_price"),
                "spot_price": res_spot[0].get("exec_price"),
            },
            positions_path,
        )
        return CrossVenueResult(True, "filled", position_id, executed, logs)

    # Spot leg failed → reopen futures leg to hedge, avoiding one-sided exposure
    logs.append("Spot leg failed, reopening futures leg to hedge")
    reopen = dict(fut_trade)
    reopen["type"] = "open_short" if direction == "forward" else "open_long"
    reopen["amount_base"] = closed_qty
    reopen["reason"] = "ROLLBACK: cross-venue spot close failed"
    send_notification(
        "Cross-Venue Close Rollback",
        f"{sv_id} {spot_trade['type']} {base} failed; re-opening {fv_id} {reopen['type']} {closed_qty}",
        config,
    )
    res_rb = fv.execute_trades([reopen], fut_market, dry_run=False)
    executed.extend(res_rb)
    if _filled(res_rb):
        logs.append("Re-hedged successfully, position remains open")
        return CrossVenueResult(False, "rolled_back", position_id, executed, logs)

    logs.append("Re-hedge failed! Spot leg naked, requires manual handling")
    send_notification(
        "NAKED POSITION",
        f"Cross-venue close rollback failed: {sv_id} {base} exposure unhedged",
        config,
    )
    return CrossVenueResult(False, "naked", position_id, executed, logs)
