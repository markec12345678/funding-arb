#!/usr/bin/env python3
"""Hermetic tests for core/funding_history.py (no network, tmp FARB_HOME)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.funding_history as fh  # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FARB_HOME", str(tmp_path / "farb"))
    monkeypatch.delenv("FARB_HOME_LEGACY", raising=False)
    monkeypatch.setenv("FARB_FUNDING_HISTORY", "1")
    return tmp_path / "farb"


def _by_base(rate_long=0.03, rate_short=0.15):
    return {
        "BTC": {
            "binance": {"rate_pct": rate_long, "interval_h": 8.0},
            "bitget": {"rate_pct": rate_short, "interval_h": 8.0},
        },
    }


def test_record_writes_one_line(home):
    assert fh.record_scan_rates(_by_base(), now=1000.0)
    p = fh.history_path()
    assert p.exists()
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["ts"] == 1000.0
    assert obj["rates"]["BTC"]["binance"]["r"] == 0.03
    assert obj["rates"]["BTC"]["bitget"]["ih"] == 8.0


def test_record_throttled_within_interval(home):
    assert fh.record_scan_rates(_by_base(), now=1000.0)
    assert fh.record_scan_rates(_by_base(), now=2000.0) is False  # <1h
    assert fh.record_scan_rates(_by_base(), now=1000.0 + 3601.0) is True


def test_record_can_be_disabled_by_env(home, monkeypatch):
    monkeypatch.setenv("FARB_FUNDING_HISTORY", "0")
    assert fh.record_scan_rates(_by_base(), now=1000.0) is False
    assert not fh.history_path().exists()


def test_record_never_raises_on_garbage(home):
    assert fh.record_scan_rates({}, now=1000.0) is False
    assert fh.record_scan_rates({"BTC": None}, now=1000.0) is False


def _write_snapshots(path, spreads, base_ts=1000.0, step=3600.0):
    """spreads: list of (long_rate, short_rate) tuples."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for i, (long_r, short_r) in enumerate(spreads):
            snap = {
                "ts": base_ts + i * step,
                "rates": {
                    "BTC": {
                        "binance": {"r": long_r, "ih": 8.0},
                        "bitget": {"r": short_r, "ih": 8.0},
                    }
                },
            }
            f.write(json.dumps(snap) + "\n")


def test_load_recent_snapshots_filters_window(home):
    p = fh.history_path()
    now = time.time()
    _write_snapshots(p, [(0.03, 0.15)] * 3, base_ts=now - 10 * 86400)
    _write_snapshots(p, [(0.03, 0.16)] * 2, base_ts=now - 3600.0)
    snaps = fh.load_recent_snapshots(window_days=7.0, now=now)
    assert len(snaps) == 2


def test_load_skips_corrupt_lines(home):
    p = fh.history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json\n" + json.dumps({"ts": 5.0, "rates": {}}) + "\n{broken\n")
    snaps = fh.load_recent_snapshots(window_days=1.0, now=10.0)
    assert len(snaps) == 1


def test_pair_metrics_computes_stats(home):
    p = fh.history_path()
    # 12 hourly snapshots, spread always 0.12 (binance 0.03 vs bitget 0.15)
    _write_snapshots(p, [(0.03, 0.15)] * 12)
    snaps = fh.load_recent_snapshots(window_days=7.0, now=1000.0 + 12 * 3600 + 10)
    m = fh.pair_history_metrics(
        "BTC", "binance", "bitget",
        current_spread_pct=0.12, entry_threshold_pct=0.05,
        snapshots=snaps,
    )
    assert m is not None
    assert m["samples"] == 12
    assert m["spread_mean_pct"] == pytest.approx(0.12)
    assert m["spread_std_pct"] == pytest.approx(0.0, abs=1e-9)
    assert m["spread_z"] is None  # std ~ 0 -> z undefined
    assert m["stable_pct"] == 100.0


def test_pair_metrics_z_and_stability(home):
    p = fh.history_path()
    # alternating spreads 0.10 / 0.14 -> mean 0.12, std 0.02
    spreads = [(0.03, 0.13), (0.03, 0.17)] * 8  # 16 snapshots
    _write_snapshots(p, spreads)
    snaps = fh.load_recent_snapshots(window_days=7.0, now=1000.0 + 16 * 3600 + 10)
    m = fh.pair_history_metrics(
        "BTC", "binance", "bitget",
        current_spread_pct=0.16, entry_threshold_pct=0.12,
        snapshots=snaps,
    )
    assert m["samples"] == 16
    assert m["spread_mean_pct"] == pytest.approx(0.12)
    assert m["spread_std_pct"] == pytest.approx(0.02)
    assert m["spread_z"] == pytest.approx(2.0)  # (0.16-0.12)/0.02
    assert m["stable_pct"] == 50.0  # half the snapshots >= 0.12


def test_pair_metrics_insufficient_samples_returns_none(home):
    p = fh.history_path()
    _write_snapshots(p, [(0.03, 0.15)] * 5)
    snaps = fh.load_recent_snapshots(window_days=7.0, now=1000.0 + 5 * 3600 + 10)
    m = fh.pair_history_metrics(
        "BTC", "binance", "bitget",
        current_spread_pct=0.12, entry_threshold_pct=0.05,
        snapshots=snaps,
    )
    assert m is None


def test_pair_metrics_unknown_pair_returns_none(home):
    p = fh.history_path()
    _write_snapshots(p, [(0.03, 0.15)] * 12)
    snaps = fh.load_recent_snapshots(window_days=7.0, now=1000.0 + 12 * 3600 + 10)
    m = fh.pair_history_metrics(
        "ETH", "binance", "bitget",
        current_spread_pct=0.12, entry_threshold_pct=0.05,
        snapshots=snaps,
    )
    assert m is None


def test_scanner_attaches_history_to_rows(home, monkeypatch):
    """End-to-end: _scan_spreads attaches metrics when snapshots exist."""
    from cli.scan_pure_futures_spreads import _scan_spreads
    from core.fee_providers import offline_fee_cache_from_by_base

    p = fh.history_path()
    _write_snapshots(p, [(0.03, 0.15)] * 12)
    snaps = fh.load_recent_snapshots(window_days=7.0, now=1000.0 + 12 * 3600 + 10)

    by_base = {
        "BTC": {
            "binance": {"symbol": "BTCUSDT", "rate_pct": 0.03, "interval_h": 8.0,
                        "next_funding_ts": 100000000, "mark_price": 100000.0},
            "bitget": {"symbol": "BTCUSDT", "rate_pct": 0.15, "interval_h": 8.0,
                       "next_funding_ts": 100000100, "mark_price": 100001.0},
        },
    }
    fwd, rev = _scan_spreads(
        by_base, 0.01, 0.001,
        fee_cache=offline_fee_cache_from_by_base(by_base),
        history_snapshots=snaps,
    )
    assert len(fwd) == 1
    hist = fwd[0].get("history")
    assert hist is not None
    assert hist["samples"] == 12
    assert hist["spread_mean_pct"] == pytest.approx(0.12)
    assert hist["stable_pct"] == 100.0


def test_scanner_rows_unchanged_without_history(home):
    """No snapshots → no history key, row shape identical to before."""
    from cli.scan_pure_futures_spreads import _scan_spreads
    from core.fee_providers import offline_fee_cache_from_by_base

    by_base = {
        "BTC": {
            "binance": {"symbol": "BTCUSDT", "rate_pct": 0.03, "interval_h": 8.0,
                        "next_funding_ts": 100000000, "mark_price": 100000.0},
            "bitget": {"symbol": "BTCUSDT", "rate_pct": 0.15, "interval_h": 8.0,
                       "next_funding_ts": 100000100, "mark_price": 100001.0},
        },
    }
    fwd, rev = _scan_spreads(
        by_base, 0.01, 0.001,
        fee_cache=offline_fee_cache_from_by_base(by_base),
        history_snapshots=[],
    )
    assert len(fwd) == 1
    assert "history" not in fwd[0]


def test_record_throttle_with_oversized_lines(home):
    """Lines can exceed 100KB (972 bases); a fixed tail-peek would read a
    truncated line, fail to parse, and silently disable throttling."""
    p = fh.history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    big = {"pad": "x" * 200_000}
    # First: one real snapshot line (big, so any 512B peek lands mid-line).
    assert fh.record_scan_rates(_by_base(), now=1000.0)
    # Append a huge synthetic line to simulate the real 100KB+ matrix rows.
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1500.0, **big}) + "\n")
    # A scan 10 minutes later must be throttled by the ts=1500 line.
    assert fh.record_scan_rates(_by_base(), now=2100.0) is False
    assert fh.record_scan_rates(_by_base(), now=1500.0 + 3601.0) is True
