# scoring/types.py
"""Frozen dataclass contracts between all modules.

Every module communicates through these types — no Dict[str, Any] at boundaries.
"""
from __future__ import annotations
from dataclasses import dataclass, field, fields
from typing import Optional


@dataclass(frozen=True)
class DimensionScore:
    """One agent's score for one asset."""
    name: str           # "technical", "derivatives", "market"
    score: float        # 0-100, clamped
    detail: str         # human-readable explanation
    tier: str           # "full", "partial", "none"

    def __post_init__(self):
        object.__setattr__(self, "score", max(0.0, min(100.0, self.score)))


@dataclass(frozen=True)
class RegimeContext:
    """Market regime snapshot at signal time."""
    regime: str              # "trending", "ranging", "unknown"
    fg_value: int            # raw Fear & Greed (0-100)
    fg_regime: str           # "extreme_fear", "fear", "neutral", "greed", "extreme_greed"
    btc_pct_from_ma30: float # absolute distance from MA30 as decimal (0.08 = 8%)


@dataclass(frozen=True)
class TargetLevels:
    """Entry, target, and stop-loss for a directional signal."""
    entry_price: float
    target_price: float
    stop_loss: float
    risk_reward_ratio: float
    predicted_move_pct: float
    confidence: str          # "high", "medium", "low"
    timeframe_hours: int     # typically 48


@dataclass(frozen=True)
class Signal:
    """Complete fused signal for one asset."""
    asset: str
    composite: float                          # 0-100
    label: str                                # STRONG BUY / MODERATE BUY / NEUTRAL / etc.
    direction: str                            # "bullish", "bearish", "neutral"
    dimensions: dict[str, DimensionScore]     # keyed by dimension name
    weights_used: dict[str, float]            # keyed by dimension name, sums to 1.0
    regime: RegimeContext
    targets: Optional[TargetLevels]           # None for NEUTRAL / INSUFFICIENT EDGE
    momentum: str                             # "improving", "degrading", "stable"
    abstained: bool                           # True if INSUFFICIENT EDGE

    def to_dict(self) -> dict:
        """Serialize for API response."""
        d = {
            "asset": self.asset,
            "composite": round(self.composite, 2),
            "label": self.label,
            "direction": self.direction,
            "dimensions": {
                name: {"score": round(ds.score, 2), "detail": ds.detail, "tier": ds.tier}
                for name, ds in self.dimensions.items()
            },
            "weights_used": {k: round(v, 4) for k, v in self.weights_used.items()},
            "regime": {
                "regime": self.regime.regime,
                "fg_value": self.regime.fg_value,
                "fg_regime": self.regime.fg_regime,
            },
            "momentum": self.momentum,
            "abstained": self.abstained,
        }
        if self.targets:
            d["targets"] = {
                "entry_price": self.targets.entry_price,
                "target_price": self.targets.target_price,
                "stop_loss": self.targets.stop_loss,
                "risk_reward_ratio": round(self.targets.risk_reward_ratio, 2),
                "predicted_move_pct": round(self.targets.predicted_move_pct, 2),
                "confidence": self.targets.confidence,
                "timeframe_hours": self.targets.timeframe_hours,
            }
        return d
