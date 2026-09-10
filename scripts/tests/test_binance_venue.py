#!/usr/bin/env python3
"""Tests for the Binance venue adapter (mocked HTTP, no network).

Covers: spot/perp tickers, spot + fapi symbol rules, the Binance funding
provider, dry-run execute_trades record shape, live order bodies captured via
a faked module-level `_api_call` (quoteOrderQty buys, reduceOnly closes,
transfer_asset, fail-loud balances) and the venue registry.

NOTE: get_all_futures_tickers is intentionally NOT tested here (a separate
regression test owns that endpoint's URL handling).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtest.funding_providers as fp_mod
import venues.binance as bn
from venues.binance import BinanceSpotVenue

# ── canned exchangeInfo payloads ──────────────────────────────────────────────

_SPOT_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            ],
        }
    ]
}

_FAPI_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "quantityPrecision": 3,
            "pricePrecision": 2,
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.002"},
                {"filterType": "NOTIONAL", "notional": "5"},
            ],
        }
    ]
}


def _reset_caches() -> None:
    bn._symbol_rules_cache = {}
    bn._futures_rules_cache = {}
    bn._exchange_info_loaded_at = 0.0
    bn._exchange_info_symbols = {}
    bn._fapi_exchange_info_loaded_at = 0.0
    bn._fapi_exchange_info_rules = {}
    bn._spot_ticker_loaded_at = 0.0
    bn._spot_ticker_prices = {}
    bn._futures_ticker_loaded_at = 0.0
    bn._futures_ticker_prices = {}
    bn._initialized_symbols = set()


def _fresh_venue() -> BinanceSpotVenue:
    """Reset module-level caches so each test fakes its own HTTP responses."""
    _reset_caches()
    return BinanceSpotVenue()


@pytest.fixture(autouse=True)
def _restore_clean_module_state():
    """Leave the venue module's caches empty after every test (no leakage)."""
    yield
    _reset_caches()


class TestMarketData:
    def test_get_ticker_spot_price_and_failure(self):
        calls: list[tuple] = []

        def fake(method, path, params=None, signed=False):
            calls.append((method, path, dict(params or {}), signed))
            return {"price": "60000.5"}

        with patch.object(bn, "_api_call", fake):
            v = _fresh_venue()
            assert v.get_ticker("BTCUSDT") == 60000.5
        assert calls == [
            ("GET", "/api/v3/ticker/price", {"symbol": "BTCUSDT"}, False)
        ]

        with patch.object(bn, "_api_call", side_effect=RuntimeError("down")):
            assert _fresh_venue().get_ticker("BTCUSDT") == 0.0

    def test_get_futures_ticker_perp_price(self):
        calls: list[tuple] = []

        def fake(method, path, params=None, signed=False):
            calls.append((method, path, dict(params or {}), signed))
            return {"price": "61000.25"}

        with patch.object(bn, "_api_call", fake):
            v = _fresh_venue()
            assert v.get_futures_ticker("BTCUSDT") == 61000.25
        assert calls == [
            ("GET", "/fapi/v1/ticker/price", {"symbol": "BTCUSDT"}, False)
        ]

        with patch.object(bn, "_api_call", side_effect=RuntimeError("down")):
            assert _fresh_venue().get_futures_ticker("BTCUSDT") == 0.0


class TestSymbolRules:
    def test_fetch_symbol_rules_spot_filters(self):
        def fake(method, path, params=None, signed=False):
            assert (method, path) == ("GET", "/api/v3/exchangeInfo")
            return _SPOT_EXCHANGE_INFO

        with patch.object(bn, "_api_call", fake):
            v = _fresh_venue()
            rules = v.fetch_symbol_rules("BTCUSDT")
        assert rules is not None
        assert rules["symbol"] == "BTCUSDT"
        assert rules["min_trade_base"] == 0.001
        assert rules["min_trade_usdt"] == 5.0
        assert rules["quantity_precision"] == 3  # from stepSize 0.001
        assert rules["quote_precision"] == 2  # from tickSize 0.01
        assert rules["status"] == "TRADING"

    def test_fetch_futures_symbol_rules_fapi(self):
        def fake(method, path, params=None, signed=False):
            assert (method, path) == ("GET", "/fapi/v1/exchangeInfo")
            return _FAPI_EXCHANGE_INFO

        with patch.object(bn, "_api_call", fake):
            v = _fresh_venue()
            rules = v.fetch_futures_symbol_rules("BTCUSDT")
            assert v.fetch_futures_symbol_rules("NOPEUSDT") is None
        assert rules is not None
        assert rules["symbol"] == "BTCUSDT"
        assert rules["min_trade_base"] == 0.002  # LOT_SIZE minQty
        assert rules["min_trade_usdt"] == 5.0  # NOTIONAL notional
        assert rules["quantity_precision"] == 3  # quantityPrecision
        assert rules["quote_precision"] == 2  # pricePrecision
        assert rules["status"] == "TRADING"


class TestFundingProvider:
    """backtest.funding_providers.BinanceFundingProvider with canned JSON."""

    _PREMIUM_ALL = [
        {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 1700006400000,
            "markPrice": "60000.5",
            "indexPrice": "59999.5",
        },
        {
            "symbol": "ETHUSDT",
            "lastFundingRate": "-0.0002",
            "nextFundingTime": 1700006400000,
            "markPrice": "3000",
            "indexPrice": "2999",
        },
        {
            "symbol": "BTCUSDC",  # non-USDT quote filtered out
            "lastFundingRate": "0.0003",
            "nextFundingTime": 1700006400000,
        },
    ]
    _FUNDING_INFO = [
        {"symbol": "BTCUSDT", "fundingIntervalHours": 4},
        {"symbol": "DOGEUSDT", "fundingIntervalHours": 1},
    ]

    def _fake_http(self, premium=None, info=None, fail_info=False):
        def fake(url, max_retries=5):
            if "/fapi/v1/premiumIndex" in url:
                return premium if premium is not None else self._PREMIUM_ALL
            if "/fapi/v1/fundingInfo" in url:
                if fail_info:
                    raise RuntimeError("fundingInfo down")
                return info if info is not None else self._FUNDING_INFO
            raise AssertionError(f"unexpected url {url}")

        return fake

    def test_fetch_all_maps_rows(self):
        with patch.object(
            fp_mod, "_http_get_with_retry", self._fake_http()
        ):
            rows = fp_mod.BinanceFundingProvider().fetch_all("USDT")
        assert {r["symbol"] for r in rows} == {"BTCUSDT", "ETHUSDT"}
        btc = next(r for r in rows if r["symbol"] == "BTCUSDT")
        assert btc["rate_pct"] == 0.01  # decimal -> pct
        assert btc["next_funding_ts"] == 1700006400000
        assert btc["mark_price"] == 60000.5
        assert btc["index_price"] == 59999.5
        eth = next(r for r in rows if r["symbol"] == "ETHUSDT")
        assert eth["rate_pct"] == -0.02

    def test_fetch_interval_map_overrides(self):
        # fundingInfo lists only non-default intervals (4h / 1h overrides)
        with patch.object(
            fp_mod, "_http_get_with_retry", self._fake_http()
        ):
            imap = fp_mod.BinanceFundingProvider().fetch_interval_map("USDT")
        assert imap == {"BTCUSDT": 4.0, "DOGEUSDT": 1.0}
        # HTTP failure -> empty map (consumers fall back to the 8h default)
        with patch.object(
            fp_mod, "_http_get_with_retry", self._fake_http(fail_info=True)
        ):
            assert fp_mod.BinanceFundingProvider().fetch_interval_map() == {}

    def test_fetch_current_interval_and_settle(self):
        single = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 1700006400000,
            "markPrice": "60000.5",
            "indexPrice": "59999.5",
        }
        with patch.object(
            fp_mod, "_http_get_with_retry", self._fake_http(premium=single)
        ):
            row = fp_mod.BinanceFundingProvider().fetch_current("BTCUSDT")
        # BTCUSDT has a 4h override in fundingInfo
        assert row["interval_ms"] == 4 * 60 * 60 * 1000
        assert row["next_funding_ts"] == 1700006400000
        assert row["last_settle_ts"] == 1700006400000 - 4 * 60 * 60 * 1000
        assert row["rate_pct"] == 0.01
        assert row["mark_price"] == 60000.5
        assert row["index_price"] == 59999.5

        eth_single = dict(single, symbol="ETHUSDT")
        with patch.object(
            fp_mod, "_http_get_with_retry", self._fake_http(premium=eth_single)
        ):
            row2 = fp_mod.BinanceFundingProvider().fetch_current("ETHUSDT")
        # ETHUSDT not in fundingInfo -> default 8h interval
        assert row2["interval_ms"] == 8 * 60 * 60 * 1000
        assert row2["last_settle_ts"] == 1700006400000 - 8 * 60 * 60 * 1000


class TestExecutionDryRun:
    def test_dry_run_normalized_records_no_http(self):
        calls: list[tuple] = []

        def fake(method, path, params=None, signed=False):
            calls.append((method, path, params, signed))
            return {}

        trades = [
            {"symbol": "BTC", "type": "buy", "amount_usdt": 600.0},
            {"symbol": "BTC", "type": "sell", "amount_base": 0.01},
            {"symbol": "BTC", "type": "open_long", "amount_base": 0.01},
            {"symbol": "BTC", "type": "open_short", "amount_base": 0.01},
            {"symbol": "BTC", "type": "close_long", "amount_base": 0.01},
            {"symbol": "BTC", "type": "close_short", "amount_base": 0.01},
        ]
        market = {"BTC": {"price": 60000.0, "pair": "BTCUSDT"}}
        with patch.object(bn, "_api_call", fake):
            v = _fresh_venue()
            results = v.execute_trades(trades, market, dry_run=True)
        assert len(results) == 6
        assert calls == []  # dry-run must not touch the network
        for r in results:
            assert r["status"] == "simulated"
            assert r["venue"] == "binance"
            assert r["dry_run"] is True
            assert r["order_id"] is None
            assert r["slippage"] == 0.0
            assert r["latency_ms"] == 0
            assert r["ref_price"] == 60000.0
        by_type = {r["type"]: r for r in results}
        assert by_type["buy"]["amount_usdt"] == 600.0
        assert by_type["open_long"]["amount_base"] == 0.01


class TestExecutionLive:
    """Live order bodies via a capturing fake `_api_call` (signed router)."""

    @staticmethod
    def _router(calls):
        def fake(method, path, params=None, signed=False):
            calls.append((method, path, dict(params or {}), signed))
            if (method, path) == ("POST", "/api/v3/order"):
                return {"orderId": 111, "status": "NEW"}
            if (method, path) == ("GET", "/api/v3/order"):
                return {
                    "executedQty": "0.01",
                    "cummulativeQuoteQty": "601",
                    "status": "FILLED",
                }
            if (method, path) == ("GET", "/fapi/v1/exchangeInfo"):
                return _FAPI_EXCHANGE_INFO
            if (method, path) == ("POST", "/fapi/v1/order"):
                return {
                    "orderId": 222,
                    "executedQty": "0.01",
                    "avgPrice": "60500",
                    "status": "FILLED",
                }
            raise AssertionError(f"unexpected api call {method} {path}")

        return fake

    def test_spot_buy_uses_quote_order_qty(self):
        calls: list[tuple] = []
        with patch.object(bn, "_api_call", self._router(calls)):
            v = _fresh_venue()
            res = v.execute_trades(
                [{"symbol": "BTC", "type": "buy", "amount_usdt": 600.0}],
                {"BTC": {"price": 60000.0, "pair": "BTCUSDT", "quote_precision": 2}},
                dry_run=False,
            )
        r = res[0]
        assert r["status"] == "filled"
        assert r["order_id"] == "111"
        assert r["order_status"] == "FILLED"
        assert abs(r["exec_price"] - 60100.0) < 1e-9  # 601 / 0.01
        assert abs(r["exec_qty"] - 0.01) < 1e-9
        assert abs(r["exec_quote_usd"] - 601.0) < 1e-9
        assert abs(r["slippage"] - round((60100.0 - 60000.0) / 60000.0, 6)) < 1e-9
        post = next(c for c in calls if c[:2] == ("POST", "/api/v3/order"))
        assert post[3] is True  # signed
        body = post[2]
        assert body["symbol"] == "BTCUSDT"
        assert body["side"] == "BUY"
        assert body["type"] == "MARKET"
        assert body["quoteOrderQty"] == "600.00"  # quote-sized market buy
        assert "quantity" not in body
        assert body["newClientOrderId"].startswith("qbuy")
        # fill confirmation comes from the signed order-detail GET
        detail = next(c for c in calls if c[:2] == ("GET", "/api/v3/order"))
        assert detail[2] == {"symbol": "BTCUSDT", "orderId": "111"}
        assert detail[3] is True

    def test_spot_sell_uses_base_quantity(self):
        calls: list[tuple] = []
        with patch.object(bn, "_api_call", self._router(calls)):
            v = _fresh_venue()
            res = v.execute_trades(
                [{"symbol": "BTC", "type": "sell", "amount_base": 0.01}],
                {"BTC": {"price": 60000.0, "pair": "BTCUSDT", "quantity_precision": 6}},
                dry_run=False,
            )
        r = res[0]
        assert r["status"] == "filled"
        assert abs(r["exec_price"] - 60100.0) < 1e-9  # 601 quote / 0.01 base
        post = next(c for c in calls if c[:2] == ("POST", "/api/v3/order"))
        body = post[2]
        assert body["side"] == "SELL"
        assert body["quantity"] == "0.01"  # base-sized market sell
        assert "quoteOrderQty" not in body

    def test_perp_side_mapping_and_reduce_only(self):
        expected = {
            "open_long": ("BUY", False),
            "open_short": ("SELL", False),
            "close_long": ("SELL", True),
            "close_short": ("BUY", True),
        }
        for typ, (side, reduce_only) in expected.items():
            calls: list[tuple] = []
            with patch.object(bn, "_api_call", self._router(calls)):
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
            assert r["order_id"] == "222", typ
            # fapi /order returns avgPrice; exec price comes straight from it
            assert abs(r["exec_price"] - 60500.0) < 1e-9, typ
            assert abs(r["exec_qty"] - 0.01) < 1e-9, typ
            assert abs(r["slippage"] - round((60500.0 - 60000.0) / 60000.0, 6)) < 1e-9, typ
            post = next(c for c in calls if c[:2] == ("POST", "/fapi/v1/order"))
            assert post[3] is True, typ  # signed
            body = post[2]
            assert body["side"] == side, typ
            assert body["quantity"] == "0.01", typ
            assert body["type"] == "MARKET", typ
            if reduce_only:
                assert body["reduceOnly"] == "true", typ
            else:
                assert "reduceOnly" not in body, typ

    def test_unknown_trade_type_fails(self):
        v = _fresh_venue()
        res = v.execute_trades(
            [{"symbol": "BTC", "type": "hodl", "amount_base": 1}],
            {"BTC": {"price": 1.0}},
            dry_run=False,
        )
        assert res[0]["status"] == "failed"
        assert res[0]["order_id"] is None
        assert "Unknown trade type" in res[0]["error"]


class TestTransfer:
    def test_transfer_asset_direction_and_body(self):
        calls: list[tuple] = []

        def fake(method, path, params=None, signed=False):
            calls.append((method, path, dict(params or {}), signed))
            return {"tranId": 123456}

        with patch.object(bn, "_api_call", fake):
            v = _fresh_venue()
            assert v.transfer_asset("USDT", 10.5, "spot", "futures") is True
            assert v.transfer_asset("USDT", 10.5, "futures", "spot") is True
            assert v.transfer_asset("USDT", 10.5, "spot", "margin") is False
        assert len(calls) == 2  # unsupported direction makes no API call
        spot_to_fut = calls[0]
        assert spot_to_fut[:2] == ("POST", "/sapi/v1/futures/transfer")
        assert spot_to_fut[2] == {"type": 1, "asset": "USDT", "amount": "10.5"}
        assert spot_to_fut[3] is True
        assert calls[1][2]["type"] == 2  # futures -> spot
        # failed transfer (no tranId) reports False
        with patch.object(bn, "_api_call", return_value={"msg": "boom"}):
            assert _fresh_venue().transfer_asset("USDT", 1, "spot", "futures") is False


class TestBalancesAndPositions:
    @staticmethod
    def _router(spot_error=False, futures_error=False):
        def fake(method, path, params=None, signed=False):
            if path == "/api/v3/account":
                if spot_error:
                    raise RuntimeError("spot account down")
                return {
                    "balances": [
                        {"asset": "USDT", "free": "100.0"},
                        {"asset": "BTC", "free": "0.5"},
                        {"asset": "ETH", "free": "2.0"},  # not requested
                    ]
                }
            if path == "/fapi/v2/account":
                if futures_error:
                    raise RuntimeError("fapi down")
                return {"assets": [{"asset": "USDT", "marginBalance": "50.0"}]}
            raise AssertionError(f"unexpected api call {method} {path}")

        return fake

    def test_fetch_balances_parses_spot_plus_futures(self):
        with patch.object(bn, "_api_call", self._router()):
            v = _fresh_venue()
            bal = v.fetch_balances(["USDT", "BTC"])
        assert bal == {"USDT": 150.0, "BTC": 0.5}  # 100 spot + 50 futures margin

    def test_fetch_balances_fail_loud_on_spot_error(self):
        # Balance failure must propagate (never silently report all zeros).
        with patch.object(bn, "_api_call", self._router(spot_error=True)):
            v = _fresh_venue()
            try:
                v.fetch_balances(["USDT"])
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert "spot account down" in str(e)

    def test_fetch_balances_futures_error_only_degrades(self):
        with patch.object(bn, "_api_call", self._router(futures_error=True)):
            v = _fresh_venue()
            bal = v.fetch_balances(["USDT"])
        assert bal == {"USDT": 100.0}  # futures side swallowed, spot kept

    def test_fetch_futures_positions_parse(self):
        def fake(method, path, params=None, signed=False):
            assert (method, path) == ("GET", "/fapi/v2/positionRisk")
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.5",
                    "entryPrice": "60000",
                    "liquidationPrice": "0",
                    "leverage": "1",
                    "unRealizedProfit": "10.0",
                },
                {"symbol": "ETHUSDT", "positionAmt": "0"},
                {
                    "symbol": "SOLUSDT",
                    "positionAmt": "-20",
                    "entryPrice": "150",
                    "liquidationPrice": "300",
                    "leverage": "2",
                    "unRealizedProfit": "-5.0",
                },
            ]

        with patch.object(bn, "_api_call", fake):
            positions = _fresh_venue().fetch_futures_positions()
        assert len(positions) == 2
        btc = next(p for p in positions if p["symbol"] == "BTCUSDT")
        assert btc["side"] == "long" and btc["qty"] == 0.5
        assert btc["entry_price"] == 60000.0
        sol = next(p for p in positions if p["symbol"] == "SOLUSDT")
        assert sol["side"] == "short" and sol["qty"] == 20.0


class TestRegistration:
    def test_get_venue_registry(self):
        from venues import get_venue, supported_venues

        assert "binance" in supported_venues()
        v = get_venue({"venue": {"type": "binance"}})
        assert v.venue_id == "binance"
        assert isinstance(v, BinanceSpotVenue)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
