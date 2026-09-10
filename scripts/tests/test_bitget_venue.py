#!/usr/bin/env python3
"""Tests for the Bitget venue adapter (mocked HTTP, no network).

Covers: spot/perp tickers (via faked http_get_json), spot + mix-contract
symbol rules, the Bitget funding provider, dry-run execute_trades records
(with simulated ±2bps slippage), live order bodies captured via a faked
module-level `_api_call` (quote-sized buys, open/close side naming, transfer
mapping spot<->usdt_futures, fail-loud balances) and the venue registry.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtest.funding_providers as fp_mod
import venues.bitget as bg
from venues.bitget import BitgetSpotVenue

# ── canned public payloads (routed by URL substring) ──────────────────────────

_SPOT_TICKER = {"code": "00000", "data": [{"symbol": "BTCUSDT", "lastPr": "60000.5"}]}
_MIX_TICKER_LIST = {
    "code": "00000",
    "data": [{"symbol": "BTCUSDT", "lastPr": "61000.25"}],
}
_MIX_TICKER_DICT = {"code": "00000", "data": {"symbol": "BTCUSDT", "lastPr": "61001.5"}}

_SPOT_SYMBOLS = {
    "code": "00000",
    "data": [
        {
            "symbol": "BTCUSDT",
            "quantityPrecision": 4,
            "quotePrecision": 2,
            "minTradeUSDT": "5",
            "minTradeAmount": "0.001",
            "status": "online",
        }
    ],
}

_MIX_CONTRACTS = {
    "code": "00000",
    "data": [
        {
            "symbol": "BTCUSDT",
            "sizeMultiplier": "0.001",
            "pricePlace": 2,
            "minTradeNum": "0.002",
            "symbolStatus": "normal",
        },
        {"symbol": "ETHUSDT", "sizeMultiplier": "0.01", "pricePlace": 1},
    ],
}


def _reset_caches() -> None:
    bg._symbol_rules_cache = {}
    bg._futures_rules_cache = {}
    bg._spot_ticker_loaded_at = 0.0
    bg._spot_ticker_prices = {}
    bg._futures_ticker_loaded_at = 0.0
    bg._futures_ticker_prices = {}
    bg._initialized_symbols = set()


def _fresh_venue() -> BitgetSpotVenue:
    """Reset module-level caches so each test fakes its own HTTP responses."""
    _reset_caches()
    return BitgetSpotVenue()


@pytest.fixture(autouse=True)
def _restore_clean_module_state():
    """Leave the venue module's caches empty after every test (no leakage)."""
    yield
    _reset_caches()


def _fake_http(spot_ticker=None, mix_ticker=None, spot_symbols=None, mix_contracts=None):
    def fake(url):
        if "spot/market/tickers" in url:
            return spot_ticker or _SPOT_TICKER
        if "mix/market/ticker" in url:
            return mix_ticker or _MIX_TICKER_LIST
        if "spot/public/symbols" in url:
            return spot_symbols or _SPOT_SYMBOLS
        if "mix/market/contracts" in url:
            return mix_contracts or _MIX_CONTRACTS
        raise AssertionError(f"unexpected url {url}")

    return fake


class TestMarketData:
    def test_get_ticker_spot_price_and_failure(self):
        urls: list[str] = []

        def fake(url):
            urls.append(url)
            return _SPOT_TICKER

        with patch.object(bg, "http_get_json", fake):
            v = _fresh_venue()
            assert v.get_ticker("BTCUSDT") == 60000.5
        assert len(urls) == 1 and "spot/market/tickers" in urls[0]
        assert "BTCUSDT" in urls[0]

        with patch.object(bg, "http_get_json", side_effect=RuntimeError("down")):
            assert _fresh_venue().get_ticker("BTCUSDT") == 0.0

    def test_get_futures_ticker_perp_price(self):
        # data may arrive as list or dict
        with patch.object(bg, "http_get_json", _fake_http()):
            v = _fresh_venue()
            assert v.get_futures_ticker("BTCUSDT") == 61000.25
        with patch.object(bg, "http_get_json", _fake_http(mix_ticker=_MIX_TICKER_DICT)):
            v = _fresh_venue()
            assert v.get_futures_ticker("BTCUSDT") == 61001.5

        with patch.object(bg, "http_get_json", side_effect=RuntimeError("down")):
            assert _fresh_venue().get_futures_ticker("BTCUSDT") == 0.0


class TestSymbolRules:
    def test_fetch_symbol_rules_spot(self):
        with patch.object(bg, "http_get_json", _fake_http()):
            v = _fresh_venue()
            rules = v.fetch_symbol_rules("BTCUSDT")
        assert rules is not None
        assert rules["symbol"] == "BTCUSDT"
        assert rules["min_trade_base"] == 0.001  # minTradeAmount
        assert rules["min_trade_usdt"] == 5.0  # minTradeUSDT
        assert rules["quantity_precision"] == 4
        assert rules["quote_precision"] == 2
        assert rules["status"] == "online"

    def test_fetch_symbol_rules_unknown_pair(self):
        empty = {"code": "00000", "data": []}
        with patch.object(bg, "http_get_json", _fake_http(spot_symbols=empty)):
            v = _fresh_venue()
            assert v.fetch_symbol_rules("NOPEUSDT") is None

    def test_fetch_futures_symbol_rules_mix_contracts(self):
        urls: list[str] = []

        def fake(url):
            urls.append(url)
            return _MIX_CONTRACTS

        with patch.object(bg, "http_get_json", fake):
            v = _fresh_venue()
            rules = v.fetch_futures_symbol_rules("BTCUSDT")
            assert v.fetch_futures_symbol_rules("NOPEUSDT") is None
        assert rules is not None
        assert rules["symbol"] == "BTCUSDT"
        assert rules["quantity_precision"] == 3  # sizeMultiplier 0.001
        assert rules["quote_precision"] == 2  # pricePlace
        assert rules["min_trade_base"] == 0.002  # minTradeNum
        assert rules["min_trade_usdt"] == 0
        assert rules["status"] == "normal"
        assert any("mix/market/contracts" in u for u in urls)


class TestFundingProvider:
    """backtest.funding_providers.BitgetFundingProvider with canned JSON."""

    _TICKERS = {
        "code": "00000",
        "data": [
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.0001",
                "markPrice": "60000",
                "indexPrice": "59990",
            },
            {
                "symbol": "ETHBTC",  # non-USDT quote filtered out
                "fundingRate": "0.0002",
            },
        ],
    }
    _CURRENT = {
        "code": "00000",
        "data": [
            {
                "fundingRate": "0.0001",
                "fundingRateInterval": "8",
                "markPrice": "60000",
                "indexPrice": "59990",
            }
        ],
    }

    def _fake_http(self, tickers=None, current=None):
        def fake(url, max_retries=5):
            if "mix/market/tickers" in url:
                return tickers or self._TICKERS
            if "current-fund-rate" in url:
                return current or self._CURRENT
            raise AssertionError(f"unexpected url {url}")

        return fake

    def test_fetch_all_maps_rows(self):
        with patch.object(fp_mod, "_http_get_with_retry", self._fake_http()):
            rows = fp_mod.BitgetFundingProvider().fetch_all("USDT")
        assert {r["symbol"] for r in rows} == {"BTCUSDT"}
        btc = rows[0]
        assert btc["rate_pct"] == 0.01
        assert btc["mark_price"] == 60000.0
        assert btc["index_price"] == 59990.0
        assert btc["next_funding_ts"] > 0  # estimated locally (fixed 8h grid)

    def test_fetch_current_interval_and_settle(self):
        with patch.object(fp_mod, "_http_get_with_retry", self._fake_http()):
            row = fp_mod.BitgetFundingProvider().fetch_current("BTCUSDT")
        assert row["rate_pct"] == 0.01
        assert row["interval_ms"] == 8 * 60 * 60 * 1000
        assert row["next_funding_ts"] > 0
        assert row["last_settle_ts"] == row["next_funding_ts"] - row["interval_ms"]
        assert row["mark_price"] == 60000.0
        assert row["index_price"] == 59990.0

        four_h = {
            "code": "00000",
            "data": [{"fundingRate": "0.0001", "fundingRateInterval": "4"}],
        }
        with patch.object(
            fp_mod, "_http_get_with_retry", self._fake_http(current=four_h)
        ):
            row4 = fp_mod.BitgetFundingProvider().fetch_current("BTCUSDT")
        assert row4["interval_ms"] == 4 * 60 * 60 * 1000

    def test_fetch_interval_map_is_default_empty(self):
        # Bitget exposes no override map; consumers default to the 8h interval.
        assert fp_mod.BitgetFundingProvider().fetch_interval_map() == {}


class TestExecutionDryRun:
    def test_dry_run_normalized_records_and_simulated_slippage(self):
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
            assert r["venue"] == "bitget"
            assert r["dry_run"] is True
            assert r["order_id"] is None
            assert r["latency_ms"] == 0
            assert r["ref_price"] == 60000.0
        by_type = {r["type"]: r for r in results}
        # Bitget paper-trades 2bps slippage: buys pay up, everything else down
        assert by_type["buy"]["slippage"] == 0.0002
        for typ in ("sell", "open_long", "open_short", "close_long", "close_short"):
            assert by_type[typ]["slippage"] == -0.0002, typ
        # custom slippage_bps honored
        res10 = v.execute_trades(
            [{"symbol": "BTC", "type": "buy", "amount_usdt": 600.0, "slippage_bps": 10}],
            market,
            dry_run=True,
        )
        assert res10[0]["slippage"] == 0.001


class TestExecutionLive:
    """Live order bodies via a capturing fake `_api_call`."""

    @staticmethod
    def _router(calls):
        def fake(method, path, params=None, body=None):
            calls.append((method, path, dict(params or {}), dict(body or {})))
            if path == "/api/v2/spot/trade/place-order":
                return {"code": "00000", "data": {"orderId": "777"}}
            if path == "/api/v2/spot/trade/orderInfo":
                return {
                    "code": "00000",
                    "data": [
                        {
                            "priceAvg": "60100",
                            "status": "filled",
                            "quoteVolume": "601",
                            "sizeAccumulate": "0.01",
                        }
                    ],
                }
            if path == "/api/v2/mix/order/place-order":
                return {"code": "00000", "data": {"orderId": "888"}}
            if path == "/api/v2/mix/order/detail":
                return {"code": "00000", "data": {"priceAvg": "60500"}}
            if path in (
                "/api/v2/mix/account/set-margin-mode",
                "/api/v2/mix/account/set-leverage",
            ):
                return {"code": "00000"}
            raise AssertionError(f"unexpected api call {method} {path}")

        return fake

    def test_spot_buy_sends_quote_size(self):
        calls: list[tuple] = []
        with patch.object(bg, "_api_call", self._router(calls)):
            v = _fresh_venue()
            res = v.execute_trades(
                [{"symbol": "BTC", "type": "buy", "amount_usdt": 600.0}],
                {"BTC": {"price": 60000.0, "pair": "BTCUSDT", "quote_precision": 2}},
                dry_run=False,
            )
        r = res[0]
        assert r["status"] == "filled"
        assert r["order_id"] == "777"
        assert r["order_status"] == "filled"
        assert abs(r["exec_price"] - 60100.0) < 1e-9  # priceAvg from orderInfo
        assert abs(r["exec_qty"] - 0.01) < 1e-9  # quoteVolume / priceAvg
        assert abs(r["exec_quote_usd"] - 601.0) < 1e-9
        assert abs(r["slippage"] - round((60100.0 - 60000.0) / 60000.0, 6)) < 1e-9
        post = next(c for c in calls if c[:2] == ("POST", "/api/v2/spot/trade/place-order"))
        body = post[3]
        assert body["symbol"] == "BTCUSDT"
        assert body["side"] == "buy"
        assert body["orderType"] == "market"
        assert body["size"] == "600.00"  # buy size is quote-denominated
        assert body["clientOid"].startswith("qbuy")
        # fill confirmation query carries the orderId
        detail = next(c for c in calls if c[:2] == ("GET", "/api/v2/spot/trade/orderInfo"))
        assert detail[2] == {"orderId": "777"}

    def test_spot_sell_sends_base_size(self):
        calls: list[tuple] = []
        with patch.object(bg, "_api_call", self._router(calls)):
            v = _fresh_venue()
            res = v.execute_trades(
                [{"symbol": "BTC", "type": "sell", "amount_base": 0.01}],
                {"BTC": {"price": 60000.0, "pair": "BTCUSDT", "quantity_precision": 6}},
                dry_run=False,
            )
        assert res[0]["status"] == "filled"
        post = next(c for c in calls if c[:2] == ("POST", "/api/v2/spot/trade/place-order"))
        body = post[3]
        assert body["side"] == "sell"
        assert body["size"] == "0.01"  # sell size is base-denominated

    def test_perp_side_naming_and_fill_query(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)  # skip 0.5s fill wait
        for typ in ("open_long", "open_short", "close_long", "close_short"):
            calls: list[tuple] = []
            with patch.object(bg, "_api_call", self._router(calls)):
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
            assert r["order_id"] == "888", typ
            assert abs(r["exec_price"] - 60500.0) < 1e-9, typ  # mix order detail
            assert abs(r["exec_qty"] - 0.01) < 1e-9, typ
            assert abs(r["slippage"] - round((60500.0 - 60000.0) / 60000.0, 6)) < 1e-9, typ
            post = next(c for c in calls if c[:2] == ("POST", "/api/v2/mix/order/place-order"))
            body = post[3]
            assert body["symbol"] == "BTCUSDT", typ
            assert body["productType"] == "USDT-FUTURES", typ
            assert body["marginMode"] == "isolated", typ
            assert body["marginCoin"] == "USDT", typ
            # Bitget names sides after the trade type itself (no reduceOnly flag)
            assert body["side"] == typ, typ
            assert body["orderType"] == "market", typ
            assert body["size"] == "0.01", typ
            detail = next(c for c in calls if c[:2] == ("GET", "/api/v2/mix/order/detail"))
            assert detail[2] == {"symbol": "BTCUSDT", "orderId": "888"}, typ

    def test_api_error_fails_the_record(self):
        def fake(method, path, params=None, body=None):
            return {"code": "40001", "msg": "insufficient balance"}

        with patch.object(bg, "_api_call", fake):
            v = _fresh_venue()
            res = v.execute_trades(
                [{"symbol": "BTC", "type": "buy", "amount_usdt": 600.0}],
                {"BTC": {"price": 60000.0, "pair": "BTCUSDT", "quote_precision": 2}},
                dry_run=False,
            )
        assert res[0]["status"] == "failed"
        assert res[0]["order_id"] is None
        assert "40001" in res[0]["error"]

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
    def test_transfer_asset_direction_and_body(self):
        calls: list[tuple] = []

        def fake(method, path, params=None, body=None):
            calls.append((method, path, dict(params or {}), dict(body or {})))
            return {"code": "00000"}

        with patch.object(bg, "_api_call", fake):
            v = _fresh_venue()
            assert v.transfer_asset("USDT", 10.5, "spot", "futures") is True
            assert v.transfer_asset("USDT", 10.5, "futures", "spot") is True
            assert v.transfer_asset("USDT", 10.5, "spot", "earn") is False
        assert len(calls) == 2
        spot_to_fut = calls[0]
        assert spot_to_fut[:2] == ("POST", "/api/v2/spot/wallet/transfer")
        assert spot_to_fut[3]["fromType"] == "spot"
        assert spot_to_fut[3]["toType"] == "usdt_futures"
        assert spot_to_fut[3]["amount"] == "10.5"
        assert spot_to_fut[3]["coin"] == "USDT"
        assert calls[1][3]["fromType"] == "usdt_futures"
        assert calls[1][3]["toType"] == "spot"
        # non-success code reports False
        with patch.object(bg, "_api_call", return_value={"code": "40001"}):
            assert _fresh_venue().transfer_asset("USDT", 1, "spot", "futures") is False


class TestBalancesAndPositions:
    @staticmethod
    def _router(spot_error=False, futures_error=False):
        def fake(method, path, params=None, body=None):
            if path == "/api/v2/spot/account/assets":
                if spot_error:
                    raise RuntimeError("spot assets down")
                return {
                    "code": "00000",
                    "data": [
                        {"coin": "USDT", "available": "100"},
                        {"coin": "BTC", "available": "0.5"},
                        {"coin": "ETH", "available": "2.0"},  # not requested
                    ],
                }
            if path == "/api/v2/mix/account/accounts":
                if futures_error:
                    raise RuntimeError("mix accounts down")
                return {"code": "00000", "data": [{"marginCoin": "USDT", "balance": "50"}]}
            raise AssertionError(f"unexpected api call {method} {path}")

        return fake

    def test_fetch_balances_parses_spot_plus_futures(self):
        with patch.object(bg, "_api_call", self._router()):
            bal = _fresh_venue().fetch_balances(["USDT", "BTC"])
        assert bal == {"USDT": 150.0, "BTC": 0.5}  # 100 spot + 50 futures

    def test_fetch_balances_fail_loud_on_spot_error(self):
        # Balance failure must propagate (never silently report all zeros).
        with patch.object(bg, "_api_call", self._router(spot_error=True)):
            v = _fresh_venue()
            try:
                v.fetch_balances(["USDT"])
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert "spot assets down" in str(e)

    def test_fetch_balances_futures_error_only_degrades(self):
        with patch.object(bg, "_api_call", self._router(futures_error=True)):
            bal = _fresh_venue().fetch_balances(["USDT"])
        assert bal == {"USDT": 100.0}  # futures side swallowed, spot kept

    def test_fetch_futures_positions_parse(self):
        def fake(method, path, params=None, body=None):
            assert (method, path) == ("GET", "/api/v2/mix/position/all-position")
            return {
                "code": "00000",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "holdSide": "long",
                        "total": "0.5",
                        "averageOpenPrice": "60000",
                        "liquidationPrice": "0",
                        "leverage": "1",
                        "unrealizedPL": "10",
                    },
                    {"symbol": "ETHUSDT", "total": "0"},
                    {
                        "symbol": "SOLUSDT",
                        "holdSide": "short",
                        "total": "20",
                        "openPriceAvg": "150",
                        "liquidationPrice": "300",
                        "leverage": "2",
                        "unrealizedPL": "-5",
                    },
                ],
            }

        with patch.object(bg, "_api_call", fake):
            positions = _fresh_venue().fetch_futures_positions()
        assert len(positions) == 2
        btc = next(p for p in positions if p["symbol"] == "BTCUSDT")
        assert btc["side"] == "long" and btc["qty"] == 0.5
        assert btc["entry_price"] == 60000.0
        sol = next(p for p in positions if p["symbol"] == "SOLUSDT")
        assert sol["side"] == "short" and sol["qty"] == 20.0
        assert sol["entry_price"] == 150.0  # openPriceAvg fallback


class TestRegistration:
    def test_get_venue_registry(self):
        from venues import get_venue, supported_venues

        assert "bitget" in supported_venues()
        v = get_venue({"venue": {"type": "bitget"}})
        assert v.venue_id == "bitget"
        assert isinstance(v, BitgetSpotVenue)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
