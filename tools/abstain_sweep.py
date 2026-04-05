# tools/abstain_sweep.py
"""Sweep abstain thresholds per asset to find optimal values.

For each asset, tries all combinations of:
- bearish_min_distance: [2, 3, 4, 5, 6, 7, 8, 10, 12]
- bullish_min_distance: [2, 3, 4, 5, 6, 7, 8, 10, 12]
- regime_multiplier_ranging: [0.8, 1.0, 1.2, 1.5, 2.0]

= 9 x 9 x 5 = 405 combinations per asset

For each combination, applies the abstain logic to scored signals and computes:
- CWA (accuracy x coverage_factor)
- Accuracy (directional signals correct %)
- Abstain miss rate (neutral when should have been directional)
- Combined score = 0.4 * CWA + 0.3 * accuracy + 0.3 * (1 - abstain_miss_rate)

Pure function: data in, results out. No side effects.
"""
from __future__ import annotations

from tools.walk_forward import gradient_score, compute_cwa

DEFAULT_BEARISH_RANGE = [2, 3, 4, 5, 6, 7, 8, 10, 12]
DEFAULT_BULLISH_RANGE = [2, 3, 4, 5, 6, 7, 8, 10, 12]
DEFAULT_REGIME_MULT_RANGE = [0.8, 1.0, 1.2, 1.5, 2.0]


def sweep_abstain_thresholds(
    composite_scores: list[float],
    forward_returns_24h: list[float],
    forward_returns_48h: list[float],
    noise_threshold: float,
    strong_threshold: float,
    atr_pct: float,
    target_coverage: float = 0.30,
    bearish_range: list[int] | None = None,
    bullish_range: list[int] | None = None,
    regime_mult_range: list[float] | None = None,
) -> dict:
    """Find optimal abstain thresholds for one asset.

    Args:
        composite_scores: Pre-computed composite scores per day (0-100).
        forward_returns_24h: Actual 24h % returns per day.
        forward_returns_48h: Actual 48h % returns per day.
        noise_threshold: Asset noise threshold (e.g. 1.0% for BTC).
        strong_threshold: Asset strong-move threshold (e.g. 3.0% for BTC).
        atr_pct: ATR as % of price — moves > atr_pct are "real" moves.
        target_coverage: CWA target coverage (default 0.30).
        bearish_range: Bearish min-distance values to try.
        bullish_range: Bullish min-distance values to try.
        regime_mult_range: Regime multiplier values to try.

    Returns:
        Dict with best thresholds and performance metrics.
    """
    if bearish_range is None:
        bearish_range = list(DEFAULT_BEARISH_RANGE)
    if bullish_range is None:
        bullish_range = list(DEFAULT_BULLISH_RANGE)
    if regime_mult_range is None:
        regime_mult_range = list(DEFAULT_REGIME_MULT_RANGE)

    n = len(composite_scores)
    if n == 0:
        return {
            "best_bearish_distance": bearish_range[0],
            "best_bullish_distance": bullish_range[0],
            "best_regime_multiplier": regime_mult_range[0],
            "combined_score": 0.0,
            "cwa": 0.0,
            "accuracy_24h": 0.0,
            "abstain_miss_rate": 0.0,
            "coverage": 0.0,
            "combos_tested": 0,
        }

    best: dict = {"combined_score": -1.0}
    combos_tested = 0

    for bear_dist in bearish_range:
        for bull_dist in bullish_range:
            for regime_mult in regime_mult_range:
                correct_24h = 0
                directional = 0
                abstain_misses = 0
                abstain_total = 0

                for i in range(n):
                    composite = composite_scores[i]
                    distance_from_50 = abs(composite - 50)

                    # Determine effective threshold
                    if composite > 50:
                        threshold = bull_dist * regime_mult
                    else:
                        threshold = bear_dist * regime_mult

                    if distance_from_50 < threshold:
                        # Would abstain
                        abstain_total += 1
                        if abs(forward_returns_24h[i]) > atr_pct:
                            abstain_misses += 1  # Missed a real move
                    else:
                        # Directional signal
                        directional += 1
                        direction = "bullish" if composite > 50 else "bearish"
                        g24 = gradient_score(
                            direction, forward_returns_24h[i],
                            noise_threshold, strong_threshold,
                        )
                        if g24 >= 0.7:
                            correct_24h += 1

                combos_tested += 1

                # Compute metrics
                if directional == 0:
                    accuracy = 0.0
                    cwa_val = 0.0
                else:
                    accuracy = correct_24h / directional

                cwa_val = compute_cwa(correct_24h, n, directional, target_coverage)
                coverage = directional / n if n > 0 else 0.0

                if abstain_total > 0:
                    miss_rate = abstain_misses / abstain_total
                else:
                    miss_rate = 0.0

                combined = 0.4 * cwa_val + 0.3 * accuracy + 0.3 * (1.0 - miss_rate)

                if combined > best["combined_score"]:
                    best = {
                        "best_bearish_distance": bear_dist,
                        "best_bullish_distance": bull_dist,
                        "best_regime_multiplier": regime_mult,
                        "combined_score": round(combined, 6),
                        "cwa": round(cwa_val, 6),
                        "accuracy_24h": round(accuracy, 6),
                        "abstain_miss_rate": round(miss_rate, 6),
                        "coverage": round(coverage, 6),
                        "combos_tested": combos_tested,
                    }

    # Final: update combos_tested to total
    best["combos_tested"] = combos_tested
    return best
