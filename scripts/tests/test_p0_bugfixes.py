#!/usr/bin/env python3
"""P0 bug-fix regression tests (Task B-3).

Covers:
1. Binance bulk futures tickers: "/fapi/" path form — absolute URL passed as
   `path` was double-prefixed into an invalid URL and silently swallowed.
2. OKX prepare_for_withdraw: internal transfer Trading(18) -> Funding(6);
   the old body sent 18 -> 18 (trading -> trading no-op).
3. Credentials: ASTER_/LIGHTER_/DYDX_/TRADE_SIGNER_/FARB_ prefixes recognized
   by ensure_env() (previously silently dropped from the JSON backend).
4. Hyperliquid trade-signer monkey-patch: installed exactly once (idempotent,
   thread-safe), resolves TRADE_SIGNER_URL / TRADE_SIGNER_API_TOKEN from
   os.environ at call time, and falls back to the original local sign_inner
   when the signer env is cleared.
5. dYdX live gate: DYDX_ENABLE_LIVE=1 opt-in intact (read-only confirmation).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import types
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Bug 1 — Binance bulk futures tickers
# ---------------------------------------------------------------------------


class _FakeUrlopenResponse:
    """Minimal stand-in for the urlopen() context manager."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeUrlopenResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _install_fake_urlopen(monkeypatch, rows: list[dict], captured: list[str]) -> None:
    body = json.dumps(rows).encode()

    def fake_urlopen(req, timeout=None):
        captured.append(req.full_url)
        return _FakeUrlopenResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_binance_bulk_futures_tickers_uses_fapi_path(monkeypatch):
    import venues.binance as bnb

    monkeypatch.setattr(bnb, "_futures_ticker_loaded_at", 0.0)
    monkeypatch.setattr(bnb, "_futures_ticker_prices", {})

    captured: list[str] = []
    _install_fake_urlopen(
        monkeypatch,
        [
            {"symbol": "BTCUSDT", "price": "42000.0"},
            {"symbol": "ETHUSDT", "price": "3000.0"},
        ],
        captured,
    )

    out = bnb.BinanceSpotVenue().get_all_futures_tickers(cache_sec=0)

    assert len(captured) == 1, "single successful GET, no retry storm"
    url = captured[0]
    # Pre-fix failure mode: absolute URL as `path` produced the double-prefixed
    # "https://api.binance.comhttps://fapi.binance.com/..." which always failed
    # and was swallowed by the except clause (silent empty tickers forever).
    assert "binance.comhttps" not in url
    assert not url.startswith("https://api.binance.comhttps")
    assert url.startswith("https://fapi.binance.com/fapi/v1/ticker/price")
    assert out == {"BTCUSDT": 42000.0, "ETHUSDT": 3000.0}


def test_binance_single_futures_ticker_uses_fapi_path(monkeypatch):
    import venues.binance as bnb

    captured: list[str] = []
    _install_fake_urlopen(
        monkeypatch, {"symbol": "BTCUSDT", "price": "42000.0"}, captured
    )

    price = bnb.BinanceSpotVenue().get_futures_ticker("BTCUSDT")

    assert price == 42000.0
    assert captured[0].startswith("https://fapi.binance.com/fapi/v1/ticker/price")
    assert "binance.comhttps" not in captured[0]


def test_binance_bulk_spot_tickers_still_use_spot_api(monkeypatch):
    """Guard: the spot bulk endpoint (already correct) stays on api.binance.com."""
    import venues.binance as bnb

    monkeypatch.setattr(bnb, "_spot_ticker_loaded_at", 0.0)
    monkeypatch.setattr(bnb, "_spot_ticker_prices", {})

    captured: list[str] = []
    _install_fake_urlopen(
        monkeypatch, [{"symbol": "BTCUSDT", "price": "42000.0"}], captured
    )

    out = bnb.BinanceSpotVenue().get_all_spot_tickers(cache_sec=0)

    assert captured[0].startswith("https://api.binance.com/api/v3/ticker/price")
    assert "binance.comhttps" not in captured[0]
    assert out == {"BTCUSDT": 42000.0}


# ---------------------------------------------------------------------------
# Bug 2 — OKX prepare_for_withdraw internal transfer
# ---------------------------------------------------------------------------


def _fake_okx_api(calls: list[dict], balances: dict[str, str]) -> Any:
    """Fake venues.okx._api_call recording calls; returns canned balances."""

    def fake_api(method, path, params=None, body=None):
        calls.append({"method": method, "path": path, "params": params, "body": body})
        if path == "/api/v5/account/balance":
            return {"code": "0", "data": [{"details": balances.get("details", [])}]}
        if path == "/api/v5/asset/transfer":
            return {"code": "0", "data": [{"transfId": "123"}]}
        return {"code": "0", "data": []}

    return fake_api


def test_okx_prepare_for_withdraw_moves_trading_to_funding(monkeypatch):
    import venues.okx as ox
    from transfer.transfer_providers import OkxTransferProvider

    calls: list[dict] = []
    monkeypatch.setattr(
        ox,
        "_api_call",
        _fake_okx_api(calls, {"details": [{"ccy": "USDT", "availBal": "10"}]}),
    )

    steps = OkxTransferProvider().prepare_for_withdraw("USDT", 100.0)

    transfers = [c for c in calls if c["path"] == "/api/v5/asset/transfer"]
    assert len(transfers) == 1, "expected exactly one internal transfer POST"
    assert transfers[0]["method"] == "POST"
    body = transfers[0]["body"]
    # Pre-fix bug: from="18"/to="18" (trading -> trading no-op). OKX v5 account
    # codes: 6 = Funding account, 18 = Trading account.
    assert body["from"] == "18"
    assert body["to"] == "6"
    assert body["ccy"] == "USDT"
    assert body["type"] == "0"
    assert float(body["amt"]) == 90.01  # 100 - 10 avail + 0.01 buffer
    assert steps and "internal transfer" in steps[0]


def test_okx_prepare_for_withdraw_noop_when_trading_sufficient(monkeypatch):
    import venues.okx as ox
    from transfer.transfer_providers import OkxTransferProvider

    calls: list[dict] = []
    monkeypatch.setattr(
        ox,
        "_api_call",
        _fake_okx_api(calls, {"details": [{"ccy": "USDT", "availBal": "500"}]}),
    )

    steps = OkxTransferProvider().prepare_for_withdraw("USDT", 100.0)

    assert steps == []
    assert not [c for c in calls if c["path"] == "/api/v5/asset/transfer"]


# ---------------------------------------------------------------------------
# Bug 3 — credentials known-prefixes gap
# ---------------------------------------------------------------------------

_NEW_VENUE_KEYS = {
    "ASTER_API_KEY": "a1",
    "ASTER_API_SECRET": "s1",
    "LIGHTER_API_PRIVATE_KEY": "0xdead",
    "LIGHTER_ACCOUNT_INDEX": "0",
    "LIGHTER_L1_ADDRESS": "0xl1",
    "LIGHTER_API_KEY_INDEX": "2",
    "DYDX_MNEMONIC": " ".join(["word"] * 12),
    "DYDX_ADDRESS": "dydx1abc",
    "DYDX_ENABLE_LIVE": "1",
    "DYDX_INDEX_MID": "1",
    "TRADE_SIGNER_URL": "http://x",
    "TRADE_SIGNER_API_TOKEN": "tok",
    "FARB_API_TOKEN": "t",
    "FARB_ALLOW_UNAUTHENTICATED": "0",
}


def test_credentials_known_prefixes_cover_new_venues():
    import core.credentials as creds

    for key in _NEW_VENUE_KEYS:
        assert creds._is_known_key(key), f"_is_known_key({key}) should be True"
        # keyring / systemd-creds backends iterate _ALL_KEYS — the new venue
        # keys must be enumerable there too.
        assert key in creds._ALL_KEYS, f"{key} missing from _ALL_KEYS"

    # Unknown prefixes must still be filtered out.
    assert not creds._is_known_key("TOTALLY_UNKNOWN_API_KEY")
    assert not creds._is_known_key("SOMETHING_SECRET_KEY")


def test_ensure_env_loads_new_venue_credentials_from_json(tmp_path, monkeypatch):
    import core.credentials as creds

    subset = {
        "ASTER_API_KEY": "a1",
        "LIGHTER_API_PRIVATE_KEY": "0xdead",
        "DYDX_MNEMONIC": " ".join(["word"] * 12),
        "TRADE_SIGNER_URL": "http://x",
        "FARB_API_TOKEN": "t",
    }
    store = tmp_path / "credentials.json"
    store.write_text(json.dumps({"env": subset}), encoding="utf-8")

    monkeypatch.setattr(creds, "_JSON_FILES", [store])
    monkeypatch.setattr(creds, "_load_keyring", lambda: {})
    monkeypatch.setattr(creds, "_load_age", lambda: {})
    monkeypatch.setattr(creds, "_load_systemd_creds", lambda: {})
    monkeypatch.setattr(creds, "_cache", None)
    monkeypatch.setattr(creds, "_loaded", False)
    for key in subset:
        monkeypatch.delenv(key, raising=False)

    creds.ensure_env()
    try:
        for key, value in subset.items():
            assert os.environ.get(key) == value, f"{key} not loaded into os.environ"
    finally:
        for key in subset:
            os.environ.pop(key, None)


def test_ensure_env_prefix_filter_still_works_for_new_venues(tmp_path, monkeypatch):
    """ensure_env("ASTER_") should inject only the ASTER_ series."""
    import core.credentials as creds

    store = tmp_path / "credentials.json"
    store.write_text(
        json.dumps({"env": {"ASTER_API_KEY": "ak", "FARB_API_TOKEN": "ft"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(creds, "_JSON_FILES", [store])
    monkeypatch.setattr(creds, "_load_keyring", lambda: {})
    monkeypatch.setattr(creds, "_load_age", lambda: {})
    monkeypatch.setattr(creds, "_load_systemd_creds", lambda: {})
    monkeypatch.setattr(creds, "_cache", None)
    monkeypatch.setattr(creds, "_loaded", False)
    for key in ("ASTER_API_KEY", "FARB_API_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    try:
        creds.ensure_env("ASTER_")
        assert os.environ.get("ASTER_API_KEY") == "ak"
        assert os.environ.get("FARB_API_TOKEN") is None
    finally:
        os.environ.pop("ASTER_API_KEY", None)
        os.environ.pop("FARB_API_TOKEN", None)


# ---------------------------------------------------------------------------
# Bug 4 — Hyperliquid trade-signer monkey-patch safety
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_hl_signing(monkeypatch):
    """Install a fake hyperliquid.utils.signing module into sys.modules.

    hyperliquid-python-sdk is not installed in this environment, so we provide
    the exact module path that _ensure_tradesigner_patch imports. The fixture
    also resets the hyperliquid venue module's patch bookkeeping so every test
    starts from an unpatched state (monkeypatch restores both afterwards).
    """
    calls: dict[str, list] = {"original": []}

    def original_sign_inner(wallet, data):
        calls["original"].append({"wallet": wallet, "data": data})
        return {"sig": "local", "r": "0xlocal", "s": "0xlocal", "v": 27}

    signing = types.ModuleType("hyperliquid.utils.signing")
    signing.sign_inner = original_sign_inner
    utils = types.ModuleType("hyperliquid.utils")
    utils.signing = signing
    hl_pkg = types.ModuleType("hyperliquid")
    hl_pkg.utils = utils

    monkeypatch.setitem(sys.modules, "hyperliquid", hl_pkg)
    monkeypatch.setitem(sys.modules, "hyperliquid.utils", utils)
    monkeypatch.setitem(sys.modules, "hyperliquid.utils.signing", signing)

    import venues.hyperliquid as hv

    monkeypatch.setattr(hv, "_signer_patched", False)
    monkeypatch.setattr(hv, "_original_sign_inner", None)

    return types.SimpleNamespace(
        signing=signing, calls=calls, original=original_sign_inner
    )


def test_tradesigner_patch_installs_once_and_is_idempotent(fake_hl_signing):
    import venues.hyperliquid as hv

    hv._ensure_tradesigner_patch()
    assert hv._signer_patched is True
    wrapper = fake_hl_signing.signing.sign_inner
    assert wrapper is not fake_hl_signing.original, "patch was not installed"

    # Second install must be a no-op (no double-wrap of the wrapper).
    hv._ensure_tradesigner_patch()
    assert fake_hl_signing.signing.sign_inner is wrapper


def test_tradesigner_patch_falls_back_to_local_when_env_cleared(
    fake_hl_signing, monkeypatch
):
    import venues.hyperliquid as hv

    # Install while a signer URL is configured ...
    monkeypatch.setenv("TRADE_SIGNER_URL", "http://signer")
    hv._ensure_tradesigner_patch()

    # ... then clear it: the wrapper must resolve env per call (not from the
    # closure captured at patch time) and restore local signing.
    monkeypatch.delenv("TRADE_SIGNER_URL")

    def _bomb(*args, **kwargs):
        raise AssertionError("no HTTP expected in local-signing fallback")

    monkeypatch.setattr(requests, "post", _bomb)

    data = {
        "domain": {"name": "Exchange"},
        "types": {},
        "primaryType": "Action",
        "message": {"foo": 1},
    }
    out = fake_hl_signing.signing.sign_inner(None, data)

    assert out == {"sig": "local", "r": "0xlocal", "s": "0xlocal", "v": 27}
    assert fake_hl_signing.calls["original"][0]["data"] == data


def test_tradesigner_patch_delegates_to_remote_signer(fake_hl_signing, monkeypatch):
    import venues.hyperliquid as hv

    # Install with NO signer env configured ...
    monkeypatch.delenv("TRADE_SIGNER_URL", raising=False)
    monkeypatch.delenv("TRADE_SIGNER_API_TOKEN", raising=False)
    hv._ensure_tradesigner_patch()

    # ... then set the env afterwards — delegation must kick in per call.
    monkeypatch.setenv("TRADE_SIGNER_URL", "http://signer")
    monkeypatch.setenv("TRADE_SIGNER_API_TOKEN", "tok")

    posted: list[dict] = []

    class _FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"r": "0xr", "s": "0xs", "v": 28}

    def fake_post(url, json=None, headers=None, timeout=None):
        posted.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        return _FakeResp()

    monkeypatch.setattr(requests, "post", fake_post)

    data = {
        "domain": {"name": "Exchange"},
        "types": {"Action": []},
        "primaryType": "Action",
        "message": {"foo": 1},
    }
    out = fake_hl_signing.signing.sign_inner(None, data)

    assert out == {"r": "0xr", "s": "0xs", "v": 28}
    assert len(posted) == 1
    assert posted[0]["url"] == "http://signer/sign-typed-data"
    assert posted[0]["headers"]["Authorization"] == "Bearer tok"
    assert posted[0]["json"]["typedData"]["message"] == {"foo": 1}
    assert posted[0]["json"]["typedData"]["primaryType"] == "Action"
    assert posted[0]["json"]["context"]["service"] == "hyperliquid"
    assert fake_hl_signing.calls["original"] == [], "original signer must not run"


def test_tradesigner_patch_thread_safe(fake_hl_signing, monkeypatch):
    import venues.hyperliquid as hv

    monkeypatch.delenv("TRADE_SIGNER_URL", raising=False)

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait()
            hv._ensure_tradesigner_patch()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"concurrent installs raised: {errors}"
    wrapper = fake_hl_signing.signing.sign_inner
    assert wrapper is not fake_hl_signing.original

    # Exactly one wrapper layer: a single sign_inner call (env unset → local
    # fallback) must reach the original exactly once. Double-wrapping would
    # still "work" but nests wrappers and re-reads env per layer.
    fake_hl_signing.signing.sign_inner(None, {"message": {}})
    assert len(fake_hl_signing.calls["original"]) == 1


def test_make_exchange_with_tradesigner_installs_patch_and_uses_dummy_wallet(
    fake_hl_signing, monkeypatch
):
    import venues.hyperliquid as hv

    monkeypatch.setenv("TRADE_SIGNER_URL", "http://signer")
    constructed: list[tuple[Any, str]] = []

    class _FakeExchange:
        def __init__(self, wallet, base_url):
            constructed.append((wallet, base_url))

    monkeypatch.setattr(
        hv, "_sdk_cache", {"Exchange": _FakeExchange, "Info": object}
    )

    ex = hv._make_exchange_with_tradesigner(
        "https://api.hyperliquid.xyz", "0xABC", "http://signer"
    )

    assert isinstance(ex, _FakeExchange)
    wallet, base_url = constructed[0]
    assert base_url == "https://api.hyperliquid.xyz"
    assert wallet.address == "0xabc"
    assert wallet.key == "0xabc"
    # The patch was installed as a side effect (exactly once).
    assert hv._signer_patched is True
    assert fake_hl_signing.signing.sign_inner is not fake_hl_signing.original


# ---------------------------------------------------------------------------
# Bug 5 — dYdX live gate confirmation (no code change; read-only check)
# ---------------------------------------------------------------------------


def test_dydx_live_gate_requires_explicit_optin(monkeypatch):
    """B-5 confirmation: without DYDX_ENABLE_LIVE=1 live trades must fail.

    Mirrors the gate at scripts/venues/dydx.py:566-578 (_live_enabled at
    lines 134-135). The pre-live branch must never submit an order.
    """
    import venues.dydx as dydx_mod

    monkeypatch.delenv("DYDX_ENABLE_LIVE", raising=False)
    monkeypatch.setenv("DYDX_MNEMONIC", " ".join(["word"] * 12))
    monkeypatch.setenv("DYDX_ADDRESS", "dydx1abc")

    venue = dydx_mod.DydxVenue()
    results = venue.execute_trades(
        [{"symbol": "BTCUSDT", "type": "open_long", "amount_base": 0.01}],
        {"BTCUSDT": {"price": 64000.0}},
        dry_run=False,
    )

    assert len(results) == 1
    assert results[0]["status"] == "failed"
    assert results[0]["order_id"] is None
    assert "DYDX_ENABLE_LIVE" in results[0]["error"]
