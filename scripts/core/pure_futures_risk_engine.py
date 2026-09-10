"""Pure-futures risk and profitability calculations.

This module is deliberately side-effect free. It converts exchange snapshots and
position metadata into deterministic risk states and net-P&L estimates so the
watcher/executor can make consistent decisions without duplicating formulas.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any


class RiskState(str, Enum):
    SAFE = "SAFE"
    WARNING = "WARNING"
    REDUCE = "REDUCE"
    EMERGENCY = "EMERGENCY"


@dataclass(frozen=True)
class RiskThresholds:
    margin_warning_pct: float = 30.0
    margin_reduce_pct: float = 20.0
    margin_emergency_pct: float = 10.0
    max_notional_skew_pct: float = 1.0
    max_loss_vs_funding_mult: float = 3.0
    min_available_margin_usd: float = 0.0


@dataclass(frozen=True)
class RiskSnapshot:
    state: str
    reasons: tuple[str, ...]
    margin_distance_min_pct: float | None
    notional_skew_pct: float
    estimated_net_pnl_usd: float
    estimated_funding_usd: float
    estimated_fees_usd: float
    price_spread_pnl_usd: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def estimate_funding_usd(
    trade_usd: float,
    funding_spread_pct: float,
    held_hours: float,
    funding_interval_hours: float = 8.0,
) -> float:
    """Estimate cumulative funding income from the observed spread.

    funding_spread_pct is the long-to-short spread in percentage points per
    funding event. A positive spread means the short leg is expected to receive
    more funding than the long leg pays.
    """
    if trade_usd <= 0 or funding_interval_hours <= 0 or held_hours <= 0:
        return 0.0
    periods = held_hours / funding_interval_hours
    return trade_usd * (funding_spread_pct / 100.0) * periods


def estimate_price_spread_pnl(
    direction: str,
    qty: float,
    entry_long_price: float,
    entry_short_price: float,
    current_long_price: float,
    current_short_price: float,
) -> float:
    """Estimate convergence P&L before fees/funding.

    For forward trades, convergence of the two venue prices is profitable.
    For reverse trades the sign is inverted because the leg directions are
    inverted.
    """
    if qty <= 0:
        return 0.0
    open_spread = abs(entry_long_price - entry_short_price)
    current_spread = abs(current_long_price - current_short_price)
    sign = 1.0 if str(direction).lower() == "forward" else -1.0
    return sign * (open_spread - current_spread) * qty


def estimate_round_trip_fees_usd(
    trade_usd: float,
    long_taker_fee_pct: float,
    short_taker_fee_pct: float,
) -> float:
    """Estimate four fills: open/close on both legs."""
    if trade_usd <= 0:
        return 0.0
    return trade_usd * 2.0 * (
        max(0.0, long_taker_fee_pct) + max(0.0, short_taker_fee_pct)
    ) / 100.0


def classify_risk(
    *,
    margin_distances_pct: list[float] | tuple[float, ...] = (),
    notional_skew_pct: float = 0.0,
    available_margin_usd: float | None = None,
    net_pnl_usd: float = 0.0,
    funding_usd: float = 0.0,
    thresholds: RiskThresholds | None = None,
) -> tuple[RiskState, tuple[str, ...]]:
    """Return the highest-severity risk state and deterministic reasons."""
    t = thresholds or RiskThresholds()
    reasons: list[str] = []
    state = RiskState.SAFE

    def escalate(target: RiskState, reason: str) -> None:
        nonlocal state
        order = {
            RiskState.SAFE: 0,
            RiskState.WARNING: 1,
            RiskState.REDUCE: 2,
            RiskState.EMERGENCY: 3,
        }
        if order[target] > order[state]:
            state = target
        reasons.append(reason)

    valid_distances = [d for d in margin_distances_pct if d >= 0]
    min_distance = min(valid_distances) if valid_distances else None
    if min_distance is not None:
        if min_distance <= t.margin_emergency_pct:
            escalate(RiskState.EMERGENCY, f"margin_distance={min_distance:.2f}%<=emergency")
        elif min_distance <= t.margin_reduce_pct:
            escalate(RiskState.REDUCE, f"margin_distance={min_distance:.2f}%<=reduce")
        elif min_distance <= t.margin_warning_pct:
            escalate(RiskState.WARNING, f"margin_distance={min_distance:.2f}%<=warning")

    if notional_skew_pct > t.max_notional_skew_pct:
        escalate(RiskState.REDUCE, f"notional_skew={notional_skew_pct:.2f}%>{t.max_notional_skew_pct:.2f}%")

    if available_margin_usd is not None and available_margin_usd <= t.min_available_margin_usd:
        escalate(RiskState.EMERGENCY, f"available_margin={available_margin_usd:.2f}<=minimum")

    loss = max(0.0, -net_pnl_usd)
    funding_abs = abs(funding_usd)
    if loss > 0 and funding_abs > 0 and loss > funding_abs * t.max_loss_vs_funding_mult:
        escalate(RiskState.REDUCE, f"loss_vs_funding={loss / funding_abs:.2f}x>{t.max_loss_vs_funding_mult:.2f}x")

    return state, tuple(reasons)


def build_risk_snapshot(
    *,
    direction: str,
    qty: float,
    trade_usd: float,
    entry_long_price: float,
    entry_short_price: float,
    current_long_price: float,
    current_short_price: float,
    held_hours: float,
    funding_spread_pct: float,
    funding_interval_hours: float = 8.0,
    long_taker_fee_pct: float = 0.0,
    short_taker_fee_pct: float = 0.0,
    notional_skew_pct: float = 0.0,
    margin_distances_pct: list[float] | tuple[float, ...] = (),
    available_margin_usd: float | None = None,
    thresholds: RiskThresholds | None = None,
) -> RiskSnapshot:
    funding = estimate_funding_usd(
        trade_usd, funding_spread_pct, held_hours, funding_interval_hours
    )
    spread_pnl = estimate_price_spread_pnl(
        direction,
        qty,
        entry_long_price,
        entry_short_price,
        current_long_price,
        current_short_price,
    )
    fees = estimate_round_trip_fees_usd(
        trade_usd, long_taker_fee_pct, short_taker_fee_pct
    )
    net = spread_pnl + funding - fees
    state, reasons = classify_risk(
        margin_distances_pct=margin_distances_pct,
        notional_skew_pct=notional_skew_pct,
        available_margin_usd=available_margin_usd,
        net_pnl_usd=net,
        funding_usd=funding,
        thresholds=thresholds,
    )
    return RiskSnapshot(
        state=state.value,
        reasons=reasons,
        margin_distance_min_pct=min(margin_distances_pct) if margin_distances_pct else None,
        notional_skew_pct=notional_skew_pct,
        estimated_net_pnl_usd=net,
        estimated_funding_usd=funding,
        estimated_fees_usd=fees,
        price_spread_pnl_usd=spread_pnl,
    )
