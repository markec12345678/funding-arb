from execution.pure_futures_risk_controller import decide_position
from core.pure_futures_risk_engine import RiskThresholds


BASE = {
    "direction": "forward",
    "qty": 1,
    "trade_usd": 1000,
    "long_price": 100,
    "short_price": 101,
}


def test_safe_position_holds():
    d = decide_position(
        position=BASE,
        current_long_price=100.2,
        current_short_price=100.8,
        current_funding_spread_pct=0.05,
        held_hours=8,
        margin_distances_pct=[40, 45],
        thresholds=RiskThresholds(),
    )
    assert d.action == "HOLD"
    assert d.state == "SAFE"
    assert d.snapshot.estimated_funding_usd == 0.5


def test_margin_warning_still_holds():
    d = decide_position(
        position=BASE,
        current_long_price=100,
        current_short_price=101,
        current_funding_spread_pct=0.05,
        held_hours=8,
        margin_distances_pct=[25, 40],
    )
    assert d.action == "HOLD"
    assert d.state == "WARNING"


def test_reduce_on_notional_skew():
    d = decide_position(
        position=BASE,
        current_long_price=100,
        current_short_price=101,
        current_funding_spread_pct=0.05,
        held_hours=8,
        notional_skew_pct=2.0,
    )
    assert d.action == "REDUCE"
    assert d.state == "REDUCE"


def test_close_on_emergency_margin():
    d = decide_position(
        position=BASE,
        current_long_price=100,
        current_short_price=101,
        current_funding_spread_pct=0.05,
        held_hours=8,
        margin_distances_pct=[8, 50],
    )
    assert d.action == "CLOSE"
    assert d.state == "EMERGENCY"
