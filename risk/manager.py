# risk/manager.py
"""Basic risk manager — all parameters from self-learning layer.

Provides:
  - Confidence-based position sizing (not flat %)
  - Daily loss cap (pause when losing streak)
  - Max open trades (prevent over-exposure)
  - Correlation filter (block correlated same-direction trades)

All thresholds from learned_state.json — zero hardcoded values.
Falls back to conservative defaults when no learned params available.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "base_position_pct": 10.0,
    "min_position_pct": 3.0,
    "max_position_pct": 20.0,
    "daily_loss_cap_pct": -5.0,
    "max_open_trades": 5,
    "max_correlated_trades": 3,
    "correlation_threshold": 0.7,
}

_DEFAULT_CORRELATION_GROUPS = {
    "large_cap": ["BTC", "ETH"],
    "l1_alts": ["SOL", "AVAX", "SUI", "NEAR"],
    "defi": ["UNI", "LINK", "ARB"],
    "infra": ["FIL", "LTC", "BNB"],
}


class RiskManager:
    def __init__(self, learned_params: dict | None = None):
        self._params = learned_params or {}
        self._risk = self._params.get("risk", {})

    def _get(self, key: str) -> float:
        return self._risk.get(key, _DEFAULTS.get(key, 0))

    def size_position(self, asset: str, direction: str, confidence: float) -> float:
        """Confidence-based sizing. Higher confidence = larger position."""
        base = self._get("base_position_pct")
        min_pct = self._get("min_position_pct")
        max_pct = self._get("max_position_pct")
        scaled = min_pct + (max_pct - min_pct) * max(0, min(1, confidence))
        return round(max(min_pct, min(max_pct, scaled)), 2)

    def check_daily_loss_cap(self, daily_pnl_pct: float) -> bool:
        """True if trading allowed, False if daily loss cap hit."""
        cap = self._get("daily_loss_cap_pct")
        return daily_pnl_pct > cap

    def check_max_open_trades(self, current_open: int) -> bool:
        """True if another trade is allowed."""
        max_trades = int(self._get("max_open_trades"))
        return current_open < max_trades

    def check_correlation_filter(self, asset: str, direction: str, open_trades: list[dict]) -> bool:
        """True if trade allowed, False if too many correlated same-direction trades."""
        max_corr = int(self._get("max_correlated_trades"))
        corr_groups = self._risk.get("correlation_groups", _DEFAULT_CORRELATION_GROUPS)

        asset_group = None
        for group_name, members in corr_groups.items():
            if asset in members:
                asset_group = group_name
                break

        if asset_group is None:
            return True

        group_members = corr_groups.get(asset_group, [])
        same_direction_count = sum(
            1 for t in open_trades
            if t.get("asset") in group_members and t.get("direction") == direction
        )
        return same_direction_count < max_corr
