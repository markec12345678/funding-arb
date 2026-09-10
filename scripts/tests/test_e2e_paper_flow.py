"""End-to-end paper-flow smoke test: live-like config → scanner → strategy
decision → funding re-check → executor → state file → watcher → exit.

This is the integration proof the unit suite lacks: the REAL runner
(``execution.run_pure_futures_spread.run_once``) is driven through a full
open→monitor→exit lifecycle with a live-like config. The public-data gates
(depth + funding re-check) run in BOTH live and paper mode — only the margin
gate is live-only (it needs authenticated balance APIs) — while every
venue/HTTP touchpoint is faked:

  scanner rows (3 candidates, one clears the funnel)
      → runner threshold funnel (spread gate, fee gate, mismatch planner)
      → executor: mark-spread gate → min-qty gate → depth gate → funding
        re-check → (live-only: margin gate) → parallel leg submission →
        ATOMIC position persistence
      → state file on disk (status=open)
      → watcher fee-aware exit decision (net = spread − taker fees)
      → runner exit-first loop → live close vs fakes → state file (closed)
      → journal.jsonl records both cycles

A second test proves the re-check gate blocks a collapsed spread BEFORE any
order is submitted (the scan→execute race fix).

No network: venues, depth checker and funding providers are all injected;
``_venue`` construction is patched at the executor module level.
"""

from __future__ import annotations

import json
import sys
import time
from functools import partial
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import execution.pure_futures_executor as pfe  # noqa: E402
import execution.run_pure_futures_spread as runner  # noqa: E402
import market.futures_depth as depth_mod  # noqa: E402
from execution.funding_recheck import (  # noqa: E402
    clear_row_cache,
    recheck_funding_edge,
)
from execution.pure_futures_executor import (  # noqa: E402
    close_pure_futures_pair,
    load_pure_futures_positions,
    open_pure_futures_pair,
)
from execution.pure_futures_watcher import check_exit  # noqa: E402

TMP = Path(__file__).parent / "_tmp_e2e_paper"


# ─── Fakes ────────────────────────────────────────────────────────────────


class FakeFuturesVenue:
    """Minimal CexVenue-shaped venue (same contract as the executor tests)."""

    def __init__(self, venue_id: str, price: float = 100.0):
        self.venue_id = venue_id
        self.price = price
        self.trades: list[dict] = []
        self.initialized: list[str] = []
        self.transfers: list[tuple] = []
        self.balances = {"spot": 100_000.0, "futures": 100_000.0}

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

    def transfer_asset(self, asset, amount, from_account, to_account):
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
            out.append(
                {
                    "symbol": t["symbol"],
                    "type": t["type"],
                    "status": "simulated" if dry_run else "filled",
                    "exec_qty": t["amount_base"],
                    "exec_price": self.price,
                }
            )
        return out


class FakeFundingProvider:
    """FundingProvider duck-type: bulk current rates + interval map."""

    def __init__(self, symbol: str, rate_pct: float):
        self.symbol = symbol
        self.rate_pct = rate_pct
        self.calls = 0

    def fetch_all(self, quote: str = "USDT"):
        self.calls += 1
        return [
            {
                "symbol": self.symbol,
                "rate_pct": self.rate_pct,
                "next_funding_ts": int(time.time() * 1000) + 4 * 3600 * 1000,
                "mark_price": 100.0,
                "index_price": 100.0,
            }
        ]

    def fetch_interval_map(self, quote: str = "USDT"):
        return {self.symbol: 8.0}


def _scan_rows(bybit_short_rate: float) -> dict:
    """Scanner output shape: forward rows for BTC/ETH/SOL on binance+bybit.

    BTC row A carries a tradable edge; ETH row B dies at the fee gate
    (spread clears but net edge does not); SOL row C dies at the spread gate.
    """
    spread = bybit_short_rate - 0.01
    return {
        "forward": [
            {
                "base": "BTC",
                "direction": "forward",
                "long_venue": "binance",
                "short_venue": "bybit",
                "long_rate_pct": 0.01,
                "short_rate_pct": bybit_short_rate,
                "long_interval_h": 8,
                "short_interval_h": 8,
                "spread_pct": spread,
                "fee_pct": 0.09,
                "net_edge_pct": spread - 0.09,
                "settle_mismatch": False,
            },
            {
                "base": "ETH",
                "direction": "forward",
                "long_venue": "binance",
                "short_venue": "bybit",
                "long_rate_pct": 0.01,
                "short_rate_pct": 0.07,
                "long_interval_h": 8,
                "short_interval_h": 8,
                "spread_pct": 0.06,
                "fee_pct": 0.11,
                "net_edge_pct": -0.05,
                "settle_mismatch": False,
            },
            {
                "base": "SOL",
                "direction": "forward",
                "long_venue": "binance",
                "short_venue": "bybit",
                "long_rate_pct": 0.01,
                "short_rate_pct": 0.04,
                "long_interval_h": 8,
                "short_interval_h": 8,
                "spread_pct": 0.03,
                "fee_pct": 0.11,
                "net_edge_pct": -0.08,
                "settle_mismatch": False,
            },
        ],
        "reverse": [],
    }


def _live_like_cfg() -> dict:
    # dry_run=False so the executor takes the LIVE path (depth/margin/re-check
    # gates + parallel submissions); the venues are fakes so nothing real
    # can be touched. This is the "live-like config" under test.
    return {
        "strategy": "pure_futures_spread",
        "dry_run": False,
        "cash": "USDT",
        "pureFuturesArbitrage": {
            "venues": ["binance", "bybit"],
            "maxConcurrentPairs": 3,
            "tradeUsdPerPair": 500.0,  # overridden to 5000 by strategy defaults
            "minSpreadPct": 0.05,  # overridden to 0.04 by strategy defaults
            "minNetEdgePct": 0.01,  # overridden to 0.02 by strategy defaults
            "exitThresholdPct": 0.01,
            "maxMarkSpreadPct": 1.0,
            "allowSettleMismatch": False,
            "depthCheckEnabled": True,
            "depthMaxDevPct": 0.3,
            "depthMinMultiple": 3.0,
            "depthCheckFailOpen": False,
            "marginCheckFailOpen": False,
            "fundingRecheck": True,
            "fundingRecheckMinSpreadPct": 0.02,
            "fundingRecheckFailOpen": False,
            "feeAwareExit": True,
            "parallelLegs": True,
        },
    }


def _wire(
    monkeypatch,
    tmp_path: Path,
    *,
    binance_rate: float,
    bybit_rate: float,
    scan_bybit_rate: float,
):
    """Patch the full chain onto tmp_path + fakes. Returns (venues, gates)."""
    positions_path = tmp_path / "positions.json"
    journal_path = tmp_path / "journal.jsonl"

    venues = {
        "binance": FakeFuturesVenue("binance", price=100.0),
        "bybit": FakeFuturesVenue("bybit", price=100.2),
    }
    providers = {
        "binance": FakeFundingProvider("BTCUSDT", binance_rate),
        "bybit": FakeFundingProvider("BTCUSDT", bybit_rate),
    }
    gates = {"depth_calls": [], "recheck_calls": []}

    def fake_scan(**kwargs):
        # The runner must scan with relaxed thresholds so held positions
        # stay visible for exit decisions — assert the wiring contract.
        assert kwargs.get("min_spread") == 0.0, "scanner must be relaxed for exits"
        assert kwargs.get("min_edge") == -999.0, "scanner must be relaxed for exits"
        return _scan_rows(scan_bybit_rate)

    def fake_depth(long_id, short_id, base, trade_usd, **kwargs):
        gates["depth_calls"].append((long_id, short_id, base, trade_usd))
        return True, "e2e fake depth: 12.0x multiple within 0.10%"

    real_recheck = recheck_funding_edge

    def recheck_with_providers(long_id, short_id, base, quote="USDT", **kwargs):
        gates["recheck_calls"].append((long_id, short_id, base, kwargs))
        return real_recheck(
            long_id, short_id, base, quote, providers=providers, **kwargs
        )

    def fake_venue_factory(venue_id, injected):
        if injected is not None:
            return injected
        return venues[str(venue_id).lower()]

    monkeypatch.setattr(runner, "scan_pure_futures_spreads", fake_scan)
    monkeypatch.setattr(pfe, "_venue", fake_venue_factory)
    monkeypatch.setattr(pfe, "recheck_funding_edge", recheck_with_providers)
    monkeypatch.setattr(depth_mod, "check_pair_depth", fake_depth)
    monkeypatch.setattr(
        runner, "open_pure_futures_pair", partial(open_pure_futures_pair, positions_path=positions_path)
    )
    monkeypatch.setattr(
        runner, "close_pure_futures_pair", partial(close_pure_futures_pair, positions_path=positions_path)
    )
    monkeypatch.setattr(
        runner, "load_pure_futures_positions", lambda: load_pure_futures_positions(positions_path)
    )
    monkeypatch.setattr(runner, "JOURNAL_PATH", journal_path)
    clear_row_cache()
    return venues, gates, positions_path, journal_path


# ─── Tests ────────────────────────────────────────────────────────────────


def test_full_paper_flow_open_hold_exit(monkeypatch, tmp_path):
    """scanner → strategy → recheck → executor → state → watcher → exit → closed ledger."""
    venues, gates, positions_path, journal_path = _wire(
        monkeypatch,
        tmp_path,
        binance_rate=0.01,  # long leg funding
        bybit_rate=0.13,  # short leg funding → recheck spread 0.12% ≥ 0.02 floor
        scan_bybit_rate=0.13,  # scanner sees the same healthy spread
    )
    cfg = _live_like_cfg()

    # ── Phase 1: open cycle ────────────────────────────────────────────
    out = runner.run_once(cfg)

    assert out["scan_total"] == 3
    # funnel: BTC clears spread(0.04)+fee(0.02) gates; ETH dies at fee gate;
    # SOL dies at spread gate → exactly one executable candidate.
    assert out["candidates_after_filter"] == 1
    assert out["dry_run"] is False
    opens = [a for a in out["actions"] if a["action"] == "open"]
    assert len(opens) == 1
    assert opens[0]["result"]["state"] == "filled"
    assert opens[0]["candidate"]["base"] == "BTC"

    # gates actually ran on the live path
    assert gates["depth_calls"] == [("binance", "bybit", "BTC", 5000.0)]
    assert len(gates["recheck_calls"]) == 1
    assert gates["recheck_calls"][0][:3] == ("binance", "bybit", "BTC")

    # legs submitted live to both fake venues (open_long / open_short)
    assert [t["type"] for t in venues["binance"].trades] == ["open_long"]
    assert [t["type"] for t in venues["bybit"].trades] == ["open_short"]
    assert all(t["dry_run"] is False for t in venues["binance"].trades + venues["bybit"].trades)

    # state file: atomic-persisted open position
    positions = json.loads(positions_path.read_text())
    assert len(positions) == 1
    pos = positions[0]
    assert pos["status"] == "open"
    assert pos["dry_run"] is False
    assert pos["base"] == "BTC"
    assert pos["direction"] == "forward"
    assert pos["long_venue"] == "binance"
    assert pos["short_venue"] == "bybit"
    assert pos["id"].startswith("pf-BTC-binance-bybit-")
    assert pos["qty"] == pytest.approx(49.9001, abs=1e-4)  # 5000 USD / 100.2 ref price, floored at 4 decimals
    assert 0.0 < pos["mark_spread_pct"] < 1.0

    # journal captured the cycle
    lines = journal_path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["scan_total"] == 3

    # ── Phase 2: watcher fee-aware exit decision ───────────────────────
    # Healthy market: raw spread 0.155%, net 0.155−0.11 = 0.045% > exit 0.01 → hold.
    healthy = {
        "BTC": {
            "binance": {"rate_pct": 0.005},
            "bybit": {"rate_pct": 0.16},
        }
    }
    should_exit, reason = check_exit(pos, healthy, 0.01, fee_aware=True, fee_pct=0.11)
    assert should_exit is False and reason == ""

    # Fee-aware divergence case (the P0 fix): raw spread 0.05% > 0.01 would
    # make the legacy watcher HOLD; net 0.05−0.11 = −0.06 ≤ 0.01 must EXIT.
    thin = {"BTC": {"binance": {"rate_pct": 0.05}, "bybit": {"rate_pct": 0.10}}}
    should_exit, reason = check_exit(pos, thin, 0.01, fee_aware=True, fee_pct=0.11)
    assert should_exit is True and "spread_collapse" in reason
    legacy_exit, _ = check_exit(pos, thin, 0.01, fee_aware=False)
    assert legacy_exit is False  # raw-spread watcher would have held a losing pair

    # Collapsed market: both modes exit.
    collapsed = {"BTC": {"binance": {"rate_pct": 0.0625}, "bybit": {"rate_pct": 0.0675}}}
    should_exit, reason = check_exit(pos, collapsed, 0.01, fee_aware=True, fee_pct=0.11)
    assert should_exit is True and "spread_collapse" in reason

    # ── Phase 3: exit cycle (runner exit-first loop) ────────────────────
    clear_row_cache()  # avoid the 20s recheck row cache from phase 1
    venues["binance"].trades.clear()
    venues["bybit"].trades.clear()
    # scanner now sees the collapsed spread (0.005% raw, −0.105% net)
    monkeypatch.setattr(
        runner,
        "scan_pure_futures_spreads",
        lambda **kw: _scan_rows(0.0675),
    )
    out2 = runner.run_once(cfg)

    closes = [a for a in out2["actions"] if a["action"] == "close"]
    assert len(closes) == 1
    assert closes[0]["result"]["state"] == "filled"
    # exit was decided on the collapsed net edge
    assert closes[0]["edge"] <= 0.01
    # no re-open: nothing clears the entry funnel on a collapsed market
    assert out2["candidates_after_filter"] == 0
    assert out2["open_positions"] == 0

    # close legs actually submitted (short first per executor contract)
    types_bybit = [t["type"] for t in venues["bybit"].trades]
    types_binance = [t["type"] for t in venues["binance"].trades]
    assert "close_short" in types_bybit and "close_long" in types_binance

    # final ledger: closed with close_info
    positions2 = json.loads(positions_path.read_text())
    assert len(positions2) == 1
    pos2 = positions2[0]
    assert pos2["status"] == "closed"
    assert pos2["id"] == pos["id"]
    ci = pos2.get("close_info") or {}
    assert ci.get("long_price", 0) > 0 and ci.get("short_price", 0) > 0

    # journal recorded both cycles
    lines2 = journal_path.read_text().strip().splitlines()
    assert len(lines2) == 2


def test_paper_mode_runs_public_gates(monkeypatch, tmp_path):
    """Paper (dry_run=True) must exercise the SAME public-data gates as live
    (depth + funding re-check) so paper trading measures the real funnel;
    only the credential-bound margin gate stays live-only."""
    venues, gates, positions_path, _journal = _wire(
        monkeypatch,
        tmp_path,
        binance_rate=0.01,
        bybit_rate=0.13,  # healthy recheck spread
        scan_bybit_rate=0.13,
    )
    cfg = _live_like_cfg()
    cfg["dry_run"] = True  # paper mode

    out = runner.run_once(cfg)

    assert out["dry_run"] is True
    opens = [a for a in out["actions"] if a["action"] == "open"]
    assert len(opens) == 1
    # paper entry is a simulated fill — but only AFTER the gates cleared
    assert opens[0]["result"]["state"] == "simulated"
    assert gates["depth_calls"] == [("binance", "bybit", "BTC", 5000.0)]
    assert len(gates["recheck_calls"]) == 1
    assert any("funding re-check" in log for log in opens[0]["result"].get("logs", []))

    positions = json.loads(positions_path.read_text())
    assert positions[0]["status"] == "open"
    assert positions[0]["dry_run"] is True


def test_paper_mode_recheck_reject_blocks_simulated_open(monkeypatch, tmp_path):
    """In paper mode a collapsed re-check spread aborts BEFORE the simulated
    entry — the scan→execute race is measurable in paper, not just live."""
    venues, gates, positions_path, _journal = _wire(
        monkeypatch,
        tmp_path,
        binance_rate=0.06,  # recheck sees collapsed 0.0075% < 0.02 floor
        bybit_rate=0.0675,
        scan_bybit_rate=0.13,  # stale scanner still sees the healthy spread
    )
    cfg = _live_like_cfg()
    cfg["dry_run"] = True
    # paper default is fail-open, but the explicit template setting is
    # fail-closed — reproduce the template semantics here
    cfg["pureFuturesArbitrage"]["fundingRecheckFailOpen"] = False

    out = runner.run_once(cfg)

    opens = [a for a in out["actions"] if a["action"] == "open"]
    assert len(opens) == 1
    assert opens[0]["result"]["state"] == "aborted"
    assert venues["binance"].trades == [] and venues["bybit"].trades == []
    assert not positions_path.exists()
    assert out["open_positions"] == 0


def test_recheck_gate_blocks_collapsed_spread_before_orders(monkeypatch, tmp_path):
    """The pre-submit funding re-check aborts when the spread collapsed after
    the scan (scan→execute race): no orders, no position record."""
    venues, gates, positions_path, journal_path = _wire(
        monkeypatch,
        tmp_path,
        binance_rate=0.06,  # recheck sees collapsed: 0.0675−0.06 = 0.0075% < 0.02 floor
        bybit_rate=0.0675,
        scan_bybit_rate=0.13,  # scanner (stale) still sees the healthy 0.12%
    )
    cfg = _live_like_cfg()

    out = runner.run_once(cfg)

    # the candidate passed the scanner funnel and reached the executor…
    assert out["candidates_after_filter"] == 1
    opens = [a for a in out["actions"] if a["action"] == "open"]
    assert len(opens) == 1
    # …but the re-check gate aborted it before any order was submitted
    assert opens[0]["result"]["state"] == "aborted"
    assert "funding re-check" in ";".join(opens[0]["result"].get("logs", []))
    assert len(gates["recheck_calls"]) == 1

    # nothing reached the venues, nothing was persisted
    assert venues["binance"].trades == [] and venues["bybit"].trades == []
    assert not positions_path.exists()
    assert out["open_positions"] == 0
