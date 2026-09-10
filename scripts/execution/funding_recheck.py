#!/usr/bin/env python3
"""Pre-submit funding edge re-check.

The scanner decision may be based on funding data that is 30s+ stale by the
time orders are submitted. Before opening a pair, re-fetch current funding
rates for both legs and re-verify the spread still clears a floor.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from backtest.funding_providers import get_funding_provider  # noqa: E402
from core.cross_interval_funding import (  # noqa: E402
    infer_last_settle_ts,
    leg_info_from_fields,
    pair_pure_futures_spread,
)

DEFAULT_MIN_SPREAD_PCT = 0.02  # % per effective interval; floor for the re-check

# Module-level TTL cache for (venue, base) funding row lookups. Several pairs
# opening in a burst would otherwise hammer the same venue APIs repeatedly.
_ROW_CACHE_TTL_SEC = 20.0
_ROW_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def clear_row_cache() -> None:
    """Drop the TTL row cache (used by tests and after clock skew/manual refresh)."""
    _ROW_CACHE.clear()


def cfg_lookup(config: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    """Read an executor knob from a runner config, block-aware.

    Both runner templates nest executor knobs in a strategy block
    (``pureFuturesArbitrage`` for the perp-perp runner, ``crossAssetArbitrage``
    for the carry runner), while some callers pass flat top-level configs
    (server-injected or test configs). Checks the blocks first, then the
    config top level; returns ``default`` when the config is None or the key
    is absent everywhere.
    """
    if not isinstance(config, dict):
        return default
    for block in ("pureFuturesArbitrage", "crossAssetArbitrage"):
        sub = config.get(block)
        if isinstance(sub, dict) and key in sub:
            return sub[key]
    return config.get(key, default)


def _base_from_symbol(symbol: str, quote: str = "USDT") -> str:
    """Strip the quote suffix from a funding symbol.

    Mirrors cli.scan_pure_futures_spreads._base_from_symbol (copied here to
    avoid an execution→cli import cycle). All funding providers normalize
    fetch_all rows to BASE+QUOTE symbols (e.g. BTCUSDT, BTC-USDT-SWAP becomes
    BTCUSDT inside the OKX provider), so stripping the suffix is sufficient.
    """
    s = str(symbol).upper()
    q = str(quote).upper()
    if q and s.endswith(q):
        return s[: -len(q)]
    return s


def _provider_for(venue_id: str, providers: dict[str, Any] | None) -> Any:
    if providers is not None:
        p = providers.get(venue_id)
        if p is None:
            p = providers.get(str(venue_id).lower())
        if p is None:
            raise LookupError(f"no provider injected for venue {venue_id!r}")
        return p
    return get_funding_provider(venue_id)


def _fetch_leg_row(
    venue_id: str,
    base: str,
    quote: str = "USDT",
    *,
    providers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch one venue's CURRENT funding row for base, via bulk fetch_all.

    Raises on any fetch failure or when the symbol is absent from the venue's
    rows — callers decide fail-open vs fail-closed. Results are cached for
    _ROW_CACHE_TTL_SEC so a burst of opens does not re-fetch per pair.
    """
    key = (str(venue_id).lower(), str(base).upper(), str(quote).upper())
    now = time.monotonic()
    cached = _ROW_CACHE.get(key)
    if cached is not None and now - cached[0] < _ROW_CACHE_TTL_SEC:
        return cached[1]

    fp = _provider_for(venue_id, providers)
    rows = fp.fetch_all(quote)  # raises on API failure
    try:
        imap = fp.fetch_interval_map(quote) or {}
    except Exception:
        imap = {}

    base_u, quote_u = key[1], key[2]
    row: dict[str, Any] | None = None
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        if not sym.endswith(quote_u):
            continue
        if _base_from_symbol(sym, quote_u) == base_u:
            row = r
            break
    if row is None:
        raise LookupError(f"{venue_id} funding rows contain no {base_u}{quote_u}")

    sym = str(row.get("symbol", "")).upper()
    interval_h = float(imap.get(sym, 8.0) or 8.0)
    next_ts = int(row.get("next_funding_ts", 0) or 0)
    out = {
        "symbol": sym,
        "rate_pct": float(row.get("rate_pct", 0.0) or 0.0),
        "interval_h": interval_h,
        "next_funding_ts": next_ts,
        "last_settle_ts": infer_last_settle_ts(next_ts, interval_h),
        "mark_price": float(row.get("mark_price", 0.0) or 0.0),
        "index_price": float(row.get("index_price", 0.0) or 0.0),
    }
    _ROW_CACHE[key] = (now, out)
    return out


def recheck_funding_edge(
    long_venue_id: str,
    short_venue_id: str,
    base: str,
    quote: str = "USDT",
    *,
    min_spread_pct: float | None = None,
    fail_open: bool = False,
    providers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-verify the perp-perp funding spread right before submitting orders.

    Re-fetches both legs' current funding rows and computes the spread with
    the exact scanner model (core.cross_interval_funding.pair_pure_futures_spread,
    including the cross-interval basis blend; same-interval pairs degrade to
    the plain rate spread).

    Returns dict with keys: ok, spread_pct, long_rate_pct, short_rate_pct,
    long_interval_h, short_interval_h, reason, source.
    - ok = spread_pct >= min_spread_pct (default floor 0.02).
    - On fetch error: ok = fail_open (fail-closed by default).
    """
    floor = DEFAULT_MIN_SPREAD_PCT if min_spread_pct is None else float(min_spread_pct)

    def _degraded(reason: str, ok: bool) -> dict[str, Any]:
        return {
            "ok": ok,
            "spread_pct": 0.0,
            "long_rate_pct": 0.0,
            "short_rate_pct": 0.0,
            "long_interval_h": 0.0,
            "short_interval_h": 0.0,
            "reason": reason,
            "source": "error_fail_open" if ok else "error",
        }

    try:
        long_row = _fetch_leg_row(long_venue_id, base, quote, providers=providers)
        short_row = _fetch_leg_row(short_venue_id, base, quote, providers=providers)
    except Exception as e:
        if fail_open:
            return _degraded(
                f"funding fetch failed ({e}); fail-open, proceeding on stale data",
                True,
            )
        return _degraded(
            f"funding fetch failed ({e}); aborting "
            f"(fail-closed; set fundingRecheckFailOpen=true to override)",
            False,
        )

    now_ms = int(time.time() * 1000)
    long_info = leg_info_from_fields(
        rate_pct=long_row["rate_pct"],
        interval_h=long_row["interval_h"],
        mark_price=long_row["mark_price"],
        index_price=long_row["index_price"],
        next_funding_ts=long_row["next_funding_ts"],
        last_settle_ts=long_row["last_settle_ts"],
        symbol=long_row["symbol"],
    )
    short_info = leg_info_from_fields(
        rate_pct=short_row["rate_pct"],
        interval_h=short_row["interval_h"],
        mark_price=short_row["mark_price"],
        index_price=short_row["index_price"],
        next_funding_ts=short_row["next_funding_ts"],
        last_settle_ts=short_row["last_settle_ts"],
        symbol=short_row["symbol"],
    )
    pair = pair_pure_futures_spread(
        long_rate_pct=long_row["rate_pct"],
        long_interval_h=long_row["interval_h"],
        long_info=long_info,
        long_venue=long_venue_id,
        short_rate_pct=short_row["rate_pct"],
        short_interval_h=short_row["interval_h"],
        short_info=short_info,
        short_venue=short_venue_id,
        now_ms=now_ms,
    )
    spread_pct = float(pair["spread_pct"])
    result: dict[str, Any] = {
        "ok": spread_pct >= floor,
        "spread_pct": spread_pct,
        "long_rate_pct": long_row["rate_pct"],
        "short_rate_pct": short_row["rate_pct"],
        "long_interval_h": long_row["interval_h"],
        "short_interval_h": short_row["interval_h"],
        "reason": "",
        "source": str(pair.get("spread_source", "rate")),
    }
    if result["ok"]:
        result["reason"] = (
            f"spread {spread_pct:.4f}% >= floor {floor:.4f}% "
            f"(long {long_row['rate_pct']:+.4f}%/{long_row['interval_h']:g}h "
            f"@ {long_venue_id}, short {short_row['rate_pct']:+.4f}%/"
            f"{short_row['interval_h']:g}h @ {short_venue_id}, "
            f"source={result['source']})"
        )
    else:
        result["reason"] = (
            f"spread_collapse: spread {spread_pct:.4f}% < floor {floor:.4f}% "
            f"(long {long_row['rate_pct']:+.4f}%/{long_row['interval_h']:g}h "
            f"@ {long_venue_id}, short {short_row['rate_pct']:+.4f}%/"
            f"{short_row['interval_h']:g}h @ {short_venue_id})"
        )
    return result


def recheck_carry_funding(
    futures_venue_id: str,
    base: str,
    direction: str = "forward",
    quote: str = "USDT",
    *,
    min_rate_pct: float | None = None,
    fail_open: bool = False,
    providers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Carry (spot + perp) pre-submit funding re-check.

    Only the futures leg earns/pays funding (the spot leg has none), so the
    relevant edge is the futures venue's current rate alone — mirroring how
    cli.scan_funding_arbitrage computes carry net_edge (rate − two-leg fees,
    with no spread between two funding legs). Keep it simple and safe: verify
    the rate still clears the floor AND has not flipped sign against the
    position direction:
      forward (short perp collecting positive funding): rate >= +floor
      reverse (long perp collecting on negative funding): rate <= -floor

    Returns dict with keys: ok, rate_pct, interval_h, reason, source.
    """
    floor = DEFAULT_MIN_SPREAD_PCT if min_rate_pct is None else float(min_rate_pct)
    direction = str(direction or "forward").lower()

    try:
        row = _fetch_leg_row(futures_venue_id, base, quote, providers=providers)
    except Exception as e:
        reason = (
            f"funding fetch failed ({e}); fail-open, proceeding on stale data"
            if fail_open
            else (
                f"funding fetch failed ({e}); aborting "
                f"(fail-closed; set fundingRecheckFailOpen=true to override)"
            )
        )
        return {
            "ok": bool(fail_open),
            "rate_pct": 0.0,
            "interval_h": 0.0,
            "reason": reason,
            "source": "error_fail_open" if fail_open else "error",
        }

    rate = float(row["rate_pct"])
    interval_h = float(row["interval_h"])
    if direction == "reverse":
        ok = rate <= -floor
        if ok:
            reason = (
                f"carry rate {rate:+.4f}%/{interval_h:g}h @ {futures_venue_id} "
                f"<= -{floor:.4f}% floor (direction=reverse)"
            )
        else:
            reason = (
                f"spread_collapse: rate {rate:+.4f}%/{interval_h:g}h @ "
                f"{futures_venue_id} above -{floor:.4f}% floor (direction=reverse, "
                f"sign flipped or collapsed)"
            )
    else:
        ok = rate >= floor
        if ok:
            reason = (
                f"carry rate {rate:+.4f}%/{interval_h:g}h @ {futures_venue_id} "
                f">= +{floor:.4f}% floor (direction=forward)"
            )
        else:
            reason = (
                f"spread_collapse: rate {rate:+.4f}%/{interval_h:g}h @ "
                f"{futures_venue_id} below +{floor:.4f}% floor (direction=forward)"
            )
    return {
        "ok": ok,
        "rate_pct": rate,
        "interval_h": interval_h,
        "reason": reason,
        "source": "rate",
    }
