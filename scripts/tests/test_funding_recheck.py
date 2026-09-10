#!/usr/bin/env python3
"""Hermetic tests for the pre-submit funding edge re-check (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import execution.funding_recheck as fr_mod  # noqa: E402
from execution.funding_recheck import (  # noqa: E402
    cfg_lookup,
    recheck_carry_funding,
    recheck_funding_edge,
)


class FakeFP:
    """Funding provider double mirroring the fetch_all/fetch_interval_map shape."""

    def __init__(self, rows, interval_map=None, fail=False):
        self.rows = rows
        self.interval_map = interval_map or {}
        self.fail = fail
        self.fetch_all_calls = 0

    def fetch_all(self, quote="USDT"):
        self.fetch_all_calls += 1
        if self.fail:
            raise RuntimeError("api down")
        return [dict(r) for r in self.rows]

    def fetch_interval_map(self, quote="USDT"):
        return dict(self.interval_map)


def _row(rate, interval_h=8.0, symbol="BTCUSDT", mark=0.0, index=0.0, next_ts=0):
    return {
        "symbol": symbol,
        "rate_pct": rate,
        "interval_h": interval_h,
        "next_funding_ts": next_ts,
        "mark_price": mark,
        "index_price": index,
    }


@pytest.fixture(autouse=True)
def _clean_cache():
    fr_mod.clear_row_cache()
    yield
    fr_mod.clear_row_cache()


# ── recheck_funding_edge ─────────────────────────────────────────────────────


def test_same_interval_spread_above_floor_ok():
    providers = {
        "okx": FakeFP([_row(0.01)]),        # long leg
        "bybit": FakeFP([_row(0.05)]),      # short leg
    }
    res = recheck_funding_edge("okx", "bybit", "BTC", providers=providers)
    assert res["ok"] is True
    assert res["spread_pct"] == pytest.approx(0.04)  # short 0.05 - long 0.01
    assert res["long_rate_pct"] == pytest.approx(0.01)
    assert res["short_rate_pct"] == pytest.approx(0.05)
    assert res["long_interval_h"] == pytest.approx(8.0)
    assert res["short_interval_h"] == pytest.approx(8.0)
    assert res["source"] == "rate"  # same-interval pair: plain rate spread
    assert "floor" in res["reason"]


def test_same_interval_spread_below_floor_not_ok():
    providers = {
        "okx": FakeFP([_row(0.04)]),
        "bybit": FakeFP([_row(0.05)]),
    }
    res = recheck_funding_edge("okx", "bybit", "BTC", providers=providers)
    assert res["ok"] is False
    assert res["spread_pct"] == pytest.approx(0.01)
    assert "spread_collapse" in res["reason"]
    # reason echoes both rates + the spread floor
    assert "+0.04" in res["reason"] and "+0.05" in res["reason"]
    assert "0.02" in res["reason"]


def test_cross_interval_pair_uses_pair_spread_model():
    """Mismatched intervals route through pair_pure_futures_spread (hourly blend)."""
    providers = {
        "hyperliquid": FakeFP(
            [_row(0.002, interval_h=1.0)], interval_map={"BTCUSDT": 1.0}
        ),  # long 1h
        "okx": FakeFP(
            [_row(0.20, interval_h=8.0)], interval_map={"BTCUSDT": 8.0}
        ),  # short 8h
    }
    res = recheck_funding_edge(
        "hyperliquid", "okx", "BTC", providers=providers, min_spread_pct=0.02
    )
    # long hourly 0.002, short hourly 0.025 → spread over 1h eff interval = 0.023
    assert res["ok"] is True
    assert res["spread_pct"] == pytest.approx(0.023)
    assert res["long_interval_h"] == pytest.approx(1.0)
    assert res["short_interval_h"] == pytest.approx(8.0)
    assert res["source"] == "rate_linear"  # mismatch, but no mark/index → no basis


def test_cross_interval_with_basis_blend_source():
    providers = {
        "hyperliquid": FakeFP(
            [_row(0.002, interval_h=1.0, mark=100.0, index=100.4)],
            interval_map={"BTCUSDT": 1.0},
        ),
        "okx": FakeFP(
            [_row(0.20, interval_h=8.0, mark=100.0, index=100.0)],
            interval_map={"BTCUSDT": 8.0},
        ),
    }
    res = recheck_funding_edge("hyperliquid", "okx", "BTC", providers=providers)
    assert res["source"] == "basis_blend"


def test_fetch_exception_fail_closed_by_default():
    providers = {
        "okx": FakeFP([_row(0.01)]),
        "bybit": FakeFP([], fail=True),
    }
    res = recheck_funding_edge("okx", "bybit", "BTC", providers=providers)
    assert res["ok"] is False
    assert "funding fetch failed" in res["reason"]
    assert "fail-closed" in res["reason"]
    assert res["source"] == "error"


def test_fetch_exception_fail_open_degraded_ok():
    providers = {
        "okx": FakeFP([_row(0.01)]),
        "bybit": FakeFP([], fail=True),
    }
    res = recheck_funding_edge("okx", "bybit", "BTC", providers=providers, fail_open=True)
    assert res["ok"] is True
    assert "fail-open" in res["reason"]
    assert res["source"] == "error_fail_open"


def test_min_spread_pct_defaults_to_0_02():
    providers = {
        "okx": FakeFP([_row(0.00)]),
        "bybit": FakeFP([_row(0.02)]),
    }
    # exactly at default floor 0.02 → ok (>=)
    res = recheck_funding_edge("okx", "bybit", "BTC", providers=providers)
    assert res["ok"] is True
    providers["bybit"] = FakeFP([_row(0.019)])
    fr_mod.clear_row_cache()
    res2 = recheck_funding_edge("okx", "bybit", "BTC", providers=providers)
    assert res2["ok"] is False


def test_row_cache_hits_within_ttl():
    providers = {
        "okx": FakeFP([_row(0.01)]),
        "bybit": FakeFP([_row(0.05)]),
    }
    recheck_funding_edge("okx", "bybit", "BTC", providers=providers)
    recheck_funding_edge("okx", "bybit", "BTC", providers=providers)
    recheck_funding_edge("okx", "bybit", "BTC", providers=providers)
    # one bulk fetch per venue; the rest served from the 20s TTL cache
    assert providers["okx"].fetch_all_calls == 1
    assert providers["bybit"].fetch_all_calls == 1


def test_missing_symbol_row_is_fetch_error():
    providers = {
        "okx": FakeFP([_row(0.01, symbol="ETHUSDT")]),
        "bybit": FakeFP([_row(0.05)]),
    }
    res = recheck_funding_edge("okx", "bybit", "BTC", providers=providers)
    assert res["ok"] is False
    assert "funding fetch failed" in res["reason"]


def test_providers_none_falls_back_to_factory(monkeypatch):
    """providers=None uses get_funding_provider — monkeypatched, no network."""
    fakes = {
        "okx": FakeFP([_row(0.01)]),
        "bybit": FakeFP([_row(0.05)]),
    }
    monkeypatch.setattr(fr_mod, "get_funding_provider", lambda venue: fakes[venue])
    res = recheck_funding_edge("okx", "bybit", "BTC", providers=None)
    assert res["ok"] is True
    assert res["spread_pct"] == pytest.approx(0.04)
    assert fakes["okx"].fetch_all_calls == 1
    assert fakes["bybit"].fetch_all_calls == 1


# ── recheck_carry_funding ─────────────────────────────────────────────────────


def test_carry_forward_rate_above_floor_ok():
    res = recheck_carry_funding(
        "okx", "BTC", "forward", providers={"okx": FakeFP([_row(0.05)])}
    )
    assert res["ok"] is True
    assert res["rate_pct"] == pytest.approx(0.05)
    assert "forward" in res["reason"]


def test_carry_forward_rate_below_floor_not_ok():
    res = recheck_carry_funding(
        "okx", "BTC", "forward", providers={"okx": FakeFP([_row(0.01)])}
    )
    assert res["ok"] is False
    assert "spread_collapse" in res["reason"]


def test_carry_reverse_negative_rate_ok_and_sign_flip_blocked():
    providers = {"okx": FakeFP([_row(-0.05)])}
    res = recheck_carry_funding("okx", "BTC", "reverse", providers=providers)
    assert res["ok"] is True
    # rate flipped positive against a reverse (long-perp) position → blocked
    fr_mod.clear_row_cache()  # drop the cached -0.05 row first
    res2 = recheck_carry_funding(
        "okx", "BTC", "reverse", providers={"okx": FakeFP([_row(0.01)])}
    )
    assert res2["ok"] is False
    assert "sign flipped" in res2["reason"]


def test_carry_fetch_error_fail_closed_then_fail_open():
    providers = {"okx": FakeFP([], fail=True)}
    res = recheck_carry_funding("okx", "BTC", "forward", providers=providers)
    assert res["ok"] is False
    assert "fail-closed" in res["reason"]
    res2 = recheck_carry_funding(
        "okx", "BTC", "forward", providers=providers, fail_open=True
    )
    assert res2["ok"] is True
    assert "fail-open" in res2["reason"]


# ── cfg_lookup ────────────────────────────────────────────────────────────────


def test_cfg_lookup_block_aware():
    cfg = {
        "pureFuturesArbitrage": {"fundingRecheck": False, "minSpreadPct": 0.05},
        "crossAssetArbitrage": {"fundingRecheck": True},
        "marginCheckFailOpen": True,
    }
    assert cfg_lookup(cfg, "fundingRecheck", True) is False  # pfa block wins
    assert cfg_lookup(cfg, "minSpreadPct", 0.02) == 0.05
    assert cfg_lookup(cfg, "marginCheckFailOpen", False) is True  # top-level
    assert cfg_lookup(None, "anything", "dft") == "dft"
    assert cfg_lookup({}, "missing", "dft") == "dft"
    assert cfg_lookup({"crossAssetArbitrage": {"k": 1}}, "k", 0) == 1
