#!/usr/bin/env python3
"""Tests for the OKX venue adapter (mocked HTTP, no network).

Covers: spot/SWAP tickers (via faked http_get_json, incl. BTCUSDT →
BTC-USDT-SWAP instId conversion), SPOT + SWAP instrument rules (lotSz / minSz
/ ctVal / tickSz), the OKX funding provider (ANY batch endpoint, interval map
inferred from fundingTime gaps), dry-run execute_trades records, live order
bodies captured via a faked module-level `_api_call` (tgtCcy=quote_ccy +
tdMode=cash buys, isolated SWAP orders, 18<->27 transfers, fail-loud
balances) and the venue registry.

prepare_for_withdraw / transfer internals beyond transfer_asset are owned by
another test and intentionally not covered here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtest.funding_providers as fp_mod
import venues.okx as ox
from venues.okx import OkxSpotVenue

# ── canned public payloads (routed by URL substring) ──────────────────────────

_SPOT_TICKER = {"code": "0", "data": [{"last": "60000.5"}]}
_SWAP_TICKER = {"code": "0", "data": [{"last": "61000.25"}]}

_SPOT_INSTRUMENTS = {
    "code": "0",
    "data": [
        {
            "instId": "BTC-USDT",
            "state": "live",
            "lotSz": "0.00000001",
            "minSz": "0",
            "tickSz": "0.01",
        }
    ],
}

_SWAP_INSTRUMENTS = {
    "code": "0",
    "data": [
        {
            "instId": "BTC-USDT-SWAP",
            "state": "live",
            "lotSz": "0.01",
            "minSz": "0.01",
            "ctVal": "0.01",
            "tickSz": "0.1",
        }
    ],
}


def _reset_caches() -> None:
    ox._symbol_rules_cache = {}
    ox._futures_rules_cache = {}
    ox._initialized_symbols = set()
    ox._acct_config_cache = None
    ox._futures_ticker_loaded_at = 0.0
    ox._futures_ticker_prices = {}


def _fresh_venue() -> OkxSpotVenue:
    """Reset module-level caches so each test fakes its own HTTP responses."""
    _reset_caches()
    return OkxSpotVenue()


@pytest.fixture(autouse=True)
def _restore_clean_module_state():
    """Leave the venue module's caches empty after every test (no leakage)."""
    yield
    _reset_caches()


def _fake_http():
    def fake(url):
        if "NOPE" in url:
            return {"code": "0", "data": []}
        if "market/ticker" in url:
            return _SWAP_TICKER if "SWAP" in url else _SPOT_TICKER
        if "public/instruments" in url:
            if "instType=SPOT" in url:
                return _SPOT_INSTRUMENTS
            return _SWAP_INSTRUMENTS
        raise AssertionError(f"unexpected url {url}")

    return fake


class TestMarketData:
    def test_get_ticker_spot_price_and_failure(self):
        urls: list[str] = []

        def fake(url):
            urls.append(url)
            return _SPOT_TICKER

        with patch.object(ox, "http_get_json", fake):
            v = _fresh_venue()
            assert v.get_ticker("BTC-USDT") == 60000.5
        assert len(urls) == 1 and "instId=BTC-USDT" in urls[0]

        with patch.object(ox, "http_get_json", side_effect=RuntimeError("down")):
            assert _fresh_venue().get_ticker("BTC-USDT") == 0.0

    def test_get_futures_ticker_converts_instid(self):
        urls: list[str] = []

        def fake(url):
            urls.append(url)
            return _SWAP_TICKER

        with patch.object(ox, "http_get_json", fake):
            v = _fresh_venue()
            assert v.get_futures_ticker("BTCUSDT") == 61000.25
            # full instId passes through unchanged
            assert v.get_futures_ticker("BTC-USDT-SWAP") == 61000.25
        # BTCUSDT is translated to the OKX SWAP instId
        assert "instId=BTC-USDT-SWAP" in urls[0]

        with patch.object(ox, "http_get_json", side_effect=RuntimeError("down")):
            assert _fresh_venue().get_futures_ticker("BTCUSDT") == 0.0


class TestSymbolRules:
    def test_fetch_symbol_rules_spot(self):
        with patch.object(ox, "http_get_json", _fake_http()):
            v = _fresh_venue()
            rules = v.fetch_symbol_rules("BTC-USDT")
            assert v.fetch_symbol_rules("NOPE-USDT") is None
        assert rules is not None
        assert rules["symbol"] == "BTC-USDT"
        assert rules["quantity_precision"] == 8  # lotSz 1e-8
        assert rules["min_trade_base"] == 1e-8  # max(minSz=0, lotSz)
        assert rules["min_trade_usdt"] == 0
        assert rules["quote_precision"] == 2  # tickSz 0.01
        assert rules["status"] == "live"

    def test_fetch_futures_symbol_rules_swap(self):
        urls: list[str] = []

        def fake(url):
            urls.append(url)
            if "NOPE" in url:
                return {"code": "0", "data": []}
            return _SWAP_INSTRUMENTS

        with patch.object(ox, "http_get_json", fake):
            v = _fresh_venue()
            rules = v.fetch_futures_symbol_rules("BTCUSDT")
            assert v.fetch_futures_symbol_rules("NOPEUSDT") is None
        assert rules is not None
        assert rules["symbol"] == "BTCUSDT"
        assert rules["quantity_precision"] == 2  # lotSz 0.01
        assert rules["min_trade_base"] == 0.01  # max(minSz*ctVal, lotSz)
        assert rules["ct_val"] == 0.01
        assert rules["quote_precision"] == 1  # tickSz 0.1
        assert rules["status"] == "live"
        assert any("instId=BTC-USDT-SWAP" in u for u in urls)


class TestFundingProvider:
    """backtest.funding_providers.OkxFundingProvider with canned JSON."""

    _ANY = {
        "code": "0",
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "fundingRate": "0.0001",
                "fundingTime": "1700000000000",
                "nextFundingTime": "1700014400000",  # +4h
            },
            {
                "instId": "ETH-USDT-SWAP",
                "fundingRate": "-0.0002",
                "fundingTime": "1700000000000",
                "nextFundingTime": "1700028800000",  # +8h
            },
            {
                "instId": "BTC-USD-SWAP",  # non-USDT quote filtered out
                "fundingRate": "0.0003",
            },
        ],
    }
    _MARKS = {
        "code": "0",
        "data": [
            {"instId": "BTC-USDT-SWAP", "markPx": "60000", "idxPx": "59990"},
            {"instId": "ETH-USDT-SWAP", "markPx": "3000", "idxPx": "2999"},
        ],
    }
    _CURRENT = {
        "code": "0",
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "fundingRate": "0.0001",
                "nextFundingTime": "1700006400000",
                "fundingInterval": "8",
            }
        ],
    }

    def _fake_http(self, any_rows=None):
        def fake(url, max_retries=5):
            if "funding-rate?instId=ANY" in url:
                return any_rows or self._ANY
            if "mark-price?instType=SWAP" in url and "instId=" not in url:
                return self._MARKS
            if "funding-rate?instId=" in url:
                return self._CURRENT
            if "mark-price?instId=" in url:
                return {
                    "code": "0",
                    "data": [{"instId": "BTC-USDT-SWAP", "markPx": "60000", "idxPx": "59990"}],
                }
            raise AssertionError(f"unexpected url {url}")

        return fake

    def test_fetch_all_maps_rows(self):
        with patch.object(fp_mod, "_http_get_with_retry", self._fake_http()):
            rows = fp_mod.OkxFundingProvider().fetch_all("USDT")
        assert {r["symbol"] for r in rows} == {"BTCUSDT", "ETHUSDT"}
        btc = next(r for r in rows if r["symbol"] == "BTCUSDT")
        assert btc["rate_pct"] == 0.01
        assert btc["next_funding_ts"] == 1700000000000  # fundingTime
        assert btc["mark_price"] == 60000.0  # joined from mark-price batch
        assert btc["index_price"] == 59990.0

    def test_fetch_interval_map_inferred_from_funding_time(self):
        with patch.object(fp_mod, "_http_get_with_retry", self._fake_http()):
            imap = fp_mod.OkxFundingProvider().fetch_interval_map("USDT")
        assert imap == {"BTCUSDT": 4.0, "ETHUSDT": 8.0}  # (next - cur) / 3600e3

    def test_fetch_current_single_symbol(self):
        with patch.object(fp_mod, "_http_get_with_retry", self._fake_http()):
            row = fp_mod.OkxFundingProvider().fetch_current("BTCUSDT")
        assert row["rate_pct"] == 0.01
        assert row["interval_ms"] == 8 * 60 * 60 * 1000  # fundingInterval "8"
        assert row["next_funding_ts"] == 1700006400000
        assert row["last_settle_ts"] == 1700006400000 - 8 * 60 * 60 * 1000
        assert row["mark_price"] == 60000.0
        assert row["index_price"] == 59990.0


class TestExecutionDryRun:
    def test_dry_run_normalized_records_no_http(self):
        trades = [
            {"symbol": "BTC", "type": "buy", "amount_usdt": 600.0},
            {"symbol": "BTC", "type": "sell", "amount_base": 0.01},
            {"symbol": "BTC", "type": "open_long", "amount_base": 0.01},
            {"symbol": "BTC", "type": "open_short", "amount_base": 0.01},
            {"symbol": "BTC", "type": "close_long", "amount_base": 0.01},
            {"symbol": "BTC", "type": "close_short", "amount_base": 0.01},
        ]
        market = {"BTC": {"price": 60000.0, "pair": "BTC-USDT"}}
        v = _fresh_venue()
        results = v.execute_trades(trades, market, dry_run=True)
        assert len(results) == 6
        for r in results:
            assert r["status"] == "simulated"
            assert r["venue"] == "okx"
            assert r["dry_run"] is True
            assert r["order_id"] is None
            assert r["slippage"] == 0.0
            assert r["latency_ms"] == 0
            assert r["ref_price"] == 60000.0


class TestExecutionLive:
    """Live order bodies via a capturing fake `_api_call`."""

    @staticmethod
    def _router(calls):
        def fake(method, path, params=None, body=None):
            calls.append((method, path, dict(params or {}), dict(body or {})))
            if path == "/api/v5/trade/order" and method == "POST":
                return {"code": "0", "data": [{"ordId": "999"}]}
            if path == "/api/v5/trade/order" and method == "GET":
                return {
                    "code": "0",
                    "data": [
                        {
                            "avgPx": "60100",
                            "fillSz": "0.01",
                            # accFillSz is in the QUOTE ccy for a spot market
                            # buy with tgtCcy=quote_ccy (fillCxqFee/fillSzQuote
                            # are not real OKX v5 fields).
                            "accFillSz": "601",
                            "state": "filled",
                        }
                    ],
                }
            if path in (
                "/api/v5/account/set-position-mode",
                "/api/v5/account/set-leverage",
            ):
                return {"code": "0", "data": []}
            raise AssertionError(f"unexpected api call {method} {path}")

        return fake

    def test_spot_buy_tgt_ccy_quote(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)  # skip 0.3s fill wait
        calls: list[tuple] = []
        with patch.object(ox, "_api_call", self._router(calls)):
            v = _fresh_venue()
            res = v.execute_trades(
                [{"symbol": "BTC", "type": "buy", "amount_usdt": 600.0}],
                {"BTC": {"price": 60000.0, "pair": "BTC-USDT", "quote_precision": 2}},
                dry_run=False,
            )
        r = res[0]
        assert r["status"] == "filled"
        assert r["order_id"] == "999"
        assert r["order_status"] == "filled"
        assert abs(r["exec_price"] - 60100.0) < 1e-9  # avgPx from order detail
        assert abs(r["exec_qty"] - 0.01) < 1e-9  # accFillSz(quote) / avgPx
        assert abs(r["exec_quote_usd"] - 601.0) < 1e-9
        assert abs(r["slippage"] - round((60100.0 - 60000.0) / 60000.0, 6)) < 1e-9
        post = next(
            c for c in calls if c[:2] == ("POST", "/api/v5/trade/order")
        )
        body = post[3]
        assert body["instId"] == "BTC-USDT"
        assert body["tdMode"] == "cash"
        assert body["side"] == "buy"
        assert body["ordType"] == "market"
        assert body["tgtCcy"] == "quote_ccy"  # quote-sized market buy
        assert body["sz"] == "600.00"
        assert body["clOrdId"].startswith("qbuy")
        detail = next(
            c for c in calls if c[:2] == ("GET", "/api/v5/trade/order")
        )
        assert detail[2] == {"instId": "BTC-USDT", "ordId": "999"}

    def test_spot_sell_base_sz(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        calls: list[tuple] = []
        with patch.object(ox, "_api_call", self._router(calls)):
            v = _fresh_venue()
            res = v.execute_trades(
                [{"symbol": "BTC", "type": "sell", "amount_base": 0.01}],
                {"BTC": {"price": 60000.0, "pair": "BTC-USDT", "quantity_precision": 6}},
                dry_run=False,
            )
        assert res[0]["status"] == "filled"
        post = next(c for c in calls if c[:2] == ("POST", "/api/v5/trade/order"))
        body = post[3]
        assert body["side"] == "sell"
        assert body["sz"] == "0.01"  # base-sized market sell
        assert "tgtCcy" not in body

    def test_perp_swap_side_mapping(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)  # skip 0.5s fill wait
        expected = {
            "open_long": "buy",
            "open_short": "sell",
            "close_long": "sell",
            "close_short": "buy",
        }
        for typ, side in expected.items():
            calls: list[tuple] = []
            with patch.object(ox, "_api_call", self._router(calls)):
                v = _fresh_venue()
                res = v.execute_trades(
                    [
                        {
                            "symbol": "BTC",
                            "type": typ,
                            "amount_base": 0.01,
                            "quantity_precision": 3,
                        }
                    ],
                    {"BTC": {"price": 60000.0, "pair": "BTCUSDT"}},
                    dry_run=False,
                )
            r = res[0]
            assert r["status"] == "filled", typ
            assert r["order_id"] == "999", typ
            assert abs(r["exec_price"] - 60100.0) < 1e-9, typ  # detail avgPx
            assert abs(r["exec_qty"] - 0.01) < 1e-9, typ
            assert abs(r["slippage"] - round((60100.0 - 60000.0) / 60000.0, 6)) < 1e-9, typ
            post = next(c for c in calls if c[:2] == ("POST", "/api/v5/trade/order"))
            body = post[3]
            assert body["instId"] == "BTC-USDT-SWAP", typ
            assert body["tdMode"] == "isolated", typ
            assert body["side"] == side, typ
            assert body["ordType"] == "market", typ
            assert body["sz"] == "0.01", typ
            if typ in ("close_long", "close_short"):
                # Reduce-only on closes: oversized close clamps at zero instead
                # of flipping position direction (was missing before hardening).
                assert body.get("reduceOnly") is True, typ
            else:
                assert "reduceOnly" not in body, typ
            detail = next(c for c in calls if c[:2] == ("GET", "/api/v5/trade/order"))
            assert detail[2] == {"instId": "BTC-USDT-SWAP", "ordId": "999"}, typ
            # symbol initialization happens before the order (net mode + 1x)
            init_paths = {c[1] for c in calls if c[0] == "POST"}
            assert "/api/v5/account/set-leverage" in init_paths, typ

    def test_order_error_fails_the_record(self):
        def fake(method, path, params=None, body=None):
            raise RuntimeError("OKX API error: insufficient balance")

        with patch.object(ox, "_api_call", fake):
            v = _fresh_venue()
            res = v.execute_trades(
                [{"symbol": "BTC", "type": "buy", "amount_usdt": 600.0}],
                {"BTC": {"price": 60000.0, "pair": "BTC-USDT", "quote_precision": 2}},
                dry_run=False,
            )
        assert res[0]["status"] == "failed"
        assert res[0]["order_id"] is None
        assert "insufficient balance" in res[0]["error"]

    def test_unknown_trade_type_fails(self):
        v = _fresh_venue()
        res = v.execute_trades(
            [{"symbol": "BTC", "type": "hodl", "amount_base": 1}],
            {"BTC": {"price": 1.0}},
            dry_run=False,
        )
        assert res[0]["status"] == "failed"
        assert "Unknown trade type" in res[0]["error"]


class TestTransfer:
    def test_transfer_asset_body_and_direction(self):
        calls: list[tuple] = []

        def fake(method, path, params=None, body=None):
            calls.append((method, path, dict(params or {}), dict(body or {})))
            return {"code": "0", "data": []}

        with patch.object(ox, "_api_call", fake):
            v = _fresh_venue()
            assert v.transfer_asset("USDT", 10.5, "spot", "futures") is True
            assert v.transfer_asset("USDT", 10.5, "futures", "spot") is True
            assert v.transfer_asset("USDT", 10.5, "earn", "spot") is False
        assert len(calls) == 2
        for method, path, _params, body in calls:
            assert (method, path) == ("POST", "/api/v5/asset/transfer")
            assert body["currency"] == "USDT"
            assert body["amount"] == "10.5"
        assert calls[0][3]["from"] == "18"  # spot (trading account)
        assert calls[0][3]["to"] == "27"  # futures
        assert calls[1][3]["from"] == "27"
        assert calls[1][3]["to"] == "18"
        # margin lives in the trading account: no transfer API call at all
        calls.clear()
        with patch.object(ox, "_api_call", fake):
            v = _fresh_venue()
            assert v.transfer_asset("USDT", 10.5, "spot", "margin") is True
        assert calls == []
        with patch.object(ox, "_api_call", side_effect=RuntimeError("api down")):
            assert _fresh_venue().transfer_asset("USDT", 1, "spot", "futures") is False


class TestBalancesAndPositions:
    @staticmethod
    def _router(error=False):
        def fake(method, path, params=None, body=None):
            if path == "/api/v5/account/balance":
                if error:
                    raise RuntimeError("balance down")
                return {
                    "code": "0",
                    "data": [
                        {
                            "details": [
                                {"ccy": "USDT", "availBal": "100"},
                                {"ccy": "BTC", "cashBal": "0.5"},
                                {"ccy": "ETH", "cashBal": "2.0"},
                            ]
                        }
                    ],
                }
            if path == "/api/v5/account/positions":
                return {
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "pos": "2",
                            "avgPx": "60000",
                            "liqPx": "30000",
                            "lever": "1",
                            "upl": "5",
                        },
                        {
                            "instId": "ETH-USDT-SWAP",
                            "pos": "-3",
                            "avgPx": "3000",
                            "liqPx": "6000",
                            "lever": "2",
                            "upl": "-1",
                        },
                        {"instId": "BTC-USDT", "pos": "1"},  # spot filtered
                        {"instId": "SOL-USDT-SWAP", "pos": "0"},  # flat filtered
                    ],
                }
            raise AssertionError(f"unexpected api call {method} {path}")

        return fake

    def test_fetch_balances_parses_trading_account(self):
        with patch.object(ox, "_api_call", self._router()):
            v = _fresh_venue()
            bal = v.fetch_balances(["USDT", "BTC"])
        assert bal == {"USDT": 100.0, "BTC": 0.5}

    def test_fetch_balances_fail_loud_on_error(self):
        # No try/except in okx's fetch_balances: errors propagate (fail-loud).
        with patch.object(ox, "_api_call", self._router(error=True)):
            v = _fresh_venue()
            try:
                v.fetch_balances(["USDT"])
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert "balance down" in str(e)

    def test_fetch_futures_positions_parse(self):
        with patch.object(ox, "_api_call", self._router()):
            positions = _fresh_venue().fetch_futures_positions()
        assert len(positions) == 2
        btc = next(p for p in positions if p["symbol"] == "BTCUSDT")
        assert btc["side"] == "long" and btc["qty"] == 2.0
        assert btc["entry_price"] == 60000.0
        assert btc["liq_price"] == 30000.0
        eth = next(p for p in positions if p["symbol"] == "ETHUSDT")
        assert eth["side"] == "short" and eth["qty"] == 3.0


class TestRegistration:
    def test_get_venue_registry(self):
        from venues import get_venue, supported_venues

        assert "okx" in supported_venues()
        v = get_venue({"venue": {"type": "okx"}})
        assert v.venue_id == "okx"
        assert isinstance(v, OkxSpotVenue)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
