from core.pure_futures_risk_engine import (
    RiskState,
    RiskThresholds,
    build_risk_snapshot,
    estimate_funding_usd,
    estimate_price_spread_pnl,
    estimate_round_trip_fees_usd,
    classify_risk,
)


def test_funding_uses_actual_interval():
    assert estimate_funding_usd(1000, 0.05, 24, 8) == 1.5
    assert estimate_funding_usd(1000, 0.05, 24, 1) == 12.0


def test_forward_spread_convergence_is_profit():
    assert estimate_price_spread_pnl("forward", 1, 100, 101, 100.4, 100.6) == 0.8


def test_reverse_spread_convergence_is_loss():
    assert estimate_price_spread_pnl("reverse", 1, 101, 100, 100.6, 100.4) == -0.8


def test_round_trip_fee_covers_four_fills():
    assert estimate_round_trip_fees_usd(1000, 0.05, 0.05) == 2.0


def test_risk_state_escalates_to_emergency_for_close_liquidation():
    state, reasons = classify_risk(
        margin_distances_pct=[8.0, 35.0],
        notional_skew_pct=0.2,
        net_pnl_usd=2,
        funding_usd=3,
    )
    assert state is RiskState.EMERGENCY
    assert reasons


def test_risk_state_reduces_for_skew():
    state, _ = classify_risk(
        margin_distances_pct=[50.0, 50.0],
        notional_skew_pct=2.0,
    )
    assert state is RiskState.REDUCE


def test_snapshot_combines_funding_price_and_fees():
    snapshot = build_risk_snapshot(
        direction="forward",
        qty=1,
        trade_usd=1000,
        entry_long_price=100,
        entry_short_price=101,
        current_long_price=100.2,
        current_short_price=100.8,
        held_hours=8,
        funding_spread_pct=0.05,
        long_taker_fee_pct=0.05,
        short_taker_fee_pct=0.05,
        margin_distances_pct=[40.0, 45.0],
        thresholds=RiskThresholds(),
    )
    assert snapshot.price_spread_pnl_usd == 0.4
    assert snapshot.estimated_funding_usd == 0.5
    assert snapshot.estimated_fees_usd == 2.0
    assert snapshot.estimated_net_pnl_usd == -1.1
    assert snapshot.state == "SAFE"
