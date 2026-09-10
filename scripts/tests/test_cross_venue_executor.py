#!/usr/bin/env python3
"""Hermetic tests for cross_venue_executor (spot+futures legs, no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import execution.cross_venue_executor as xvm  # noqa: E402
from execution.cross_venue_executor import (  # noqa: E402
    close_cross_venue_position,
    load_positions,
    open_cross_venue_position,
)


class FakeSpotVenue:
    """Spot venue double implementing the surface cross_venue_executor uses."""

    def __init__(
        self,
        venue_id: str,
        price: float = 100.0,
        fail_types: set[str] | None = None,
        supports_reverse: bool = True,
    ):
        self.venue_id = venue_id
        self.price = price
        self.fail_types = fail_types or set()
        self.supports_reverse = supports_reverse
        self.trades: list[dict] = []
        self.transfers: list[tuple] = []

    def fetch_asset_market(self, base: str, quote: str):
        return {
            "pair": f"{base}{quote}",
            "price": self.price,
            "symbol_rules": {
                "symbol": f"{base}{quote}",
                "quantity_precision": 4,
                "quote_precision": 2,
                "min_trade_usdt": 5.0,
                "min_trade_base": 0.0,
            },
        }

    def fetch_symbol_rules(self, pair: str, cache_sec: int = 3600):
        return self.fetch_asset_market(pair[:-4], "USDT")["symbol_rules"]

    def get_ticker(self, pair: str):
        return self.price

    def supports_reverse_arbitrage(self):
        return self.supports_reverse

    def transfer_asset(
        self, asset: str, amount: float, from_account: str, to_account: str
    ):
        self.transfers.append((asset, amount, from_account, to_account))
        return True

    def fetch_margin_debt(self, bases):
        return {b: 0.0 for b in bases}

    def execute_trades(self, trades, market, dry_run=True):
        out = []
        for t in trades:
            self.trades.append(dict(t, dry_run=dry_run))
            typ = t["type"]
            if typ in self.fail_types and not dry_run:
                out.append(
                    {
                        "symbol": t["symbol"],
                        "type": typ,
                        "status": "failed",
                        "error": f"fail {typ}",
                    }
                )
            else:
                out.append(
                    {
                        "symbol": t["symbol"],
                        "type": typ,
                        "status": "simulated" if dry_run else "filled",
                        "exec_qty": t["amount_base"],
                        "exec_price": self.price,
                    }
                )
        return out


class FakeFuturesVenue:
    """Futures venue double mirroring the pure-futures test fake."""

    def __init__(
        self,
        venue_id: str,
        price: float = 100.0,
        fail_types: set[str] | None = None,
        balances: dict[str, float] | None = None,
    ):
        self.venue_id = venue_id
        self.price = price
        self.fail_types = fail_types or set()
        self.trades: list[dict] = []
        self.initialized: list[str] = []
        self.transfers: list[tuple] = []
        self.balances = (
            balances if balances is not None else {"spot": 100000.0, "futures": 100000.0}
        )

    def fetch_futures_symbol_rules(self, pair: str, cache_sec: int = 3600):
        return {
            "symbol": pair,
            "quantity_precision": 4,
            "quote_precision": 2,
            "min_trade_usdt": 5.0,
            "min_trade_base": 0.0,
        }

    def fetch_symbol_rules(self, pair: str, cache_sec: int = 3600):
        return self.fetch_futures_symbol_rules(pair, cache_sec)

    def get_ticker(self, pair: str):
        return self.price

    def initialize_futures_symbol(self, pair: str):
        self.initialized.append(pair)

    def fetch_usdt_account_balances(self):
        return dict(self.balances)

    def transfer_asset(
        self, asset: str, amount: float, from_account: str, to_account: str
    ):
        self.transfers.append((asset, amount, from_account, to_account))
        if self.balances.get(from_account, 0.0) < amount:
            return False
        self.balances[from_account] -= amount
        self.balances[to_account] = self.balances.get(to_account, 0.0) + amount
        return True

    def execute_trades(self, trades, market, dry_run=True):
        out = []
        for t in trades:
            self.trades.append(dict(t, dry_run=dry_run))
            typ = t["type"]
            if typ in self.fail_types and not dry_run:
                out.append(
                    {
                        "symbol": t["symbol"],
                        "type": typ,
                        "status": "failed",
                        "error": f"fail {typ}",
                    }
                )
            else:
                out.append(
                    {
                        "symbol": t["symbol"],
                        "type": typ,
                        "status": "simulated" if dry_run else "filled",
                        "exec_qty": t["amount_base"],
                        "exec_price": self.price,
                    }
                )
        return out


def _open_live(tmp_path, *, sv=None, fv=None, direction="forward", config=None):
    path = tmp_path / "positions.json"
    sv = sv or FakeSpotVenue("binance")
    fv = fv or FakeFuturesVenue("okx")
    res = open_cross_venue_position(
        "BTC",
        direction,
        "okx",
        "binance",
        500,
        dry_run=False,
        config=config,
        futures_venue=fv,
        spot_venue=sv,
        positions_path=path,
    )
    return res, sv, fv, path


# ── open paths ────────────────────────────────────────────────────────────────


def test_dry_run_forward_open_records_simulated_position(tmp_path):
    path = tmp_path / "positions.json"
    sv, fv = FakeSpotVenue("binance"), FakeFuturesVenue("okx")
    res = open_cross_venue_position(
        "BTC", "forward", "okx", "binance", 500,
        dry_run=True, spot_venue=sv, futures_venue=fv, positions_path=path,
    )
    assert res.ok and res.state == "simulated"
    rows = load_positions(path)
    assert len(rows) == 1 and rows[0]["status"] == "open"
    assert rows[0]["dry_run"] is True and rows[0]["direction"] == "forward"
    assert rows[0]["futures_venue"] == "okx" and rows[0]["spot_venue"] == "binance"
    assert [t["type"] for t in sv.trades] == ["buy"]
    assert [t["type"] for t in fv.trades] == ["open_short"]


def test_live_forward_open_fills_both_legs_and_records(tmp_path):
    res, sv, fv, path = _open_live(tmp_path)
    assert res.ok and res.state == "filled"
    assert [t["type"] for t in sv.trades] == ["buy"]
    assert [t["type"] for t in fv.trades] == ["open_short"]
    rows = load_positions(path)
    assert rows[0]["dry_run"] is False
    assert rows[0]["qty"] == pytest.approx(5.0)
    assert rows[0]["futures_qty"] == pytest.approx(5.0)
    assert res.position_id == rows[0]["id"]


def test_live_reverse_open_borrow_sell_margin_leg(tmp_path):
    res, sv, fv, path = _open_live(tmp_path, direction="reverse")
    assert res.ok and res.state == "filled"
    spot_types = [t["type"] for t in sv.trades]
    assert spot_types[0] == "sell"
    assert sv.trades[0].get("account") == "margin"
    assert sv.trades[0].get("side_effect") == "auto_borrow"
    assert [t["type"] for t in fv.trades] == ["open_long"]
    # collateral transfer spot→margin attempted before the margin sell
    assert ("USDT", 300.0, "spot", "margin") in sv.transfers
    rows = load_positions(path)
    assert rows[0]["direction"] == "reverse"


def test_reverse_open_aborts_when_spot_venue_lacks_margin_support(tmp_path):
    path = tmp_path / "positions.json"
    sv = FakeSpotVenue("binance", supports_reverse=False)
    fv = FakeFuturesVenue("okx")
    res = open_cross_venue_position(
        "BTC", "reverse", "okx", "binance", 500,
        dry_run=False, spot_venue=sv, futures_venue=fv, positions_path=path,
    )
    assert not res.ok and res.state == "aborted"
    assert "margin borrow-sell" in res.logs[0]
    assert load_positions(path) == []


def test_futures_leg_fail_rolls_back_spot(tmp_path):
    sv, fv = FakeSpotVenue("binance"), FakeFuturesVenue("okx", fail_types={"open_short"})
    res, sv, fv, path = _open_live(tmp_path, sv=sv, fv=fv)
    assert not res.ok and res.state == "rolled_back"
    assert [t["type"] for t in sv.trades] == ["buy", "sell"]  # spot sold back
    assert [t["type"] for t in fv.trades] == ["open_short"]
    assert load_positions(path) == []  # no open record left behind


def test_futures_leg_fail_and_rollback_fail_is_naked(tmp_path):
    sv = FakeSpotVenue("binance", fail_types={"sell"})
    fv = FakeFuturesVenue("okx", fail_types={"open_short"})
    res, _, _, _ = _open_live(tmp_path, sv=sv, fv=fv)
    assert not res.ok and res.state == "naked"
    assert any("naked" in log for log in res.logs)


def test_spot_leg_fail_aborts_no_record(tmp_path):
    sv = FakeSpotVenue("binance", fail_types={"buy"})
    fv = FakeFuturesVenue("okx")
    res, sv, fv, path = _open_live(tmp_path, sv=sv, fv=fv)
    assert not res.ok and res.state == "aborted"
    assert fv.trades == []  # futures leg never submitted
    assert load_positions(path) == []


def test_venue_price_spread_gate_aborts(tmp_path):
    sv = FakeSpotVenue("binance", price=100.0)
    fv = FakeFuturesVenue("okx", price=103.0)  # 3% > 1% venue spread gate
    res, _, _, path = _open_live(tmp_path, sv=sv, fv=fv)
    assert not res.ok and res.state == "aborted"
    assert "Cross-venue spread" in res.logs[0]
    assert sv.trades == [] and fv.trades == []


def test_margin_shortfall_triggers_spot_to_futures_transfer(tmp_path):
    fv = FakeFuturesVenue("okx", balances={"spot": 5000.0, "futures": 100.0})
    res, sv, fv, path = _open_live(tmp_path, fv=fv)
    assert res.ok and res.state == "filled"
    assert len(fv.transfers) == 1
    asset, amount, src, dst = fv.transfers[0]
    assert asset == "USDT" and src == "spot" and dst == "futures"
    assert amount == pytest.approx(525.0)  # 5 qty * 100 px * 1.05 buffer


# ── margin readiness fail-closed knob ─────────────────────────────────────────


def test_margin_api_fail_default_rolls_back_spot(tmp_path):
    """Balance API failure with default (fail-closed) → spot leg rolled back, no futures order."""
    sv = FakeSpotVenue("binance")
    fv = FakeFuturesVenue("okx")

    def _boom():
        raise RuntimeError("api down")

    fv.fetch_usdt_account_balances = _boom
    res, sv, fv, path = _open_live(tmp_path, sv=sv, fv=fv)
    assert not res.ok and res.state == "rolled_back"
    assert [t["type"] for t in sv.trades] == ["buy", "sell"]  # rolled back
    assert fv.trades == []  # futures leg never submitted with unverified margin
    assert any("fail-closed" in log for log in res.logs)
    assert any("marginCheckFailOpen" in log for log in res.logs)
    assert load_positions(path) == []


def test_margin_api_fail_open_config_proceeds(tmp_path):
    """marginCheckFailOpen=true restores best-effort skip on API failure."""
    sv = FakeSpotVenue("binance")
    fv = FakeFuturesVenue("okx")

    def _boom():
        raise RuntimeError("api down")

    fv.fetch_usdt_account_balances = _boom
    res, sv, fv, path = _open_live(
        tmp_path,
        sv=sv,
        fv=fv,
        config={"marginCheckFailOpen": True, "fundingRecheck": False},
    )
    assert res.ok and res.state == "filled"
    assert any("skipped" in log for log in res.logs)


# ── close paths ───────────────────────────────────────────────────────────────


def test_close_forward_marks_closed(tmp_path):
    res, sv, fv, path = _open_live(tmp_path)
    sv.trades.clear()
    fv.trades.clear()
    res2 = close_cross_venue_position(
        res.position_id,
        dry_run=False,
        spot_venue=sv,
        futures_venue=fv,
        positions_path=path,
    )
    assert res2.ok and res2.state == "filled"
    assert [t["type"] for t in fv.trades] == ["close_short"]  # futures first
    assert [t["type"] for t in sv.trades] == ["sell"]
    rows = load_positions(path)
    assert rows[0]["status"] == "closed"
    assert rows[0]["close_info"]["spot_price"] == 100.0


def test_close_futures_fail_aborts_keeps_open(tmp_path):
    res, sv, fv, path = _open_live(tmp_path)
    fv.fail_types.add("close_short")
    res2 = close_cross_venue_position(
        res.position_id, dry_run=False, spot_venue=sv, futures_venue=fv,
        positions_path=path,
    )
    assert not res2.ok and res2.state == "aborted"
    assert load_positions(path)[0]["status"] == "open"


def test_close_spot_fail_reopens_futures_hedge(tmp_path):
    res, sv, fv, path = _open_live(tmp_path)
    sv.fail_types.add("sell")
    res2 = close_cross_venue_position(
        res.position_id, dry_run=False, spot_venue=sv, futures_venue=fv,
        positions_path=path,
    )
    assert not res2.ok and res2.state == "rolled_back"
    # futures: open_short → close_short → re-open_short hedge
    assert [t["type"] for t in fv.trades] == ["open_short", "close_short", "open_short"]
    assert load_positions(path)[0]["status"] == "open"  # position remains open


def test_close_unknown_or_closed_id_aborts(tmp_path):
    res, sv, fv, path = _open_live(tmp_path)
    res_missing = close_cross_venue_position(
        "xv-does-not-exist", dry_run=False, spot_venue=sv, futures_venue=fv,
        positions_path=path,
    )
    assert not res_missing.ok and res_missing.state == "aborted"
    close_cross_venue_position(
        res.position_id, dry_run=False, spot_venue=sv, futures_venue=fv,
        positions_path=path,
    )
    res_again = close_cross_venue_position(
        res.position_id, dry_run=False, spot_venue=sv, futures_venue=fv,
        positions_path=path,
    )
    assert not res_again.ok and res_again.state == "aborted"  # already closed


def test_dry_run_close_marks_closed(tmp_path):
    path = tmp_path / "positions.json"
    sv, fv = FakeSpotVenue("binance"), FakeFuturesVenue("okx")
    res = open_cross_venue_position(
        "BTC", "forward", "okx", "binance", 500,
        dry_run=True, spot_venue=sv, futures_venue=fv, positions_path=path,
    )
    res2 = close_cross_venue_position(
        res.position_id, spot_venue=sv, futures_venue=fv, positions_path=path
    )
    assert res2.ok and res2.state == "simulated"
    assert load_positions(path)[0]["status"] == "closed"


# ── persistence hardening ─────────────────────────────────────────────────────


def test_atomic_persistence_no_tmp_leftovers(tmp_path):
    res, sv, fv, path = _open_live(tmp_path)
    assert res.ok
    # ledger parses and no temp/junk files remain next to it
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) == 1
    leftovers = [
        p.name
        for p in tmp_path.iterdir()
        if p.name.endswith(".tmp") or p.name.startswith(".positions-")
    ]
    assert leftovers == []


def test_lock_file_created_on_record(tmp_path):
    res, sv, fv, path = _open_live(tmp_path)
    assert res.ok
    assert (tmp_path / "positions.lock").exists()


def test_corrupt_positions_quarantined(tmp_path, capsys):
    path = tmp_path / "positions.json"
    path.write_text('{"id": "xv-1", "status": "open", ', encoding="utf-8")  # truncated
    rows = load_positions(path)
    assert rows == []
    backups = list(tmp_path.glob("positions.corrupt-*.json"))
    assert len(backups) == 1
    assert not path.exists()  # original moved aside
    err = capsys.readouterr().err
    assert "[POSITIONS]" in err
    assert "quarantined" in err
    assert str(path) in err
    assert backups[0].read_text(encoding="utf-8").startswith('{"id"')  # data preserved


def test_load_non_list_json_returns_empty_without_quarantine(tmp_path):
    path = tmp_path / "positions.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    assert load_positions(path) == []
    assert path.exists()  # valid JSON: NOT quarantined
    assert list(tmp_path.glob("positions.corrupt-*.json")) == []


def test_mark_closed_only_closes_matching_open_id(tmp_path):
    path = tmp_path / "positions.json"
    from execution.cross_venue_executor import _mark_closed, _record_position

    _record_position({"id": "xv-a", "status": "open"}, path)
    _record_position({"id": "xv-b", "status": "open"}, path)
    assert _mark_closed("xv-missing", {}, path) is False
    assert _mark_closed("xv-a", {"dry_run": False}, path) is True
    assert _mark_closed("xv-a", {}, path) is False  # already closed
    rows = load_positions(path)
    by_id = {r["id"]: r for r in rows}
    assert by_id["xv-a"]["status"] == "closed"
    assert by_id["xv-b"]["status"] == "open"


# ── pre-submit funding re-check (carry semantics) ─────────────────────────────


def test_funding_recheck_not_ok_aborts_before_any_orders(tmp_path, monkeypatch):
    calls = []

    def _fake_recheck(venue, base, direction, quote="USDT", **kwargs):
        calls.append((venue, base, direction))
        return {
            "ok": False,
            "rate_pct": 0.001,
            "interval_h": 8.0,
            "reason": "spread_collapse: rate +0.0010% below +0.02% floor",
            "source": "rate",
        }

    monkeypatch.setattr(xvm, "recheck_carry_funding", _fake_recheck)
    sv, fv = FakeSpotVenue("binance"), FakeFuturesVenue("okx")
    res, sv, fv, path = _open_live(
        tmp_path, sv=sv, fv=fv, config={"fundingRecheck": True}
    )
    assert not res.ok and res.state == "aborted"
    assert calls == [("okx", "BTC", "forward")]
    assert any("funding re-check" in log and "spread_collapse" in log for log in res.logs)
    assert sv.trades == [] and fv.trades == []  # no orders submitted
    assert load_positions(path) == []


def test_funding_recheck_ok_proceeds_to_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(
        xvm,
        "recheck_carry_funding",
        lambda venue, base, direction, quote="USDT", **kwargs: {
            "ok": True,
            "rate_pct": 0.05,
            "interval_h": 8.0,
            "reason": "carry rate +0.0500%/8h @ okx >= +0.02% floor",
            "source": "rate",
        },
    )
    res, sv, fv, path = _open_live(tmp_path, config={"fundingRecheck": True})
    assert res.ok and res.state == "filled"
    assert any("funding re-check" in log for log in res.logs)


def test_funding_recheck_disabled_skips_call(tmp_path, monkeypatch):
    calls = []

    def _fake_recheck(*args, **kwargs):
        calls.append(args)
        return {"ok": True, "reason": "", "source": "rate"}

    monkeypatch.setattr(xvm, "recheck_carry_funding", _fake_recheck)
    res, _, _, _ = _open_live(tmp_path, config={"fundingRecheck": False})
    assert res.ok and res.state == "filled"
    assert calls == []


def test_funding_recheck_default_on_when_config_given(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        xvm,
        "recheck_carry_funding",
        lambda *args, **kwargs: calls.append(args)
        or {"ok": True, "reason": "fine", "source": "rate"},
    )
    res, _, _, _ = _open_live(tmp_path, config={"marginCheckFailOpen": False})
    assert res.ok and res.state == "filled"
    assert len(calls) == 1  # no fundingRecheck key → default ON
