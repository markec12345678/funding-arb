#!/usr/bin/env python3
"""Tests for the dYdX v4 venue adapter (mocked indexer/SDK, no network)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import venues.dydx as dydx_mod  # noqa: E402
from venues.dydx import (  # noqa: E402
    DydxVenue,
    _base_from_pair,
    _market_meta,
    _side_from_type,
)

_BTC_META_ROW = {
    "ticker": "BTC-USD",
    "status": "ACTIVE",
    "stepSize": "0.0001",
    "tickSize": "1",
    "initialMarginFraction": "0.02",
    "maintenanceMarginFraction": "0.012",
    "atomicResolution": -10,
    "quantumConversionExponent": -9,
    "subticksPerTick": 100000,
    "clobPairId": 0,
    "stepBaseQuantums": 1000000,
}

_META_MAP = {"BTC": {**_market_meta(_BTC_META_ROW), "ticker": "BTC-USD"}}


def _fresh(monkeypatch=None) -> DydxVenue:
    v = DydxVenue()
    v._meta_cache = (9e18, _META_MAP)  # pre-warm: never expires in-test
    return v


class TestHelpers:
    def test_base_from_pair(self):
        assert _base_from_pair("BTCUSDT") == "BTC"
        assert _base_from_pair("1000PEPEUSDT") == "1000PEPE"
        assert _base_from_pair("ETH-USD") == "ETH-USD"  # passthrough non-USDT

    def test_market_meta_transform(self):
        meta = _market_meta(_BTC_META_ROW)
        assert meta["step_size"] == 0.0001
        assert meta["tick_size"] == 1.0
        assert meta["quantity_precision"] == 4  # 0.0001 → 4dp
        assert meta["quote_precision"] == 0  # tick 1 → 0dp
        assert meta["atomic_resolution"] == -10
        assert meta["quantum_conversion_exponent"] == -9
        assert meta["subticks_per_tick"] == 100000

    def test_market_meta_empty(self):
        assert _market_meta({}) == {}


class TestMarketData:
    def test_ticker_from_funding_provider(self):
        v = _fresh()
        with patch.object(
            v._funding, "fetch_current", return_value={"mark_price": 64000.5}
        ):
            assert v.get_futures_ticker("BTCUSDT") == 64000.5
            assert v.get_ticker("BTCUSDT") == 64000.5

    def test_symbol_rules(self):
        v = _fresh()
        rules = v.fetch_futures_symbol_rules("BTCUSDT")
        assert rules is not None
        assert rules["symbol"] == "BTCUSDT"
        assert rules["quantity_precision"] == 4
        assert rules["quote_precision"] == 0
        assert rules["step_size"] == 0.0001
        assert rules["subticks_per_tick"] == 100000

    def test_symbol_rules_unknown_base(self):
        v = _fresh()
        assert v.fetch_futures_symbol_rules("NOPEUSDT") is None

    def test_contract_meta_for_base(self):
        v = _fresh()
        meta = v.contract_meta_for_base("btc")
        assert meta is not None and meta["ticker"] == "BTC-USD"


class TestAccountGating:
    """Without DYDX_ENABLE_LIVE=1 + creds, account reads degrade safely."""

    def test_balances_no_creds(self, monkeypatch):
        monkeypatch.delenv("DYDX_MNEMONIC", raising=False)
        monkeypatch.delenv("DYDX_ADDRESS", raising=False)
        monkeypatch.delenv("DYDX_ENABLE_LIVE", raising=False)
        assert _fresh().fetch_usdt_account_balances() == {"spot": 0.0, "futures": 0.0}

    def test_positions_no_creds(self, monkeypatch):
        monkeypatch.delenv("DYDX_MNEMONIC", raising=False)
        monkeypatch.delenv("DYDX_ADDRESS", raising=False)
        monkeypatch.delenv("DYDX_ENABLE_LIVE", raising=False)
        assert _fresh().fetch_futures_positions() == []

    def test_creds_without_live_flag_still_gated(self, monkeypatch):
        monkeypatch.setenv("DYDX_MNEMONIC", "word " * 24)
        monkeypatch.setenv("DYDX_ADDRESS", "dydx1abc")
        monkeypatch.delenv("DYDX_ENABLE_LIVE", raising=False)
        v = _fresh()
        assert v.fetch_usdt_account_balances() == {"spot": 0.0, "futures": 0.0}
        assert v.fetch_futures_positions() == []


class TestExecution:
    def test_dry_run_simulated(self):
        v = _fresh()
        trades = [{"symbol": "BTCUSDT", "type": "open_long", "amount_base": 0.01}]
        market = {"BTCUSDT": {"price": 64000.0}}
        results = v.execute_trades(trades, market, dry_run=True)
        assert results[0]["status"] == "simulated"
        assert results[0]["venue"] == "dydx"
        assert results[0]["exec_price"] == 64000.0
        assert results[0]["exec_qty"] == 0.01
        assert results[0]["error"] is None

    def test_unknown_type_fails(self):
        v = _fresh()
        results = v.execute_trades(
            [{"symbol": "BTCUSDT", "type": "rebalance", "amount_base": 1}],
            {"BTCUSDT": {"price": 1.0}},
            dry_run=True,
        )
        assert results[0]["status"] == "failed"
        assert "Unknown trade type" in results[0]["error"]

    def test_live_without_optin_fails_with_guidance(self, monkeypatch):
        monkeypatch.delenv("DYDX_ENABLE_LIVE", raising=False)
        v = _fresh()
        results = v.execute_trades(
            [{"symbol": "BTCUSDT", "type": "open_long", "amount_base": 0.01}],
            {"BTCUSDT": {"price": 64000.0}},
            dry_run=False,
        )
        assert results[0]["status"] == "failed"
        assert "DYDX_ENABLE_LIVE" in results[0]["error"]

    def test_live_optin_without_creds_fails(self, monkeypatch):
        monkeypatch.setenv("DYDX_ENABLE_LIVE", "1")
        monkeypatch.delenv("DYDX_MNEMONIC", raising=False)
        monkeypatch.delenv("DYDX_ADDRESS", raising=False)
        v = _fresh()
        # _ensure_wallet hits _ensure_sdk first; stub it out so the test
        # doesn't require the real dydx-v4-client wheel.
        with (
            patch.object(dydx_mod, "_ensure_sdk"),
            patch.object(dydx_mod, "_network_module", object()),
            patch.object(dydx_mod, "_node_client_cls", object),
            patch.object(dydx_mod, "_wallet_cls", object),
        ):
            results = v.execute_trades(
                [{"symbol": "BTCUSDT", "type": "open_long", "amount_base": 0.01}],
                {"BTCUSDT": {"price": 64000.0}},
                dry_run=False,
            )
        assert results[0]["status"] == "failed"
        assert "DYDX_MNEMONIC" in results[0]["error"]


class TestFundingIndexMid:
    def test_fetch_current_with_index_mid(self):
        from venues.dydx_funding import DydxFundingProvider

        payload = {
            "markets": {
                "BTC-USD": {
                    "ticker": "BTC-USD",
                    "status": "ACTIVE",
                    "oraclePrice": "64000.5",
                    "nextFundingRate": "0.0000125",
                }
            }
        }
        import venues.dydx_funding as df

        with (
            patch.object(DydxFundingProvider, "_get", return_value=payload),
            patch.object(df, "_orderbook_mid", return_value=64010.0),
        ):
            cur = DydxFundingProvider().fetch_current("BTCUSDT", include_index_mid=True)
        assert cur["mark_price"] == 64000.5
        assert cur["index_price"] == 64010.0  # mid ≠ oracle → basis available

    def test_fetch_current_mid_fallback_to_oracle(self):
        from venues.dydx_funding import DydxFundingProvider

        payload = {
            "markets": {
                "BTC-USD": {
                    "ticker": "BTC-USD",
                    "status": "ACTIVE",
                    "oraclePrice": "64000.5",
                    "nextFundingRate": "0.0000125",
                }
            }
        }
        import venues.dydx_funding as df

        with (
            patch.object(DydxFundingProvider, "_get", return_value=payload),
            patch.object(df, "_orderbook_mid", return_value=0.0),
        ):
            cur = DydxFundingProvider().fetch_current("BTCUSDT", include_index_mid=True)
        assert cur["index_price"] == 64000.5  # book empty → oracle fallback

    def test_fetch_all_mid_enrichment_env_gated(self, monkeypatch):
        from venues.dydx_funding import DydxFundingProvider

        payload = {
            "markets": {
                "BTC-USD": {
                    "ticker": "BTC-USD",
                    "status": "ACTIVE",
                    "oraclePrice": "64000.5",
                    "nextFundingRate": "0.0000125",
                },
                # not in the default mid-bases whitelist → never enriched
                "ZZZ-USD": {
                    "ticker": "ZZZ-USD",
                    "status": "ACTIVE",
                    "oraclePrice": "1.0",
                    "nextFundingRate": "0",
                },
            }
        }
        import venues.dydx_funding as df

        # Disabled (default): index stays oracle.
        monkeypatch.delenv("DYDX_INDEX_MID", raising=False)
        with patch.object(DydxFundingProvider, "_get", return_value=payload):
            rows = DydxFundingProvider().fetch_all()
        by_sym = {r["symbol"]: r for r in rows}
        assert by_sym["BTCUSDT"]["index_price"] == 64000.5

        # Enabled: BTC gets the mid, ZZZ (off-whitelist) keeps oracle.
        monkeypatch.setenv("DYDX_INDEX_MID", "1")
        with (
            patch.object(DydxFundingProvider, "_get", return_value=payload),
            patch.object(df, "_orderbook_mid", return_value=64010.0),
        ):
            rows = DydxFundingProvider().fetch_all()
        by_sym = {r["symbol"]: r for r in rows}
        assert by_sym["BTCUSDT"]["index_price"] == 64010.0
        assert by_sym["BTCUSDT"]["mark_price"] == 64000.5
        assert by_sym["ZZZUSDT"]["index_price"] == 1.0


class TestRegistration:
    def test_get_venue(self):
        from venues import get_venue, supported_venues

        assert "dydx" in supported_venues()
        v = get_venue({"venue": {"type": "dydx"}})
        assert v.venue_id == "dydx"


class TestLiveExecution:
    """Test the live order submission path with mocked SDK components."""

    def test_side_mapping_open_long_is_buy(self, monkeypatch):
        """open_long and close_short should map to BUY side."""
        assert _side_from_type("open_long") == "BUY"
        assert _side_from_type("close_short") == "BUY"

    def test_side_mapping_open_short_is_sell(self, monkeypatch):
        """open_short and close_long should map to SELL side."""
        assert _side_from_type("open_short") == "SELL"
        assert _side_from_type("close_long") == "SELL"

    def test_live_order_success_path(self, monkeypatch):
        """Full happy path: wallet → build → sign → broadcast."""
        monkeypatch.setenv("DYDX_ENABLE_LIVE", "1")
        monkeypatch.setenv("DYDX_MNEMONIC", "word " * 24)
        monkeypatch.setenv("DYDX_ADDRESS", "dydx1test")

        v = _fresh()

        # Mock SDK components
        mock_wallet = type("W", (), {"address": "dydx1test"})()
        mock_node = type("N", (), {})()
        v._wallet = mock_wallet
        v._node = mock_node

        # Mock _submit_market_order to return success
        mock_result = {
            "order_id": "BTC-USD-12345",
            "tx_hash": "ABC123",
            "latency_ms": 150,
        }

        with patch.object(dydx_mod, "_submit_market_order", return_value=mock_result):
            results = v.execute_trades(
                [{"symbol": "BTCUSDT", "type": "open_long", "amount_base": 0.01}],
                {"BTCUSDT": {"price": 64000.0}},
                dry_run=False,
            )
        assert len(results) == 1
        r = results[0]
        assert r["status"] == "submitted"
        assert r["order_id"] == "BTC-USD-12345"
        assert r["tx_hash"] == "ABC123"
        assert r["exec_qty"] == 0.01
        assert r["venue"] == "dydx"
        assert r["error"] is None

    def test_live_order_sdk_error_returns_failed(self, monkeypatch):
        """SDK raises on broadcast → record.status == 'failed'."""
        monkeypatch.setenv("DYDX_ENABLE_LIVE", "1")
        monkeypatch.setenv("DYDX_MNEMONIC", "word " * 24)
        monkeypatch.setenv("DYDX_ADDRESS", "dydx1test")

        v = _fresh()
        mock_wallet = type("W", (), {"address": "dydx1test"})()
        v._wallet = mock_wallet
        v._node = type("N", (), {})()

        with patch.object(
            dydx_mod,
            "_submit_market_order",
            side_effect=RuntimeError("insufficient margin"),
        ):
            results = v.execute_trades(
                [{"symbol": "BTCUSDT", "type": "open_long", "amount_base": 0.01}],
                {"BTCUSDT": {"price": 64000.0}},
                dry_run=False,
            )
        assert results[0]["status"] == "failed"
        assert "insufficient margin" in results[0]["error"]

    def test_live_order_no_market_meta(self, monkeypatch):
        """Missing market metadata → failed."""
        monkeypatch.setenv("DYDX_ENABLE_LIVE", "1")
        monkeypatch.setenv("DYDX_MNEMONIC", "word " * 24)
        monkeypatch.setenv("DYDX_ADDRESS", "dydx1test")

        v = _fresh()
        v._wallet = type("W", (), {"address": "dydx1test"})()
        v._node = type("N", (), {})()

        results = v.execute_trades(
            [{"symbol": "ZZZUSDT", "type": "open_long", "amount_base": 0.01}],
            {"ZZZUSDT": {"price": 1.0}},
            dry_run=False,
        )
        assert results[0]["status"] == "failed"
        assert "no market metadata" in results[0]["error"]


# ---------------------------------------------------------------------------
# Live order path with fully mocked SDK components (plan: TestLiveExecution)
# ---------------------------------------------------------------------------


class _FakeOrderPb:
    class Order:
        SIDE_BUY = "SIDE_BUY"
        SIDE_SELL = "SIDE_SELL"


class _FakeMarket:
    """Records every order built via the SDK Market helper."""

    last_order: dict | None = None
    last_order_id: dict | None = None

    def __init__(self, market: dict):
        self.market_info = market

    def order_id(self, address, subaccount, client_id, flags):
        _FakeMarket.last_order_id = {
            "address": address,
            "subaccount": subaccount,
            "client_id": client_id,
            "flags": flags,
        }
        return _FakeMarket.last_order_id

    def order(self, **kwargs):
        _FakeMarket.last_order = dict(kwargs)
        return dict(kwargs)


class _FakeFlags:
    SHORT_TERM = "SHORT_TERM"


class _FakeOrderType:
    MARKET = "MARKET"


class _FakeNode:
    """latest_block_height=100, place_order returns a tx_response shape."""

    def __init__(self, fail: bool = False):
        self.fail = fail

    async def latest_block_height(self):
        return 100

    async def place_order(self, wallet, order):
        if self.fail:
            raise RuntimeError("broadcast rejected: insufficient margin")
        tx_response = type("TxResponse", (), {"txhash": "TESTTXHASH123"})()
        return type("Result", (), {"tx_response": tx_response})()


class _FakeWallet:
    address = "dydx1testaddress"


def _install_fake_sdk(monkeypatch, node=None):
    """Point the module-level SDK globals at fakes; return the node."""
    _FakeMarket.last_order = None
    _FakeMarket.last_order_id = None
    monkeypatch.setattr(dydx_mod, "_market_cls", _FakeMarket)
    monkeypatch.setattr(dydx_mod, "_order_flags_cls", _FakeFlags)
    monkeypatch.setattr(dydx_mod, "_order_type_cls", _FakeOrderType)
    monkeypatch.setattr(dydx_mod, "_order_pb2_module", _FakeOrderPb)
    fake_node = node or _FakeNode()
    monkeypatch.setattr(
        dydx_mod.DydxVenue,
        "_ensure_wallet",
        lambda self: (_FakeWallet(), fake_node),
    )
    return fake_node


def _oracle(price: float = 100.0):
    """Patch DydxFundingProvider.fetch_current to a fixed oracle price."""
    return patch.object(
        dydx_mod.DydxFundingProvider,
        "fetch_current",
        lambda self, symbol: {"mark_price": price},
    )


class TestLiveExecution:
    def test_build_order_buy_side(self, monkeypatch):
        """open_long → SIDE_BUY with +0.5% slippage buffer; good_til = height+20."""
        monkeypatch.setenv("DYDX_ENABLE_LIVE", "1")
        _install_fake_sdk(monkeypatch)
        v = _fresh()
        with _oracle(100.0):
            res = v.execute_trades(
                [{"symbol": "BTCUSDT", "type": "open_long", "amount_base": 0.5}],
                {"BTCUSDT": {"price": 100.0}},
                dry_run=False,
            )
        assert res[0]["status"] == "submitted"
        order = _FakeMarket.last_order
        assert order is not None
        assert order["side"] == _FakeOrderPb.Order.SIDE_BUY
        assert order["price"] == 100.0 * 1.005  # slippage buffer up
        assert order["size"] == 0.5
        assert order["order_type"] == _FakeOrderType.MARKET
        assert order["good_til_block"] == 120  # height 100 + 20
        assert order["reduce_only"] is False
        assert _FakeMarket.last_order_id["address"] == "dydx1testaddress"

    def test_build_order_sell_side(self, monkeypatch):
        """open_short → SIDE_SELL with -0.5% slippage buffer."""
        monkeypatch.setenv("DYDX_ENABLE_LIVE", "1")
        _install_fake_sdk(monkeypatch)
        v = _fresh()
        with _oracle(200.0):
            res = v.execute_trades(
                [{"symbol": "BTCUSDT", "type": "open_short", "amount_base": 1.0}],
                {"BTCUSDT": {"price": 200.0}},
                dry_run=False,
            )
        assert res[0]["status"] == "submitted"
        order = _FakeMarket.last_order
        assert order["side"] == _FakeOrderPb.Order.SIDE_SELL
        assert order["price"] == 200.0 * 0.995  # slippage buffer down

    def test_live_order_success(self, monkeypatch):
        """Full happy path: wallet → build → sign → broadcast → record."""
        monkeypatch.setenv("DYDX_ENABLE_LIVE", "1")
        _install_fake_sdk(monkeypatch)
        v = _fresh()
        with _oracle(100.0):
            res = v.execute_trades(
                [{"symbol": "BTCUSDT", "type": "open_long", "amount_base": 0.01}],
                {"BTCUSDT": {"price": 100.0}},
                dry_run=False,
            )
        r = res[0]
        assert r["status"] == "submitted"
        assert r["venue"] == "dydx"
        assert r["order_id"].startswith("BTC-USD-")
        assert r["tx_hash"] == "TESTTXHASH123"
        assert r["exec_qty"] == 0.01
        assert r["error"] is None
        assert r["latency_ms"] >= 0

    def test_live_order_sdk_error(self, monkeypatch):
        """SDK raises on broadcast → record.status == 'failed', error surfaced."""
        monkeypatch.setenv("DYDX_ENABLE_LIVE", "1")
        _install_fake_sdk(monkeypatch, node=_FakeNode(fail=True))
        v = _fresh()
        with _oracle(100.0):
            res = v.execute_trades(
                [{"symbol": "BTCUSDT", "type": "open_long", "amount_base": 0.01}],
                {"BTCUSDT": {"price": 100.0}},
                dry_run=False,
            )
        r = res[0]
        assert r["status"] == "failed"
        assert r["order_id"] is None
        assert "broadcast rejected" in r["error"]

    def test_live_order_no_oracle_price(self, monkeypatch):
        """Indexer returns no oracle price → builder refuses (never price=0)."""
        monkeypatch.setenv("DYDX_ENABLE_LIVE", "1")
        _install_fake_sdk(monkeypatch)
        v = _fresh()
        with _oracle(0.0):
            res = v.execute_trades(
                [{"symbol": "BTCUSDT", "type": "open_long", "amount_base": 0.01}],
                {"BTCUSDT": {"price": 100.0}},
                dry_run=False,
            )
        assert res[0]["status"] == "failed"
        assert "no oracle price" in res[0]["error"]
        assert _FakeMarket.last_order is None  # never built
