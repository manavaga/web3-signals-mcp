"""
Self-learning weight optimizer for signal fusion.

Adjusts dimension weights based on rolling per-dimension accuracy.
Better-performing dimensions get higher weights; poorly-performing ones get less.

All config lives in YAML (learning section). This module only does generic math:
  - Load per-dimension accuracy from kv_json
  - Compute accuracy-proportional weights with bounds
  - Apply EMA blending with previous weights
  - Store updated weights back to kv_json

Integration:
  - server.py calls optimizer after accuracy evaluation
  - engine.py loads learned weights at fuse() time, overriding YAML defaults
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class WeightOptimizer:
    """Accuracy-proportional weight optimizer with EMA blending."""

    def __init__(self, store, profile: Dict[str, Any]) -> None:
        self.store = store
        self.profile = profile
        self.cfg = profile.get("learning", {})
        self.enabled = self.cfg.get("enabled", False)
        self.namespace = self.cfg.get("state_namespace", "learning_optimizer")
        self.all_roles = ["whale", "technical", "derivatives", "narrative", "market"]

    def is_enabled(self) -> bool:
        return self.enabled

    def should_optimize(self) -> bool:
        """Check if enough new evaluations have accumulated since last optimization."""
        if not self.enabled:
            return False

        optimize_every = int(self.cfg.get("optimize_every_n_evals", 8))

        # Load last optimization state
        state = self.store.load_kv_json(self.namespace, "optimizer_state")
        last_eval_count = state.get("last_eval_count", 0) if state else 0

        # Get current eval count
        current_count = self._get_eval_count()

        new_evals = current_count - last_eval_count
        logger.info(f"WeightOptimizer: {new_evals} new evals since last optimize "
                     f"(need {optimize_every})")
        return new_evals >= optimize_every

    def compute_and_apply(self) -> Optional[Dict[str, float]]:
        """Compute optimal weights and apply them. Returns new weights or None."""
        if not self.enabled:
            return None

        # Load per-dimension accuracy
        dim_accuracy = self._load_dimension_accuracy()
        if not dim_accuracy:
            logger.warning("WeightOptimizer: no dimension accuracy data available")
            return None

        min_evals = int(self.cfg.get("min_evaluations", 20))
        total_evals = sum(d.get("count", 0) for d in dim_accuracy.values())
        if total_evals < min_evals:
            logger.info(f"WeightOptimizer: only {total_evals} evals, need {min_evals}")
            return None

        # Compute accuracy-proportional weights
        new_weights = self._compute_optimal_weights(dim_accuracy)
        if new_weights is None:
            return None

        # EMA blend with previous weights
        blended = self._ema_blend(new_weights)

        # Save and return
        self._save_weights(blended)
        self._save_state()

        logger.info(f"WeightOptimizer: updated weights: {blended}")
        return blended

    def get_current_weights(self) -> Optional[Dict[str, float]]:
        """Load the most recently optimized weights, or None if never optimized."""
        data = self.store.load_kv_json(self.namespace, "learned_weights")
        if data and "weights" in data:
            return data["weights"]
        return None

    def record_dimension_accuracy(self, dimension_scores: Dict[str, Dict[str, Any]]) -> None:
        """
        Record per-dimension accuracy data after an evaluation.

        dimension_scores: {
            "whale": {"score": 65.2, "direction": "bullish", "gradient_score": 0.7},
            "technical": {"score": 42.1, "direction": "bearish", "gradient_score": 1.0},
            ...
        }
        """
        if not self.enabled:
            return

        # Load existing running stats
        existing = self.store.load_kv_json(self.namespace, "dimension_accuracy") or {}

        for role, data in dimension_scores.items():
            gs = data.get("gradient_score")
            if gs is None:
                continue

            role_stats = existing.get(role, {"sum": 0.0, "count": 0})
            role_stats["sum"] = role_stats.get("sum", 0.0) + float(gs)
            role_stats["count"] = role_stats.get("count", 0) + 1
            existing[role] = role_stats

        self.store.save_kv_json(self.namespace, "dimension_accuracy", existing)

    # ------------------------------------------------------------------ #
    #  Internal methods
    # ------------------------------------------------------------------ #

    def _load_dimension_accuracy(self) -> Optional[Dict[str, Dict[str, float]]]:
        """Load accumulated per-dimension accuracy stats."""
        data = self.store.load_kv_json(self.namespace, "dimension_accuracy")
        if not data:
            return None

        result = {}
        for role in self.all_roles:
            stats = data.get(role, {})
            count = stats.get("count", 0)
            if count > 0:
                avg = stats["sum"] / count
                result[role] = {"accuracy": avg, "count": count}

        return result if result else None

    def _compute_optimal_weights(self, dim_accuracy: Dict[str, Dict[str, float]]) -> Optional[Dict[str, float]]:
        """
        Compute weights proportional to dimension accuracy.

        Method: accuracy_proportional
        - weight_i = accuracy_i / sum(all accuracies)
        - Bounded by min_weight and max_weight
        - Redistributes any excess/deficit proportionally
        """
        method = self.cfg.get("method", "accuracy_proportional")
        min_weight = float(self.cfg.get("min_weight", 0.05))
        max_weight = float(self.cfg.get("max_weight", 0.40))
        fallback = self.cfg.get("fallback_weights", {})

        if method != "accuracy_proportional":
            logger.warning(f"WeightOptimizer: unknown method '{method}'")
            return None

        # Collect accuracies for all roles
        accuracies: Dict[str, float] = {}
        for role in self.all_roles:
            if role in dim_accuracy:
                accuracies[role] = max(dim_accuracy[role]["accuracy"], 0.01)  # floor at 1%
            else:
                # Use fallback weight as proxy
                accuracies[role] = float(fallback.get(role, 0.2))

        total_acc = sum(accuracies.values())
        if total_acc <= 0:
            return None

        # Raw proportional weights
        raw: Dict[str, float] = {}
        for role in self.all_roles:
            raw[role] = accuracies[role] / total_acc

        # Apply bounds with redistribution
        bounded = self._apply_bounds(raw, min_weight, max_weight)
        return bounded

    def _apply_bounds(self, raw: Dict[str, float],
                       min_w: float, max_w: float) -> Dict[str, float]:
        """Apply min/max bounds and redistribute to sum to 1.0."""
        result = dict(raw)

        # Iterative bounding (max 10 rounds)
        for _ in range(10):
            excess = 0.0
            deficit = 0.0
            unbounded = []

            for role in self.all_roles:
                if result[role] < min_w:
                    deficit += min_w - result[role]
                    result[role] = min_w
                elif result[role] > max_w:
                    excess += result[role] - max_w
                    result[role] = max_w
                else:
                    unbounded.append(role)

            # Redistribute excess/deficit
            if unbounded and (excess > 0 or deficit > 0):
                net = excess - deficit
                unbounded_sum = sum(result[r] for r in unbounded)
                if unbounded_sum > 0 and net != 0:
                    for role in unbounded:
                        result[role] += net * (result[role] / unbounded_sum)

            # Check if sum is ~1.0
            total = sum(result.values())
            if abs(total - 1.0) < 0.001:
                break
            # Normalize
            for role in self.all_roles:
                result[role] /= total

        # Final normalize to exactly 1.0
        total = sum(result.values())
        if total > 0:
            for role in self.all_roles:
                result[role] = round(result[role] / total, 4)

        # Ensure sum is exactly 1.0 (fix rounding)
        diff = 1.0 - sum(result.values())
        if abs(diff) > 0.0001:
            # Add diff to largest weight
            max_role = max(result, key=result.get)
            result[max_role] = round(result[max_role] + diff, 4)

        return result

    def _ema_blend(self, new_weights: Dict[str, float]) -> Dict[str, float]:
        """Blend new weights with previous using EMA (exponential moving average)."""
        learning_rate = float(self.cfg.get("learning_rate", 0.3))

        # Load previous weights
        prev_data = self.store.load_kv_json(self.namespace, "learned_weights")
        if prev_data and "weights" in prev_data:
            prev = prev_data["weights"]
        else:
            # Use fallback weights as starting point
            prev = self.cfg.get("fallback_weights", {})
            if not prev:
                prev = dict(self.profile.get("weights", {}))

        blended: Dict[str, float] = {}
        for role in self.all_roles:
            old_w = float(prev.get(role, 0.2))
            new_w = float(new_weights.get(role, 0.2))
            # EMA: blended = learning_rate * new + (1 - learning_rate) * old
            blended[role] = round(learning_rate * new_w + (1 - learning_rate) * old_w, 4)

        # Normalize to sum to 1.0
        total = sum(blended.values())
        if total > 0:
            for role in self.all_roles:
                blended[role] = round(blended[role] / total, 4)

        # Fix rounding
        diff = 1.0 - sum(blended.values())
        if abs(diff) > 0.0001:
            max_role = max(blended, key=blended.get)
            blended[max_role] = round(blended[max_role] + diff, 4)

        return blended

    def _save_weights(self, weights: Dict[str, float]) -> None:
        """Save learned weights to kv_json."""
        self.store.save_kv_json(self.namespace, "learned_weights", {
            "weights": weights,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    def _save_state(self) -> None:
        """Save optimizer state (eval count, timestamp)."""
        current_count = self._get_eval_count()
        self.store.save_kv_json(self.namespace, "optimizer_state", {
            "last_eval_count": current_count,
            "last_optimized_at": datetime.now(timezone.utc).isoformat(),
        })

    def _get_eval_count(self) -> int:
        """Get total evaluation count from dimension_accuracy stats."""
        data = self.store.load_kv_json(self.namespace, "dimension_accuracy")
        if not data:
            return 0
        # Use max count across dimensions
        counts = [d.get("count", 0) for d in data.values() if isinstance(d, dict)]
        return max(counts) if counts else 0
