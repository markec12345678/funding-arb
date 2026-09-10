#!/usr/bin/env python3
"""Tests for the pure-futures manual trade CLI (hermetic, no network).

Drives cli/pure_futures_trade.py's ``main()`` with a patched ``sys.argv``.
The CLI does not expose a positions-path or venue-injection parameter, and
monkeypatching ``POSITIONS_PATH`` module attributes would have no effect (the
executor binds that default at def time), so the executor functions imported
into the CLI namespace are wrapped with ``functools.partial`` to inject
FakeFuturesVenue legs + a tmp_path ledger. All executor logic still runs for
real; only venue construction and the ledger location are redirected.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cli.pure_futures_trade as cli_mod
import execution.pure_futures_executor as executor_mod


class FakeFuturesVenue:
    """Minimal futures venue double (mirrors test_pure_futures_executor)."""

    def __init__(
        self,
        venue_id: str,
        price: float = 100.0,
        fail_types: set[str] | None = None,
    ):
        self.venue_id = venue_id
        self.price = price
        self.fail_types = fail_types or set()
        self.trades: list[dict] = []
        self.initialized: list[str] = []
        self.transfers: list[tuple] = []
        self.balances = {"spot": 100000.0, "futures": 100000.0}

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


def _run_cli(argv: list[str], monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["pure_futures_trade.py"] + argv)
    return cli_mod.main()


def _inject(monkeypatch, path: Path, lv, sv) -> None:
    """Redirect the CLI's executor bindings to tmp ledger + fake venues."""
    monkeypatch.setattr(
        cli_mod,
        "open_pure_futures_pair",
        functools.partial(
            executor_mod.open_pure_futures_pair,
            long_venue=lv,
            short_venue=sv,
            positions_path=path,
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "close_pure_futures_pair",
        functools.partial(
            executor_mod.close_pure_futures_pair,
            long_venue=lv,
            short_venue=sv,
            positions_path=path,
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "load_pure_futures_positions",
        functools.partial(executor_mod.load_pure_futures_positions, path),
    )


def _open_dry_run(path: Path, monkeypatch, capsys, trade_usd="500") -> str:
    lv, sv = FakeFuturesVenue("okx"), FakeFuturesVenue("bybit")
    _inject(monkeypatch, path, lv, sv)
    rc = _run_cli(
        [
            "open",
            "BTC",
            "--long-venue",
            "okx",
            "--short-venue",
            "bybit",
            "--trade-usd",
            trade_usd,
            "--dry-run",
        ],
        monkeypatch,
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["state"] == "simulated"
    return out["position_id"]


class TestOpenCommand:
    def test_open_dry_run_creates_open_record(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "positions.json"
        lv, sv = FakeFuturesVenue("okx"), FakeFuturesVenue("bybit")
        _inject(monkeypatch, path, lv, sv)
        rc = _run_cli(
            [
                "open",
                "BTC",
                "--long-venue",
                "okx",
                "--short-venue",
                "bybit",
                "--trade-usd",
                "500",
                "--dry-run",
            ],
            monkeypatch,
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["state"] == "simulated"
        assert out["position_id"].startswith("pf-BTC-okx-bybit-")

        rows = executor_mod.load_pure_futures_positions(path)
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "open"
        assert row["dry_run"] is True
        assert row["base"] == "BTC"
        assert row["long_venue"] == "okx"
        assert row["short_venue"] == "bybit"
        assert row["trade_usd"] == 500.0
        assert row["qty"] == 5.0  # 500 / 100, floored to 4 decimals
        # one simulated leg per venue
        assert [t["type"] for t in lv.trades] == ["open_long"]
        assert [t["type"] for t in sv.trades] == ["open_short"]
        assert lv.trades[0]["dry_run"] is True

    def test_open_reverse_direction_metadata(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "positions.json"
        lv, sv = FakeFuturesVenue("okx"), FakeFuturesVenue("bybit")
        _inject(monkeypatch, path, lv, sv)
        rc = _run_cli(
            [
                "open",
                "BTC",
                "--long-venue",
                "okx",
                "--short-venue",
                "bybit",
                "--trade-usd",
                "500",
                "--direction",
                "reverse",
                "--dry-run",
            ],
            monkeypatch,
        )
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True
        rows = executor_mod.load_pure_futures_positions(path)
        assert rows[0]["direction"] == "reverse"

    def test_tiny_trade_usd_aborts_quantity_floored(
        self, tmp_path, monkeypatch, capsys
    ):
        path = tmp_path / "positions.json"
        # 1 USD / 60000 price floors to 0 at 4-decimal precision
        lv = FakeFuturesVenue("okx", price=60000.0)
        sv = FakeFuturesVenue("bybit", price=60000.0)
        _inject(monkeypatch, path, lv, sv)
        rc = _run_cli(
            [
                "open",
                "BTC",
                "--long-venue",
                "okx",
                "--short-venue",
                "bybit",
                "--trade-usd",
                "1",
                "--dry-run",
            ],
            monkeypatch,
        )
        assert rc == 2  # failure exit code
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False and out["state"] == "aborted"
        assert any("Quantity floored to 0" in log for log in out["logs"])
        # nothing recorded and no orders simulated
        assert executor_mod.load_pure_futures_positions(path) == []
        assert lv.trades == [] and sv.trades == []

    def test_unknown_venue_id_clean_error(self, tmp_path, monkeypatch):
        # No injection: the real executor resolves venues via get_venue and
        # fails fast (before any HTTP) on an unknown venue id.
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "pure_futures_trade.py",
                "open",
                "BTC",
                "--long-venue",
                "nope",
                "--short-venue",
                "bybit",
                "--trade-usd",
                "500",
                "--dry-run",
            ],
        )
        try:
            cli_mod.main()
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "Unsupported exchange venue.type" in str(e)
            assert "nope" in str(e)

    def test_live_and_dry_run_are_mutually_exclusive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "pure_futures_trade.py",
                "open",
                "BTC",
                "--long-venue",
                "okx",
                "--short-venue",
                "bybit",
                "--trade-usd",
                "500",
                "--live",
                "--dry-run",
            ],
        )
        try:
            cli_mod.main()
            raise AssertionError("expected SystemExit")
        except SystemExit as e:
            assert e.code == 2  # argparse usage error

    def test_open_short_leg_failure_rolls_back(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "positions.json"
        lv = FakeFuturesVenue("okx")
        sv = FakeFuturesVenue("bybit", fail_types={"open_short"})
        _inject(monkeypatch, path, lv, sv)
        rc = _run_cli(
            [
                "open",
                "BTC",
                "--long-venue",
                "okx",
                "--short-venue",
                "bybit",
                "--trade-usd",
                "500",
                "--live",
            ],
            monkeypatch,
        )
        assert rc == 2
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False and out["state"] == "rolled_back"
        # long leg opened then rolled back; no open record remains
        assert [t["type"] for t in lv.trades] == ["open_long", "close_long"]
        assert executor_mod.load_pure_futures_positions(path) == []


class TestListCommand:
    def test_list_shows_open_position(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "positions.json"
        pid = _open_dry_run(path, monkeypatch, capsys)
        rc = _run_cli(["list"], monkeypatch)
        assert rc == 0
        out = capsys.readouterr().out
        assert pid in out
        assert "open" in out and "BTC" in out
        assert "long@okx" in out and "short@bybit" in out
        assert "dry=True" in out

        rc = _run_cli(["list", "--json"], monkeypatch)
        assert rc == 0
        rows = json.loads(capsys.readouterr().out)
        assert len(rows) == 1 and rows[0]["id"] == pid

    def test_list_empty_prints_placeholder(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "positions.json"
        lv, sv = FakeFuturesVenue("okx"), FakeFuturesVenue("bybit")
        _inject(monkeypatch, path, lv, sv)
        rc = _run_cli(["list"], monkeypatch)
        assert rc == 0
        assert "No positions." in capsys.readouterr().out

    def test_list_all_includes_closed(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "positions.json"
        pid = _open_dry_run(path, monkeypatch, capsys)
        # default list hides nothing here (single open row)
        rc = _run_cli(["list", "--all", "--json"], monkeypatch)
        assert rc == 0
        rows = json.loads(capsys.readouterr().out)
        assert [r["id"] for r in rows] == [pid]
        assert rows[0]["status"] == "open"


class TestCloseCommand:
    def test_close_dry_run_marks_closed(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "positions.json"
        pid = _open_dry_run(path, monkeypatch, capsys)
        capsys.readouterr()
        rc = _run_cli(["close", pid, "--dry-run"], monkeypatch)
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True and out["state"] == "simulated"
        rows = executor_mod.load_pure_futures_positions(path)
        assert rows[0]["status"] == "closed"
        assert rows[0]["close_info"]["dry_run"] is True
        assert "closed_at" in rows[0]

    def test_close_without_flags_defaults_to_position_mode(
        self, tmp_path, monkeypatch, capsys
    ):
        path = tmp_path / "positions.json"
        pid = _open_dry_run(path, monkeypatch, capsys)
        capsys.readouterr()
        # no --dry-run / --live: dry_run falls back to the record's flag
        rc = _run_cli(["close", pid], monkeypatch)
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["state"] == "simulated"
        assert executor_mod.load_pure_futures_positions(path)[0]["status"] == "closed"

    def test_close_unknown_position_aborts(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "positions.json"
        lv, sv = FakeFuturesVenue("okx"), FakeFuturesVenue("bybit")
        _inject(monkeypatch, path, lv, sv)
        rc = _run_cli(["close", "pf-BTC-okx-bybit-0-deadbe", "--dry-run"], monkeypatch)
        assert rc == 2
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False and out["state"] == "aborted"
        assert "not found" in out["logs"][0]


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
