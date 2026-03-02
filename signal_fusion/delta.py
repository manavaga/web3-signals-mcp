"""
Delta (change-detection) scorer for signal fusion.

Scores CHANGES in dimension values rather than absolute levels.
"Whale activity jumped +20 points" is more predictive than "whale is at 65."

Key concept: A dimension rapidly improving suggests a trend change that
hasn't been priced in yet. A dimension collapsing suggests a move that's
about to play out.

All thresholds and bonuses are YAML-configurable (delta_scoring section).

Integration: After computing the absolute composite score, the engine blends
the delta composite: final = absolute * absolute_weight + delta * delta_weight.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class DeltaScorer:
    """Scores dimension changes and computes a delta composite."""

    def __init__(self, profile: Dict[str, Any]) -> None:
        self.cfg = profile.get("delta_scoring", {})
        self.enabled = self.cfg.get("enabled", False)
        self.all_roles = ["whale", "technical", "derivatives", "narrative", "market"]

    def is_enabled(self) -> bool:
        return self.enabled

    def compute_delta_composite(
        self,
        asset: str,
        current_dims: Dict[str, Dict[str, Any]],
        previous_dims: Optional[Dict[str, Dict[str, Any]]],
    ) -> Tuple[Optional[float], Dict[str, Any]]:
        """
        Compute a delta-based composite score for one asset.

        Args:
            asset: Asset symbol
            current_dims: Current dimension scores {role: {score: float, ...}}
            previous_dims: Previous run dimension scores (same format) or None

        Returns:
            (delta_composite, delta_details) or (None, {}) if no previous data
        """
        if not self.enabled or previous_dims is None:
            return None, {}

        thresholds = self.cfg.get("thresholds", {})

        deltas: Dict[str, Dict[str, Any]] = {}
        delta_scores: List[float] = []

        for role in self.all_roles:
            curr = current_dims.get(role, {})
            prev = previous_dims.get(role, {})

            curr_score = curr.get("score")
            prev_score = prev.get("score")

            if curr_score is None or prev_score is None:
                continue

            change = float(curr_score) - float(prev_score)
            abs_change = abs(change)

            # Classify the change
            role_thresholds = thresholds.get(role, {})
            significant = float(role_thresholds.get("significant", 12))
            major = float(role_thresholds.get("major", 20))

            if abs_change >= major:
                magnitude = "major"
            elif abs_change >= significant:
                magnitude = "significant"
            else:
                magnitude = "minor"

            # Determine direction of change
            if change > 0:
                change_dir = "improving"
            elif change < 0:
                change_dir = "degrading"
            else:
                change_dir = "stable"

            # Detect reversal: previous was bearish (<45) and now bullish (>55) or vice versa
            is_reversal = (
                (float(prev_score) < 45 and float(curr_score) > 55) or
                (float(prev_score) > 55 and float(curr_score) < 45)
            )

            # Compute delta score for this dimension (centered at 50)
            delta_score = 50.0  # base = neutral

            if change_dir == "improving":
                if magnitude == "major":
                    delta_score += float(self.cfg.get("improving_major_bonus", 20))
                elif magnitude == "significant":
                    delta_score += float(self.cfg.get("improving_significant_bonus", 10))
                else:
                    delta_score += 3  # minor improvement
            elif change_dir == "degrading":
                if magnitude == "major":
                    delta_score += float(self.cfg.get("degrading_major_penalty", -20))
                elif magnitude == "significant":
                    delta_score += float(self.cfg.get("degrading_significant_penalty", -10))
                else:
                    delta_score -= 3  # minor degradation

            if is_reversal:
                reversal_bonus = float(self.cfg.get("reversal_bonus", 15))
                # Apply in the direction of the reversal
                if change > 0:
                    delta_score += reversal_bonus  # bullish reversal
                else:
                    delta_score -= reversal_bonus  # bearish reversal

            delta_score = max(0.0, min(100.0, delta_score))
            delta_scores.append(delta_score)

            deltas[role] = {
                "change": round(change, 1),
                "magnitude": magnitude,
                "direction": change_dir,
                "reversal": is_reversal,
                "delta_score": round(delta_score, 1),
            }

        if not delta_scores:
            return None, {}

        # Average delta scores across dimensions (equal weight for delta)
        delta_composite = sum(delta_scores) / len(delta_scores)
        delta_composite = round(max(0.0, min(100.0, delta_composite)), 1)

        return delta_composite, deltas

    def blend(self, absolute_composite: float, delta_composite: Optional[float]) -> float:
        """
        Blend absolute and delta composites.

        final = absolute * absolute_weight + delta * delta_weight
        """
        if delta_composite is None:
            return absolute_composite

        abs_weight = float(self.cfg.get("absolute_weight", 0.6))
        delta_weight = float(self.cfg.get("delta_weight", 0.4))

        blended = absolute_composite * abs_weight + delta_composite * delta_weight
        return round(max(0.0, min(100.0, blended)), 1)
