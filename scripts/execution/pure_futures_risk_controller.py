"""Watcher decision adapter for the pure-futures risk/profit engine.

This module is intentionally separate from the exchange executor. It converts a
watcher position + live market snapshot into one deterministic action:
HOLD, REDUCE, or CLOSE. Existing liquidation/leg-loss/rebalance checks remain
owned by pure_futures_watcher.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.pure_futures_risk_engine import (
    RiskSnapshot,
    RiskState,
    RiskThresholds,
    build_risk_snapshot,
)


@dataclass(frozen=True)
class RiskDecision:
    action: str
    state: str
    reason: str
    snapshot: RiskSnapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "state": self.state,
            "reason": self.reason,
            "risk": self.snapshot.to_dict(),
        }


def decide_position(
    *,
    position: dict[str, Any],
    current_long_price: float,
    current_short_price: float,
    current_funding_spread_pct: float,
    held_hours: float,
    funding_interval_hours: float = 8.0,
    long_taker_fee_pct: float = 0.0,
    short_taker_fee_pct: float = 0.0,
    margin_distances_pct: list[float] | tuple[float, ...] = (),
    available_margin_usd: float | None = None,
    notional_skew_pct: float = 0.0,
    thresholds: RiskThresholds | None = None,
) -> RiskDecision:
    """Evaluate one live position without placing an order.

    Action mapping is deliberately conservative:
      SAFE/WARNING -> HOLD
      REDUCE       -> REDUCE
      EMERGENCY    -> CLOSE
    """
    direction = str(position.get("direction", "forward"))
    qty = float(position.get("qty", 0) or 0)
    trade_usd = float(position.get("trade_usd", 0) or 0)

    snapshot = build_risk_snapshot(
        direction=direction,
        qty=qty,
        trade_usd=trade_usd,
        entry_long_price=float(position.get("long_price", 0) or 0),
        entry_short_price=float(position.get("short_price", 0) or 0),
        current_long_price=current_long_price,
        current_short_price=current_short_price,
        held_hours=held_hours,
        funding_spread_pct=current_funding_spread_pct,
        funding_interval_hours=funding_interval_hours,
        long_taker_fee_pct=long_taker_fee_pct,
        short_taker_fee_pct=short_taker_fee_pct,
        notional_skew_pct=notional_skew_pct,
        margin_distances_pct=margin_distances_pct,
        available_margin_usd=available_margin_usd,
        thresholds=thresholds,
    )

    state = RiskState(snapshot.state)
    if state is RiskState.EMERGENCY:
        action = "CLOSE"
    elif state is RiskState.REDUCE:
        action = "REDUCE"
    else:
        action = "HOLD"

    reason = "; ".join(snapshot.reasons) if snapshot.reasons else "risk_state_safe"
    return RiskDecision(action=action, state=state.value, reason=reason, snapshot=snapshot)
