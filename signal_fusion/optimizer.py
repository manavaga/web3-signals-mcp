"""
Self-learning weight optimizer for signal fusion.

Adjusts dimension weights based on Information Coefficient (IC) —
Spearman rank correlation between dimension scores and future returns.

Phase 1: Accuracy-proportional (original)
Phase 2: IC-based with auto-promote/demote and decay detection (current)

All config lives in YAML (learning section). This module does:
  - Load per-dimension IC from ic_tracking storage
  - Compute IC-proportional weights with promote/demote rules
  - Apply EMA blending with previous weights
  - Detect IC decay and log alerts
  - Store updated weights back to kv_json

Integration:
  - server.py calls optimizer after accuracy evaluation + IC computation
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
    """IC-based weight optimizer with auto-promote/demote and decay detection."""

    def __init__(self, store, profile: Dict[str, Any]) -> None:
        self.store = store
        self.profile = profile
        self.cfg = profile.get("learning", {})
        self.enabled = self.cfg.get("enabled", False)
        self.namespace = self.cfg.get("state_namespace", "learning_optimizer")
        self.all_roles = ["whale", "technical", "derivatives", "narrative", "market", "trend"]

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
        """Compute optimal weights from IC data and apply them. Returns new weights or None."""
        if not self.enabled:
            return None

        # Load IC data (computed by storage.compute_ic, saved by server.py)
        ic_data = self._load_ic_data()
        if not ic_data:
            # Fall back to accuracy-based optimization
            logger.info("WeightOptimizer: no IC data, falling back to accuracy-based")
            return self._compute_from_accuracy()

        min_slices = int(self.cfg.get("min_ic_slices", 10))
        total_slices = ic_data.get("total_slices", 0)
        if total_slices < min_slices:
            logger.info(f"WeightOptimizer: only {total_slices} IC slices (need {min_slices}), "
                        f"falling back to accuracy-based")
            return self._compute_from_accuracy()

        # Compute IC-based weights with promote/demote
        new_weights, reasons = self._compute_ic_weights(ic_data)
        if new_weights is None:
            return None

        # Detect IC decay
        self._detect_decay(ic_data)

        # EMA blend with previous weights
        blended = self._ema_blend(new_weights)

        # Save weights, state, and change log
        self._save_weights(blended)
        self._save_state()
        self._save_change_log(blended, reasons, ic_data)

        logger.info(f"WeightOptimizer [IC]: updated weights: {blended}")
        for role, reason in reasons.items():
            logger.info(f"  {role}: {reason}")

        return blended

    def get_current_weights(self) -> Optional[Dict[str, float]]:
        """Load the most recently optimized weights, or None if never optimized."""
        data = self.store.load_kv_json(self.namespace, "learned_weights")
        if data and "weights" in data:
            return data["weights"]
        return None

    def record_dimension_accuracy(self, dimension_scores: Dict[str, Dict[str, Any]]) -> None:
        """Record per-dimension accuracy data after an evaluation (legacy support)."""
        if not self.enabled:
            return

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
    #  IC-based optimization (Phase 2)
    # ------------------------------------------------------------------ #

    def _load_ic_data(self) -> Optional[Dict[str, Any]]:
        """Load the most recent IC computation from storage."""
        # Try 24h window first, fall back to 48h
        for window in ["ic_24h_30d", "ic_48h_30d"]:
            data = self.store.load_kv_json("ic_tracking", window)
            if data and data.get("dimensions"):
                return data
        return None

    def _compute_ic_weights(
        self, ic_data: Dict[str, Any]
    ) -> Tuple[Optional[Dict[str, float]], Dict[str, str]]:
        """Compute weights from IC values with auto-promote/demote.

        Rules from YAML:
          - ic_promote_threshold: IC above this → boost weight (default 0.03)
          - ic_demote_threshold: IC below this → reduce weight (default 0.01)
          - ic_disable_threshold: IC below this → minimize weight (default -0.02)
          - promote_boost: multiplier for strong dimensions (default 1.3)
          - demote_factor: multiplier for weak dimensions (default 0.5)
          - disable_factor: multiplier for negative-IC dimensions (default 0.15)
        """
        promote_thresh = float(self.cfg.get("ic_promote_threshold", 0.03))
        demote_thresh = float(self.cfg.get("ic_demote_threshold", 0.01))
        disable_thresh = float(self.cfg.get("ic_disable_threshold", -0.02))
        promote_boost = float(self.cfg.get("promote_boost", 1.3))
        demote_factor = float(self.cfg.get("demote_factor", 0.5))
        disable_factor = float(self.cfg.get("disable_factor", 0.15))
        min_weight = float(self.cfg.get("min_weight", 0.05))
        max_weight = float(self.cfg.get("max_weight", 0.40))

        fallback = self.cfg.get("fallback_weights", {})
        dimensions = ic_data.get("dimensions", {})

        raw_weights: Dict[str, float] = {}
        reasons: Dict[str, str] = {}

        for role in self.all_roles:
            base_w = float(fallback.get(role, 0.17))
            dim_ic = dimensions.get(role, {})
            ic_val = dim_ic.get("ic")
            icir = dim_ic.get("icir")
            n_slices = dim_ic.get("slices", 0)

            if ic_val is None or n_slices < 5:
                # Not enough data — keep fallback
                raw_weights[role] = base_w
                reasons[role] = f"insufficient data ({n_slices} slices) → fallback {base_w:.3f}"
                continue

            # Auto-promote/demote based on IC
            if ic_val >= promote_thresh:
                # Strong signal — boost
                weight = base_w * promote_boost
                # Extra boost for consistent IC (high ICIR)
                if icir is not None and icir > 1.0:
                    weight *= 1.1  # 10% bonus for consistency
                reasons[role] = (
                    f"PROMOTE: IC={ic_val:.4f} > {promote_thresh} "
                    f"(ICIR={icir}, {n_slices} slices) → {weight:.3f}"
                )
            elif ic_val >= demote_thresh:
                # Mediocre — keep base weight
                weight = base_w
                reasons[role] = (
                    f"HOLD: IC={ic_val:.4f} in [{demote_thresh}, {promote_thresh}] "
                    f"→ {weight:.3f}"
                )
            elif ic_val >= disable_thresh:
                # Weak — reduce weight
                weight = base_w * demote_factor
                reasons[role] = (
                    f"DEMOTE: IC={ic_val:.4f} < {demote_thresh} "
                    f"({n_slices} slices) → {weight:.3f}"
                )
            else:
                # Negative IC — minimize weight (actively hurting predictions)
                weight = base_w * disable_factor
                reasons[role] = (
                    f"DISABLE: IC={ic_val:.4f} < {disable_thresh} "
                    f"(negative signal, {n_slices} slices) → {weight:.3f}"
                )

            raw_weights[role] = weight

        # Apply bounds and normalize
        bounded = self._apply_bounds(raw_weights, min_weight, max_weight)
        return bounded, reasons

    def _detect_decay(self, ic_data: Dict[str, Any]) -> None:
        """Compare current IC to previous IC and flag decaying dimensions."""
        prev_ic_data = self.store.load_kv_json(self.namespace, "previous_ic")
        if not prev_ic_data:
            # Save current as baseline for next time
            self.store.save_kv_json(self.namespace, "previous_ic", {
                "dimensions": ic_data.get("dimensions", {}),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return

        decay_threshold = float(self.cfg.get("decay_threshold_pct", 50))
        prev_dims = prev_ic_data.get("dimensions", {})
        curr_dims = ic_data.get("dimensions", {})

        alerts: List[str] = []
        for role in self.all_roles:
            prev_ic = (prev_dims.get(role, {}).get("ic") or 0)
            curr_ic = (curr_dims.get(role, {}).get("ic") or 0)

            if prev_ic > 0.01 and curr_ic < prev_ic:
                decay_pct = ((prev_ic - curr_ic) / prev_ic) * 100
                if decay_pct >= decay_threshold:
                    alert = (
                        f"DECAY ALERT: {role} IC dropped {decay_pct:.0f}% "
                        f"({prev_ic:.4f} → {curr_ic:.4f})"
                    )
                    alerts.append(alert)
                    logger.warning(f"WeightOptimizer: {alert}")

        if alerts:
            self.store.save_kv_json(self.namespace, "decay_alerts", {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "alerts": alerts,
            })

        # Update baseline for next comparison
        self.store.save_kv_json(self.namespace, "previous_ic", {
            "dimensions": ic_data.get("dimensions", {}),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # ------------------------------------------------------------------ #
    #  Per-asset weight optimization (Level 2)
    # ------------------------------------------------------------------ #

    def compute_per_asset_weights(self) -> Optional[Dict[str, Dict[str, float]]]:
        """Compute per-asset weight profiles from per-asset IC data.

        For each asset with enough IC data, generates a custom weight profile.
        Assets without enough data fall back to global learned weights.

        Returns {BTC: {whale: 0.15, technical: 0.10, ...}, ETH: {...}, ...}
        or None if no per-asset IC data is available.
        """
        if not self.enabled:
            return None

        # Load per-asset IC (try 24h first, then 48h)
        pa_ic_data = None
        for window in ["ic_per_asset_24h_30d", "ic_per_asset_48h_30d"]:
            data = self.store.load_kv_json("ic_tracking", window)
            if data and data.get("assets"):
                pa_ic_data = data
                break

        if not pa_ic_data:
            return None

        assets = pa_ic_data.get("assets", {})
        if not assets:
            return None

        min_obs = int(self.cfg.get("min_per_asset_observations", 8))
        promote_thresh = float(self.cfg.get("ic_promote_threshold", 0.03))
        demote_thresh = float(self.cfg.get("ic_demote_threshold", 0.01))
        disable_thresh = float(self.cfg.get("ic_disable_threshold", -0.02))
        promote_boost = float(self.cfg.get("promote_boost", 1.3))
        demote_factor = float(self.cfg.get("demote_factor", 0.5))
        disable_factor = float(self.cfg.get("disable_factor", 0.15))
        min_weight = float(self.cfg.get("min_weight", 0.05))
        max_weight = float(self.cfg.get("max_weight", 0.40))
        fallback = self.cfg.get("fallback_weights", {})

        per_asset_weights: Dict[str, Dict[str, float]] = {}
        per_asset_reasons: Dict[str, Dict[str, str]] = {}

        for asset, asset_ic in assets.items():
            n_obs = asset_ic.get("n_observations", 0)
            if n_obs < min_obs:
                continue

            dims = asset_ic.get("dimensions", {})
            raw_weights: Dict[str, float] = {}
            reasons: Dict[str, str] = {}

            for role in self.all_roles:
                base_w = float(fallback.get(role, 0.17))
                dim_ic = dims.get(role, {})
                ic_val = dim_ic.get("ic")
                n = dim_ic.get("n", 0)

                if ic_val is None or n < 5:
                    raw_weights[role] = base_w
                    reasons[role] = f"insufficient ({n} obs) → fallback"
                    continue

                if ic_val >= promote_thresh:
                    weight = base_w * promote_boost
                    reasons[role] = f"PROMOTE IC={ic_val:.3f}"
                elif ic_val >= demote_thresh:
                    weight = base_w
                    reasons[role] = f"HOLD IC={ic_val:.3f}"
                elif ic_val >= disable_thresh:
                    weight = base_w * demote_factor
                    reasons[role] = f"DEMOTE IC={ic_val:.3f}"
                else:
                    weight = base_w * disable_factor
                    reasons[role] = f"DISABLE IC={ic_val:.3f}"

                raw_weights[role] = weight

            bounded = self._apply_bounds(raw_weights, min_weight, max_weight)
            per_asset_weights[asset] = bounded
            per_asset_reasons[asset] = reasons

        if not per_asset_weights:
            return None

        # EMA blend with previous per-asset weights
        prev_data = self.store.load_kv_json(self.namespace, "per_asset_weights")
        prev_weights = prev_data.get("weights", {}) if prev_data else {}
        learning_rate = float(self.cfg.get("learning_rate", 0.3))

        blended_all: Dict[str, Dict[str, float]] = {}
        for asset, new_w in per_asset_weights.items():
            prev_w = prev_weights.get(asset, {})
            if prev_w:
                blended: Dict[str, float] = {}
                for role in self.all_roles:
                    old = float(prev_w.get(role, new_w.get(role, 0.17)))
                    blended[role] = round(learning_rate * new_w[role] + (1 - learning_rate) * old, 4)
                total = sum(blended.values())
                if total > 0:
                    for role in self.all_roles:
                        blended[role] = round(blended[role] / total, 4)
                diff = 1.0 - sum(blended.values())
                if abs(diff) > 0.0001:
                    max_role = max(blended, key=blended.get)
                    blended[max_role] = round(blended[max_role] + diff, 4)
                blended_all[asset] = blended
            else:
                blended_all[asset] = new_w

        # Save
        self.store.save_kv_json(self.namespace, "per_asset_weights", {
            "weights": blended_all,
            "reasons": per_asset_reasons,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "n_assets": len(blended_all),
        })

        # Log summary
        for asset, w in blended_all.items():
            top = sorted(w.items(), key=lambda x: x[1], reverse=True)[:2]
            bot = sorted(w.items(), key=lambda x: x[1])[:1]
            logger.info(
                "  [LEARN] %s: top=%s(%.2f),%s(%.2f) bottom=%s(%.2f)",
                asset, top[0][0], top[0][1], top[1][0], top[1][1],
                bot[0][0], bot[0][1],
            )

        return blended_all

    def get_per_asset_weights(self) -> Optional[Dict[str, Dict[str, float]]]:
        """Load the most recently optimized per-asset weights."""
        data = self.store.load_kv_json(self.namespace, "per_asset_weights")
        if data and "weights" in data:
            return data["weights"]
        return None

    # ------------------------------------------------------------------ #
    #  Weight impact tracking
    # ------------------------------------------------------------------ #

    def track_weight_impact(self) -> Optional[Dict[str, Any]]:
        """Compare current accuracy against baseline to measure optimization impact.

        On first call: saves current accuracy as baseline.
        On subsequent calls: computes delta (current - baseline) per asset and overall.

        Returns {overall_delta, per_asset: {BTC: {before, after, delta}, ...}} or None.
        """
        if not self.enabled:
            return None

        # Get current accuracy per asset
        accuracy = self.store.compute_accuracy_by_asset(window_hours=24, days=7)
        if not accuracy:
            return None

        baseline = self.store.load_kv_json(self.namespace, "accuracy_baseline")

        if not baseline or not baseline.get("assets"):
            # First run: save baseline and return
            self.store.save_kv_json(self.namespace, "accuracy_baseline", {
                "assets": accuracy,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "note": "pre-optimization baseline",
            })
            logger.info("  [TRACK] Saved accuracy baseline for %s assets", len(accuracy))
            return {"status": "baseline_saved", "n_assets": len(accuracy)}

        # Compare current vs baseline
        baseline_assets = baseline.get("assets", {})
        comparisons: Dict[str, Dict] = {}
        deltas = []

        for asset, current in accuracy.items():
            prev = baseline_assets.get(asset)
            if prev and prev.get("n", 0) >= 3 and current.get("n", 0) >= 3:
                before_grad = prev["avg_gradient"]
                after_grad = current["avg_gradient"]
                delta = round(after_grad - before_grad, 4)
                deltas.append(delta)
                comparisons[asset] = {
                    "before": before_grad,
                    "after": after_grad,
                    "delta": delta,
                    "direction": "improved" if delta > 0.02 else ("declined" if delta < -0.02 else "stable"),
                    "n_before": prev["n"],
                    "n_after": current["n"],
                }

        if not deltas:
            return {"status": "insufficient_data"}

        overall_delta = round(sum(deltas) / len(deltas), 4)
        improved = sum(1 for d in deltas if d > 0.02)
        declined = sum(1 for d in deltas if d < -0.02)
        stable = len(deltas) - improved - declined

        result = {
            "status": "tracked",
            "overall_delta": overall_delta,
            "improved": improved,
            "declined": declined,
            "stable": stable,
            "per_asset": comparisons,
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "baseline_from": baseline.get("saved_at"),
        }

        # Save tracking history (keep last 20)
        history = self.store.load_kv_json(self.namespace, "impact_history") or []
        if not isinstance(history, list):
            history = []
        history.append(result)
        history = history[-20:]
        self.store.save_kv_json(self.namespace, "impact_history", history)

        # If enough data and things improved, update the baseline
        total_current_evals = sum(a.get("n", 0) for a in accuracy.values())
        if total_current_evals >= 50 and overall_delta > 0:
            self.store.save_kv_json(self.namespace, "accuracy_baseline", {
                "assets": accuracy,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "note": f"updated: +{overall_delta:.3f} overall (from {baseline.get('saved_at', '?')})",
            })
            logger.info("  [TRACK] Updated baseline (overall improved by %.3f)", overall_delta)

        return result

    # ------------------------------------------------------------------ #
    #  Accuracy-based fallback (Phase 1, for when IC data is unavailable)
    # ------------------------------------------------------------------ #

    def _compute_from_accuracy(self) -> Optional[Dict[str, float]]:
        """Original accuracy-based optimization (fallback when IC not available)."""
        dim_accuracy = self._load_dimension_accuracy()
        if not dim_accuracy:
            return None

        min_evals = int(self.cfg.get("min_evaluations", 20))
        total_evals = sum(d.get("count", 0) for d in dim_accuracy.values())
        if total_evals < min_evals:
            logger.info(f"WeightOptimizer: only {total_evals} evals, need {min_evals}")
            return None

        new_weights = self._compute_accuracy_weights(dim_accuracy)
        if new_weights is None:
            return None

        blended = self._ema_blend(new_weights)
        self._save_weights(blended)
        self._save_state()

        logger.info(f"WeightOptimizer [accuracy fallback]: updated weights: {blended}")
        return blended

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

    def _compute_accuracy_weights(
        self, dim_accuracy: Dict[str, Dict[str, float]]
    ) -> Optional[Dict[str, float]]:
        """Compute weights proportional to accuracy (fallback method)."""
        min_weight = float(self.cfg.get("min_weight", 0.05))
        max_weight = float(self.cfg.get("max_weight", 0.40))
        fallback = self.cfg.get("fallback_weights", {})

        accuracies: Dict[str, float] = {}
        for role in self.all_roles:
            if role in dim_accuracy:
                accuracies[role] = max(dim_accuracy[role]["accuracy"], 0.01)
            else:
                accuracies[role] = float(fallback.get(role, 0.2))

        total_acc = sum(accuracies.values())
        if total_acc <= 0:
            return None

        raw = {role: accuracies[role] / total_acc for role in self.all_roles}
        return self._apply_bounds(raw, min_weight, max_weight)

    # ------------------------------------------------------------------ #
    #  Shared helpers
    # ------------------------------------------------------------------ #

    def _apply_bounds(self, raw: Dict[str, float],
                       min_w: float, max_w: float) -> Dict[str, float]:
        """Apply min/max bounds and redistribute to sum to 1.0."""
        result = dict(raw)

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

            if unbounded and (excess > 0 or deficit > 0):
                net = excess - deficit
                unbounded_sum = sum(result[r] for r in unbounded)
                if unbounded_sum > 0 and net != 0:
                    for role in unbounded:
                        result[role] += net * (result[role] / unbounded_sum)

            total = sum(result.values())
            if abs(total - 1.0) < 0.001:
                break
            for role in self.all_roles:
                result[role] /= total

        # Final normalize
        total = sum(result.values())
        if total > 0:
            for role in self.all_roles:
                result[role] = round(result[role] / total, 4)

        # Fix rounding
        diff = 1.0 - sum(result.values())
        if abs(diff) > 0.0001:
            max_role = max(result, key=result.get)
            result[max_role] = round(result[max_role] + diff, 4)

        return result

    def _ema_blend(self, new_weights: Dict[str, float]) -> Dict[str, float]:
        """Blend new weights with previous using EMA (exponential moving average)."""
        learning_rate = float(self.cfg.get("learning_rate", 0.3))

        prev_data = self.store.load_kv_json(self.namespace, "learned_weights")
        if prev_data and "weights" in prev_data:
            prev = prev_data["weights"]
        else:
            prev = self.cfg.get("fallback_weights", {})
            if not prev:
                prev = dict(self.profile.get("weights", {}))

        blended: Dict[str, float] = {}
        for role in self.all_roles:
            old_w = float(prev.get(role, 0.2))
            new_w = float(new_weights.get(role, 0.2))
            blended[role] = round(learning_rate * new_w + (1 - learning_rate) * old_w, 4)

        # Normalize
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

    def _save_change_log(
        self,
        weights: Dict[str, float],
        reasons: Dict[str, str],
        ic_data: Dict[str, Any],
    ) -> None:
        """Save a log entry of weight changes with reasons for transparency."""
        # Load existing log (keep last 50 entries)
        log = self.store.load_kv_json(self.namespace, "change_log") or []
        if not isinstance(log, list):
            log = []

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "weights": weights,
            "reasons": reasons,
            "ic_summary": {
                role: ic_data.get("dimensions", {}).get(role, {}).get("ic")
                for role in self.all_roles
            },
            "overall_ic": ic_data.get("overall_ic"),
            "total_slices": ic_data.get("total_slices", 0),
        }

        log.append(entry)
        log = log[-50:]  # Keep last 50

        self.store.save_kv_json(self.namespace, "change_log", log)

    def _get_eval_count(self) -> int:
        """Get total evaluation count from performance_accuracy table."""
        try:
            if self.store.backend == "postgres":
                import psycopg2
                from shared.storage import _pg_conn
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM performance_accuracy")
                        return cur.fetchone()[0]
            else:
                import sqlite3
                with sqlite3.connect(self.store.db_path) as conn:
                    row = conn.execute("SELECT COUNT(*) FROM performance_accuracy").fetchone()
                    return row[0] if row else 0
        except Exception:
            return 0
