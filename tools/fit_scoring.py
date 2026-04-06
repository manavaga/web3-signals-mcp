# tools/fit_scoring.py
"""Data-fitted scoring — replaces ALL hardcoded scoring curves.

Instead of hand-tuned functions like `_rsi_score(rsi) -> 95 - (rsi/30) * 20`,
this computes the actual statistical relationship between each indicator
and forward returns, then scores based on that.

For each indicator, from training data we compute:
  - mean: average value
  - std: standard deviation
  - ic: Spearman correlation with 48h forward returns (direction + strength)

Score = 50 + clamp(z_score * ic * SCALE)

This automatically:
  - Handles direction: positive IC → high value = bullish
  - Handles sensitivity: higher IC magnitude → larger score swing
  - Handles irrelevant indicators: IC ≈ 0 → score stays near 50
  - No magic numbers, everything from data
"""
from __future__ import annotations

import math
from typing import Optional

from scipy.stats import spearmanr


# The only tunable: controls score range. 40 means a 2-sigma move
# on an indicator with IC=0.25 gives score = 50 ± 20 (range 30-70).
# This is calibrated so composite scores actually cross 50 for signals.
SCALE = 40.0


def fit_indicator_params(
    indicator_series: dict[str, list[float]],
    forward_returns: list[float],
    min_obs: int = 20,
) -> dict[str, dict]:
    """Fit scoring parameters from training data.

    Args:
        indicator_series: {indicator_name: [value_day0, value_day1, ...]}
        forward_returns: [return_day0, return_day1, ...]
        min_obs: Minimum observations for a valid fit.

    Returns: {indicator_name: {"mean": float, "std": float, "ic": float}}
    """
    params = {}
    for name, values in indicator_series.items():
        if len(values) < min_obs or len(forward_returns) < min_obs:
            continue

        # Align lengths and filter None/NaN
        pairs = [
            (v, r)
            for v, r in zip(values, forward_returns)
            if v is not None and r is not None
            and v == v and r == r  # NaN check
        ]
        if len(pairs) < min_obs:
            continue

        vals, rets = zip(*pairs)
        mean = sum(vals) / len(vals)
        variance = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = math.sqrt(variance) if variance > 0 else 0.0

        if std == 0:
            continue

        corr, p_value = spearmanr(vals, rets)
        if corr != corr:  # NaN
            continue

        # Use IC even if p > 0.05, but dampen weak signals
        ic = float(corr)
        if p_value > 0.20:
            ic = 0.0  # Too noisy, ignore

        params[name] = {"mean": mean, "std": std, "ic": ic}

    return params


def fitted_score(value: float, mean: float, std: float, ic: float) -> float:
    """Convert indicator value to 0-100 score using data-fitted params.

    No hardcoded curves. Direction and sensitivity entirely from data:
    - If IC > 0: higher values → higher scores (bullish)
    - If IC < 0: higher values → lower scores (bearish)
    - |IC| determines how much the indicator moves the score
    """
    if std == 0 or ic == 0:
        return 50.0
    z = (value - mean) / std
    z = max(-3.0, min(3.0, z))  # Clamp extreme z-scores
    score = 50.0 + z * ic * SCALE
    return max(10.0, min(90.0, score))


def score_dimension_fitted(
    data: dict,
    fitted_params: dict,
    indicator_names: list[str],
) -> float:
    """Score a dimension using fitted params for multiple indicators.

    Each indicator is weighted by abs(IC) — predictive power determines weight.
    Indicators with IC ≈ 0 contribute almost nothing.

    Args:
        data: Raw indicator values for this asset/day.
        fitted_params: Output of fit_indicator_params().
        indicator_names: Which indicators to include.

    Returns: Score 0-100.
    """
    scores = []
    weights = []

    for name in indicator_names:
        if name not in fitted_params or name not in data:
            continue
        value = data.get(name)
        if value is None:
            continue

        p = fitted_params[name]
        s = fitted_score(value, p["mean"], p["std"], p["ic"])
        w = abs(p["ic"])

        if w > 0.01:  # Only include indicators with meaningful IC
            scores.append(s)
            weights.append(w)

    if not weights:
        return 50.0

    total_w = sum(weights)
    return sum(s * w for s, w in zip(scores, weights)) / total_w


# ---------------------------------------------------------------------------
# Indicator groups by dimension
# ---------------------------------------------------------------------------

TECHNICAL_INDICATORS = [
    "rsi_14", "macd_histogram", "bb_bandwidth",
    "obv_slope", "roc_7d",
    "squeeze_momentum", "macd_zscore", "rsi_zscore", "bb_zscore",
    "adx_14", "volume_ratio", "atr_pct",
]

MARKET_INDICATORS = [
    "fear_greed", "sp500_change", "dxy_change",
    "nasdaq_change", "vix_roc",
]

DERIVATIVES_INDICATORS = [
    "funding_rate", "long_short_ratio",
    "taker_buy_sell_ratio", "oi_change_pct",
]
