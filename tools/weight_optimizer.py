# tools/weight_optimizer.py
"""Per-asset weight optimizer using grid search over dimension weights.

All functions are pure — no side effects, no API calls. Data is passed in.
Weights are determined by backtesting, never by human intuition.
"""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import product

import numpy as np

from tools.walk_forward import gradient_score, compute_cwa, evaluate_neutral


# ---------------------------------------------------------------------------
# Component 1: Weight Grid Generator
# ---------------------------------------------------------------------------

def generate_weight_grid(
    n_dims: int = 3,
    step: float = 0.05,
    min_weight: float = 0.05,
    max_weight: float = 0.70,
) -> list[tuple]:
    """Generate all valid weight combinations that sum to 1.0.

    With 3 dims, step=0.05, min=0.05, max=0.70 -> ~66 valid combinations.
    Examples: (0.50, 0.30, 0.20), (0.40, 0.35, 0.25), etc.
    """
    values = np.arange(min_weight, max_weight + step / 2, step)
    values = np.round(values, 2)

    combos = []
    for first_dims in product(values, repeat=n_dims - 1):
        remainder = round(1.0 - sum(first_dims), 2)
        if min_weight <= remainder <= max_weight:
            combo = tuple(round(v, 2) for v in first_dims) + (remainder,)
            combos.append(combo)
    return combos


# ---------------------------------------------------------------------------
# Component 2: IC-Based Sub-Weight Calculator
# ---------------------------------------------------------------------------

def compute_ic_sub_weights(ic_dict: dict) -> dict:
    """Convert IC values to normalized sub-weights within a dimension.

    Positive IC -> proportional weight.
    Negative IC -> zero weight (anti-predictive, drop it).

    Example:
        {"rsi": 0.10, "macd": 0.05, "obv": 0.15, "mfi": -0.03}
        -> {"rsi": 0.33, "macd": 0.17, "obv": 0.50, "mfi": 0.0}
    """
    clipped = {k: max(0, v) for k, v in ic_dict.items()}
    total = sum(clipped.values())
    if total == 0:
        n = len(clipped)
        return {k: round(1.0 / n, 4) for k in clipped}
    return {k: round(v / total, 4) for k, v in clipped.items()}


# ---------------------------------------------------------------------------
# Component 3: Confidence Tiers
# ---------------------------------------------------------------------------

def get_confidence_tier(n_signals: int) -> str:
    """Determine confidence level from sample size.

    >= 80 signals: "high"         -- use per-asset weights directly
    50-79:         "medium"       -- use but flag for review
    30-49:         "low"          -- fall back to tier-average weights
    < 30:          "insufficient" -- fall back to equal weights
    """
    if n_signals >= 80:
        return "high"
    elif n_signals >= 50:
        return "medium"
    elif n_signals >= 30:
        return "low"
    else:
        return "insufficient"


# ---------------------------------------------------------------------------
# Component 4: Per-Asset Optimizer
# ---------------------------------------------------------------------------

def _evaluate_weights(
    weights: tuple,
    dim_names: list[str],
    dimension_scores: dict,
    forward_returns_24h: dict,
    forward_returns_48h: dict,
    noise_threshold: float,
    strong_threshold: float,
    atr_pct: float,
) -> dict:
    """Evaluate a single weight combination against historical data.

    Returns metrics dict with CWA, accuracy, coverage, abstain_miss_rate.
    """
    weight_map = dict(zip(dim_names, weights))

    correct_24h = 0
    correct_48h = 0
    total_directional = 0
    total_signals = 0
    abstain_count = 0
    abstain_misses = 0

    day_indices = sorted(
        set(dimension_scores.keys())
        & set(forward_returns_24h.keys())
        & set(forward_returns_48h.keys())
    )

    for d in day_indices:
        scores = dimension_scores[d]
        composite = sum(scores[dim] * weight_map[dim] for dim in dim_names)
        total_signals += 1

        # Determine direction from composite
        if composite > 55:
            direction = "bullish"
        elif composite < 45:
            direction = "bearish"
        else:
            # Neutral / abstain
            abstain_count += 1
            ret_24 = forward_returns_24h[d]
            if evaluate_neutral(ret_24, atr_pct) == 0.0:
                abstain_misses += 1
            continue

        total_directional += 1
        ret_24 = forward_returns_24h[d]
        ret_48 = forward_returns_48h[d]

        score_24 = gradient_score(direction, ret_24, noise_threshold, strong_threshold)
        score_48 = gradient_score(direction, ret_48, noise_threshold, strong_threshold)

        if score_24 >= 0.7:
            correct_24h += 1
        if score_48 >= 0.7:
            correct_48h += 1

    # Compute metrics
    if total_signals == 0:
        return {
            "cwa_24h": 0.0, "cwa_48h": 0.0,
            "accuracy_24h": 0.0, "accuracy_48h": 0.0,
            "coverage": 0.0, "abstain_miss_rate": 0.0,
            "n_signals": 0, "combined_score": 0.0,
        }

    accuracy_24h = correct_24h / total_directional if total_directional > 0 else 0.0
    accuracy_48h = correct_48h / total_directional if total_directional > 0 else 0.0
    coverage = total_directional / total_signals if total_signals > 0 else 0.0

    cwa_24h = compute_cwa(correct_24h, total_signals, total_directional)
    cwa_48h = compute_cwa(correct_48h, total_signals, total_directional)

    abstain_miss_rate = (
        abstain_misses / abstain_count if abstain_count > 0 else 0.0
    )

    combined_score = (
        0.4 * cwa_24h + 0.4 * cwa_48h + 0.2 * (1.0 - abstain_miss_rate)
    )

    return {
        "cwa_24h": round(cwa_24h, 4),
        "cwa_48h": round(cwa_48h, 4),
        "accuracy_24h": round(accuracy_24h, 4),
        "accuracy_48h": round(accuracy_48h, 4),
        "coverage": round(coverage, 4),
        "abstain_miss_rate": round(abstain_miss_rate, 4),
        "n_signals": total_directional,
        "combined_score": round(combined_score, 4),
    }


def optimize_asset(
    asset: str,
    dimension_scores: dict,
    forward_returns_24h: dict,
    forward_returns_48h: dict,
    noise_threshold: float,
    strong_threshold: float,
    atr_pct: float = 2.0,
) -> dict:
    """Find optimal dimension weights for one asset using grid search.

    For each weight combination in the grid:
    1. Compute composite score = sum(dim_score * weight)
    2. Determine direction from composite (>55 = bullish, <45 = bearish, else neutral)
    3. Evaluate against actual 24h and 48h returns
    4. Compute combined score = 0.4*CWA_24h + 0.4*CWA_48h + 0.2*(1-abstain_miss_rate)

    Returns dict with weights, metrics, and combined_score.
    """
    # Detect dimension names from data
    sample_day = next(iter(dimension_scores.values()))
    dim_names = sorted(sample_day.keys())

    grid = generate_weight_grid(n_dims=len(dim_names))

    best_result = None
    best_score = -1.0

    for weights in grid:
        result = _evaluate_weights(
            weights, dim_names, dimension_scores,
            forward_returns_24h, forward_returns_48h,
            noise_threshold, strong_threshold, atr_pct,
        )
        if result["combined_score"] > best_score:
            best_score = result["combined_score"]
            best_result = result
            best_result["weights"] = dict(zip(dim_names, weights))

    # Fallback: if no grid entry produced results (shouldn't happen)
    if best_result is None:
        equal_w = round(1.0 / len(dim_names), 4)
        best_result = {
            "weights": {d: equal_w for d in dim_names},
            "cwa_24h": 0.0, "cwa_48h": 0.0,
            "accuracy_24h": 0.0, "accuracy_48h": 0.0,
            "coverage": 0.0, "abstain_miss_rate": 0.0,
            "n_signals": 0, "combined_score": 0.0,
        }

    return best_result


# ---------------------------------------------------------------------------
# Component 5: Full Optimization Runner
# ---------------------------------------------------------------------------

def run_optimization(all_asset_data: dict, asset_configs: dict) -> dict:
    """Run optimization for all assets. Returns baseline-format results.

    Args:
        all_asset_data: {
            "BTC": {
                "dimension_scores": {day_idx: {"technical": float, ...}},
                "forward_returns_24h": {day_idx: float},
                "forward_returns_48h": {day_idx: float},
            }, ...
        }
        asset_configs: {
            "BTC": {"noise_threshold": 1.0, "strong_threshold": 3.0}, ...
        }

    Returns baseline-format results dict.
    """
    assets_results = {}
    total_cwa = 0.0
    total_acc_24h = 0.0
    n_assets = 0

    for asset, data in all_asset_data.items():
        cfg = asset_configs.get(asset, {})
        noise = cfg.get("noise_threshold", 2.0)
        strong = cfg.get("strong_threshold", 5.0)

        result = optimize_asset(
            asset=asset,
            dimension_scores=data["dimension_scores"],
            forward_returns_24h=data["forward_returns_24h"],
            forward_returns_48h=data["forward_returns_48h"],
            noise_threshold=noise,
            strong_threshold=strong,
        )
        result["confidence"] = get_confidence_tier(result["n_signals"])
        assets_results[asset] = result

        total_cwa += result["cwa_24h"]
        total_acc_24h += result["accuracy_24h"]
        n_assets += 1

    overall_cwa = round(total_cwa / n_assets, 4) if n_assets > 0 else 0.0
    overall_acc = round(total_acc_24h / n_assets, 4) if n_assets > 0 else 0.0

    return {
        "version": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "overall_cwa": overall_cwa,
        "overall_accuracy_24h": overall_acc,
        "assets": assets_results,
    }
