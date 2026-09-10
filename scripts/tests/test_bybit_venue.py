#!/usr/bin/env python3
"""Tests for the Bybit venue adapter (mocked HTTP, no network).

Covers: spot/linear tickers (via faked http_get_json), spot + linear
instrument rules, the Bybit funding provider, dry-run execute_trades records,
live order bodies captured via a faked module-level `_api_call`
(marketUnit=quoteCoin buys, Buy/Sell perp side mapping, isLeverage-free
plain spot/futures orders, inter-transfer body, fail-loud balances) and the
venue registry.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtest.funding_providers as fp_mod
import venues.bybit as bb
from venues.bybit import BybitSpotVenue

# ── canned public payloads (routed by URL substring) ──────────────────────────

_SPOT_TICKER = {
    "retCode": 0,
    "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "60000.5"}]},
}
_LINEAR_TICKER = {
    "retCode": 0,
    "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "61000.25"}]},
}

_SPOT_INSTRUMENT = {
    "retCode": 0,
    "result": {
        "list": [
            {
                "symbol": "BTCUSDT",
                "status": "Trading",
                "lotSizeFilter": {"minOrderQty": "0.001", "basePrecision": "0.000001"},
                "minNotionalFilter": {"minNotionalValue": "1"},
            }
        ]
    },
}

_LINEAR_INSTRUMENT = {
    "retCode": 0,
    "result": {
        "list": [
            {
                "symbol": "BTCUSDT",
                "status": "Trading",
                "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.002"},
            }
        ]
    },
}


def _reset_caches() -> None:
    bb._symbol_rules_cache = {}
    bb._futures_rules_cache = {}
    bb._spot_ticker_loaded_at = 0.0
    bb._spot_ticker_prices = {}
    bb._futures_ticker_loaded_at = 0.0
    bb._futures_ticker_prices = {}
    bb._initialized_symbols = set()


def _fresh_venue() -> BybitSpotVenue:
    """Reset module-level caches so each test fakes its own HTTP responses."""
    _reset_caches()
    return BybitSpotVenue()


@pytest.fixture(autouse=True)
def _restore_clean_module_state():
    """Leave the venue module's caches empty after every test (no leakage)."""
    yield
    _reset_caches()


def _fake_http(spot_ticker=None, linear_ticker=None, spot_inst=None, linear_inst=None):
    def fake(url):
        if "category=spot" in url and "tickers" in url:
            return spot_ticker or _SPOT_TICKER
        if "category=linear" in url and "tickers" in url:
            return linear_ticker or _LINEAR_TICKER
        if "category=spot" in url and "instruments-info" in url:
            if "symbol=NOPEUSDT" in url:
                return {"retCode": 0, "result": {"list": []}}
            return spot_inst or _SPOT_INSTRUMENT
        if "category=linear" in url and "instruments-info" in url:
            if "symbol=NOPEUSDT" in url:
                return {"retCode": 0, "result": {"list": []}}
            return linear_inst or _LINEAR_INSTRUMENT
        raise AssertionError(f"unexpected url {url}")

    return fake


class TestMarketData:
    def test_get_ticker_spot_price_and_failure(self):
        urls: list[str] = []

        def fake(url):
            urls.append(url)
            return _SPOT_TICKER

        with patch.object(bb, "http_get_json", fake):
            v = _fresh_venue()
            assert v.get_ticker("BTCUSDT") == 60000.5
        assert len(urls) == 1
        assert "category=spot" in urls[0] and "BTCUSDT" in urls[0]

        with patch.object(bb, "http_get_json", side_effect=RuntimeError("down")):
            assert _fresh_venue().get_ticker("BTCUSDT") == 0.0

    def test_get_futures_ticker_linear_price(self):
        urls: list[str] = []

        def fake(url):
            urls.append(url)
            return _LINEAR_TICKER

        with patch.object(bb, "http_get_json", fake):
            v = _fresh_venue()
            assert v.get_futures_ticker("BTCUSDT") == 61000.25
        assert "category=linear" in urls[0]

        with patch.object(bb, "http_get_json", side_effect=RuntimeError("down")):
            assert _fresh_venue().get_futures_ticker("BTCUSDT") == 0.0


class TestSymbolRules:
    def test_fetch_symbol_rules_spot(self):
        with patch.object(bb, "http_get_json", _fake_http()):
            v = _fresh_venue()
            rules = v.fetch_symbol_rules("BTCUSDT")
        assert rules is not None
        assert rules["symbol"] == "BTCUSDT"
        assert rules["min_trade_base"] == 0.001  # minOrderQty
        assert rules["min_trade_usdt"] == 1.0  # minNotionalValue
        assert rules["quantity_precision"] == 6  # basePrecision 0.000001
        assert rules["quote_precision"] == 2
        assert rules["status"] == "Trading"

    def test_fetch_symbol_rules_unknown_pair(self):
        empty = {"retCode": 0, "result": {"list": []}}
        with patch.object(bb, "http_get_json", _fake_http(spot_inst=empty)):
            v = _fresh_venue()
            assert v.fetch_symbol_rules("NOPEUSDT") is None

    def test_fetch_futures_symbol_rules_linear(self):
        with patch.object(bb, "http_get_json", _fake_http()):
            v = _fresh_venue()
            rules = v.fetch_futures_symbol_rules("BTCUSDT")
            assert v.fetch_futures_symbol_rules("NOPEUSDT") is None
        assert rules is not None
        assert rules["symbol"] == "BTCUSDT"
        assert rules["quantity_precision"] == 3  # qtyStep 0.001
        assert rules["min_trade_base"] == 0.002  # minOrderQty
        assert rules["min_trade_usdt"] == 0
        assert rules["quote_precision"] == 2
        assert rules["status"] == "Trading"


class TestFundingProvider:
    """backtest.funding_providers.BybitFundingProvider with canned JSON."""

    _TICKERS = {
        "retCode": 0,
        "result": {
            "list": [
                {
                    "symbol": "BTCUSDT",
                    "fundingRate": "0.0001",
                    "nextFundingTime": "1700006400000",
                    "markPrice": "60000",
                    "indexPrice": "59990",
                },
                {
                    "symbol": "BTCUSDC",  # non-USDT quote filtered out
                    "fundingRate": "0.0002",
                },
            ]
        },
    }

    def _fake_http(self, tickers=None):
        def fake(url, max_retries=5):
            if "market/tickers" in url:
                return tickers or self._TICKERS
            raise AssertionError(f"unexpected url {url}")

        return fake

    def test_fetch_all_maps_rows(self):
        with patch.object(fp_mod, "_http_get_with_retry", self._fake_http()):
            rows = fp_mod.BybitFundingProvider().fetch_all("USDT")
        assert {r["symbol"] for r in rows} == {"BTCUSDT"}
        btc = rows[0]
        assert btc["rate_pct"] == 0.01
        assert btc["next_funding_ts"] == 1700006400000
        assert btc["mark_price"] == 60000.0
        assert btc["index_price"] == 59990.0

    def test_fetch_current_uses_8h_default(self):
        with patch.object(fp_mod, "_http_get_with_retry", self._fake_http()):
            row = fp_mod.BybitFundingProvider().fetch_current("BTCUSDT")
        assert row["rate_pct"] == 0.01
        assert row["interval_ms"] == 8 * 60 * 60 * 1000  # bybit: fixed 8h
        assert row["next_funding_ts"] == 1700006400000
        assert row["last_settle_ts"] == 1700006400000 - 8 * 60 * 60 * 1000

    def test_fetch_interval_map_is_default_empty(self):
        # Bybit exposes no override map; consumers default to the 8h interval.
        assert fp_mod.BybitFundingProvider().fetch_interval_map() == {}


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
        market = {"BTC": {"price": 60000.0, "pair": "BTCUSDT"}}
        v = _fresh_venue()
        results = v.execute_trades(trades, market, dry_run=True)
        assert len(results) == 6
        for r in results:
            assert r["status"] == "simulated"
            assert r["venue"] == "bybit"
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
            if path == "/v5/order/create" and method == "POST":
                return {"retCode": 0, "result": {"orderId": "555"}}
            if path == "/v5/order/realtime":
                return {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "avgPrice": "60100",
                                "cumExecQty": "0.01",
                                "cumExecValue": "601",
                                "orderStatus": "Filled",
                            }
                        ]
                    },
                }
            if path in (
                "/v5/position/switch-mode",
                "/v5/account/set-leverage",
                "/v5/account/set-margin-mode",
            ):
                return {"retCode": 0, "result": {}}
            raise AssertionError(f"unexpected api call {method} {path}")

        return fake

    def test_spot_buy_uses_market_unit_quote(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)  # skip 0.3s fill wait
        calls: list[tuple] = []
        with patch.object(bb, "_api_call", self._router(calls)):
            v = _fresh_venue()
            res = v.execute_trades(
                [{"symbol": "BTC", "type": "buy", "amount_usdt": 600.0}],
                {"BTC": {"price": 60000.0, "pair": "BTCUSDT", "quote_precision": 2}},
                dry_run=False,
            )
        r = res[0]
        assert r["status"] == "filled"
        assert r["order_id"] == "555"
        assert r["order_status"] == "Filled"
        assert abs(r["exec_price"] - 60100.0) < 1e-9  # avgPrice from realtime
        assert abs(r["exec_qty"] - 0.01) < 1e-9  # cumExecQty
        assert abs(r["exec_quote_usd"] - 601.0) < 1e-9  # cumExecValue
        assert abs(r["slippage"] - round((60100.0 - 60000.0) / 60000.0, 6)) < 1e-9
        post = next(c for c in calls if c[:2] == ("POST", "/v5/order/create"))
        body = post[3]
        assert body["category"] == "spot"
        assert body["symbol"] == "BTCUSDT"
        assert body["side"] == "Buy"
        assert body["orderType"] == "Market"
        assert body["marketUnit"] == "quoteCoin"  # quote-sized market buy
        assert body["qty"] == "600.00"
        assert body["orderLinkId"].startswith("qbuy")
        detail = next(c for c in calls if c[:2] == ("GET", "/v5/order/realtime"))
        assert detail[2] == {"category": "spot", "orderId": "555"}

    def test_spot_sell_uses_base_qty(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        calls: list[tuple] = []
        with patch.object(bb, "_api_call", self._router(calls)):
            v = _fresh_venue()
            res = v.execute_trades(
                [{"symbol": "BTC", "type": "sell", "amount_base": 0.01}],
                {"BTC": {"price": 60000.0, "pair": "BTCUSDT", "quantity_precision": 6}},
                dry_run=False,
            )
        assert res[0]["status"] == "filled"
        post = next(c for c in calls if c[:2] == ("POST", "/v5/order/create"))
        body = post[3]
        assert body["side"] == "Sell"
        assert body["qty"] == "0.01"  # base-sized market sell
        assert "marketUnit" not in body

    def test_perp_side_mapping(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)  # skip 0.5s fill wait
        expected = {
            "open_long": "Buy",
            "open_short": "Sell",
            "close_long": "Sell",
            "close_short": "Buy",
        }
        for typ, side in expected.items():
            calls: list[tuple] = []
            with patch.object(bb, "_api_call", self._router(calls)):
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
            assert r["order_id"] == "555", typ
            assert abs(r["exec_price"] - 60100.0) < 1e-9, typ  # realtime avgPrice
            assert abs(r["exec_qty"] - 0.01) < 1e-9, typ
            assert abs(r["slippage"] - round((60100.0 - 60000.0) / 60000.0, 6)) < 1e-9, typ
            post = next(c for c in calls if c[:2] == ("POST", "/v5/order/create"))
            body = post[3]
            assert body["category"] == "linear", typ
            assert body["symbol"] == "BTCUSDT", typ
            assert body["side"] == side, typ
            assert body["orderType"] == "Market", typ
            assert body["qty"] == "0.01", typ
            if typ in ("close_long", "close_short"):
                # Reduce-only on closes: oversized close clamps at zero instead
                # of flipping position direction (was missing before hardening).
                assert body.get("reduceOnly") is True, typ
            else:
                assert "reduceOnly" not in body, typ
            # symbol initialization happens before the order
            init_paths = {c[1] for c in calls if c[0] == "POST"}
            assert "/v5/account/set-leverage" in init_paths, typ
            detail = next(c for c in calls if c[:2] == ("GET", "/v5/order/realtime"))
            assert detail[2] == {"category": "linear", "orderId": "555"}, typ

    def test_order_error_fails_the_record(self):
        def fake(method, path, params=None, body=None):
            raise RuntimeError("Bybit API error: insufficient balance")

        with patch.object(bb, "_api_call", fake):
            v = _fresh_venue()
            res = v.execute_trades(
                [{"symbol": "BTC", "type": "buy", "amount_usdt": 600.0}],
                {"BTC": {"price": 60000.0, "pair": "BTCUSDT", "quote_precision": 2}},
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
    def test_transfer_asset_body(self):
        calls: list[tuple] = []

        def fake(method, path, params=None, body=None):
            calls.append((method, path, dict(params or {}), dict(body or {})))
            return {"retCode": 0, "result": {}}

        with patch.object(bb, "_api_call", fake):
            v = _fresh_venue()
            assert v.transfer_asset("USDT", 10.5, "spot", "futures") is True
            assert v.transfer_asset("USDT", 10.5, "futures", "spot") is True
            assert v.transfer_asset("USDT", 10.5, "margin", "spot") is False
        assert len(calls) == 2
        bodies = [body for _, _, _, body in calls]
        for method, path, _params, body in calls:
            assert (method, path) == ("POST", "/v5/asset/transfer/inter-transfer")
            assert body["coin"] == "USDT"
            assert body["amount"] == "10.5"
            # v5 inter-transfer needs directional account types + unique id
            # (the old body sent a single transferAccountType, identical for
            # both directions, which the API always rejected).
            assert "transferAccountType" not in body
        assert bodies[0]["fromAccountType"] == "SPOT"
        assert bodies[0]["toAccountType"] == "UNIFIED"
        assert bodies[1]["fromAccountType"] == "UNIFIED"
        assert bodies[1]["toAccountType"] == "SPOT"
        assert all(b.get("transferId") for b in bodies)
        assert bodies[0]["transferId"] != bodies[1]["transferId"]
        with patch.object(bb, "_api_call", side_effect=RuntimeError("api down")):
            assert _fresh_venue().transfer_asset("USDT", 1, "spot", "futures") is False


class TestBalancesAndPositions:
    @staticmethod
    def _router(error=False):
        def fake(method, path, params=None, body=None):
            if path == "/v5/account/wallet-balance":
                if error:
                    raise RuntimeError("wallet down")
                return {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "coin": [
                                    {"coin": "USDT", "availableToWithdraw": "100"},
                                    {"coin": "BTC", "walletBalance": "0.5"},
                                    {"coin": "ETH", "walletBalance": "2.0"},
                                ]
                            }
                        ]
                    },
                }
            if path == "/v5/position/list":
                return {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "side": "Buy",
                                "size": "0.5",
                                "avgPrice": "60000",
                                "liqPrice": "0",
                                "leverage": "1",
                                "unrealisedPnl": "10",
                            },
                            {"symbol": "ETHUSDT", "size": "0"},
                            {
                                "symbol": "SOLUSDT",
                                "side": "Sell",
                                "size": "20",
                                "avgPrice": "150",
                                "liqPrice": "300",
                                "leverage": "2",
                                "unrealisedPnl": "-5",
                            },
                        ]
                    },
                }
            raise AssertionError(f"unexpected api call {method} {path}")

        return fake

    def test_fetch_balances_parses_unified_wallet(self):
        calls: list[tuple] = []
        with patch.object(bb, "_api_call", self._router()):
            v = _fresh_venue()
            bal = v.fetch_balances(["USDT", "BTC"])
        assert bal == {"USDT": 100.0, "BTC": 0.5}

    def test_fetch_balances_fail_loud_on_error(self):
        # No try/except in bybit's fetch_balances: errors propagate (fail-loud).
        with patch.object(bb, "_api_call", self._router(error=True)):
            v = _fresh_venue()
            try:
                v.fetch_balances(["USDT"])
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert "wallet down" in str(e)

    def test_fetch_futures_positions_parse(self):
        with patch.object(bb, "_api_call", self._router()):
            positions = _fresh_venue().fetch_futures_positions()
        assert len(positions) == 2
        btc = next(p for p in positions if p["symbol"] == "BTCUSDT")
        assert btc["side"] == "long" and btc["qty"] == 0.5
        assert btc["entry_price"] == 60000.0
        sol = next(p for p in positions if p["symbol"] == "SOLUSDT")
        assert sol["side"] == "short" and sol["qty"] == 20.0
        assert sol["liq_price"] == 300.0


class TestRegistration:
    def test_get_venue_registry(self):
        from venues import get_venue, supported_venues

        assert "bybit" in supported_venues()
        v = get_venue({"venue": {"type": "bybit"}})
        assert v.venue_id == "bybit"
        assert isinstance(v, BybitSpotVenue)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
