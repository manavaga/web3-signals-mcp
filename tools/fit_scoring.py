# tools/fit_scoring.py
"""Data-fitted scoring — replaces ALL hardcoded scoring curves.

Instead of hand-tuned functions like `_rsi_score(rsi) -> 95 - (rsi/30) * 20`,
this computes the actual statistical relationship between each indicator
and forward returns, then scores based on that.

For each indicator, from training data we compute:
  - mean: average value
  - std: standard deviation
  - ic: Ensemble correlation (Spearman+Pearson+Kendall median) with 48h forward returns

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

from scipy.stats import spearmanr, pearsonr, kendalltau


# Controls score range from center (50). Higher = more extreme scores = more
# signals crossing the abstain threshold. Calibrated via walk-forward trade
# simulation: SCALE=140 gives +46.8% PnL, best early-period performance.
# With typical IC ~0.2 and z ~2, score = 50 ± 2*0.2*140 = 50 ± 56.
SCALE = 140.0


def _ensemble_ic(vals, rets, adjusted_p=0.05):
    """Compute ensemble IC from Spearman + Pearson + Kendall.

    Returns the median IC across methods that pass significance.
    More robust than any single method, especially on small samples.
    """
    ics = []

    for corr_func in (spearmanr, pearsonr, kendalltau):
        try:
            corr, p_value = corr_func(vals, rets)
            if corr == corr and p_value < adjusted_p:  # Not NaN and significant
                ics.append(float(corr))
        except Exception:
            continue

    if not ics:
        return 0.0

    # Use median for robustness against outlier methods
    ics.sort()
    return ics[len(ics) // 2]


def fit_indicator_params(
    indicator_series: dict[str, list[float]],
    forward_returns: list[float],
    min_obs: int = 20,
    base_p_threshold: float = 0.05,
    min_ic: float = 0.03,
) -> dict[str, dict]:
    """Fit scoring parameters from training data.

    Uses ensemble IC (median of Spearman+Pearson+Kendall) with optional
    BH FDR correction. Indicators below min_ic absolute value are zeroed
    as noise regardless of p-value.

    In walk-forward backtesting, the fold structure prevents overfitting,
    so p-value filtering can be lenient. The IC magnitude itself determines
    each indicator's weight in scoring (via abs(IC) weighting).

    Args:
        indicator_series: {indicator_name: [value_day0, value_day1, ...]}
        forward_returns: [return_day0, return_day1, ...]
        min_obs: Minimum observations for a valid fit.
        base_p_threshold: Significance level for BH FDR correction.
        min_ic: Minimum absolute IC to keep (filters pure noise).

    Returns: {indicator_name: {"mean": float, "std": float, "ic": float}}
    """
    # Phase 1: Compute raw IC and p-values for all indicators
    raw_results = {}
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

        # Compute ensemble IC with raw p-values
        best_ic, best_p = _ensemble_ic_with_pvalue(vals, rets)
        raw_results[name] = {"mean": mean, "std": std, "ic": best_ic, "p_value": best_p}

    # Phase 2: Apply BH FDR correction, then min_ic filter
    params = _apply_bh_correction(raw_results, base_p_threshold)

    # Phase 3: Zero out ICs below minimum threshold (pure noise)
    for name in params:
        if abs(params[name]["ic"]) < min_ic:
            params[name]["ic"] = 0.0

    return params


def _ensemble_ic_with_pvalue(vals, rets):
    """Compute ensemble IC and return best (ic, p_value) pair.

    Uses median IC across methods, minimum p-value for significance.
    """
    results = []
    for corr_func in (spearmanr, pearsonr, kendalltau):
        try:
            corr, p_value = corr_func(vals, rets)
            if corr == corr:  # Not NaN
                results.append((float(corr), float(p_value)))
        except Exception:
            continue

    if not results:
        return 0.0, 1.0

    # Median IC for robustness
    ics = sorted([r[0] for r in results])
    median_ic = ics[len(ics) // 2]

    # Minimum p-value (most significant result across methods)
    min_p = min(r[1] for r in results)

    return median_ic, min_p


def _apply_bh_correction(raw_results: dict, fdr_threshold: float) -> dict:
    """Apply Benjamini-Hochberg FDR correction to IC p-values.

    BH procedure:
    1. Sort p-values ascending
    2. For rank i of m tests, threshold = (i/m) * fdr_threshold
    3. Find largest i where p_i <= threshold_i
    4. All indicators with rank <= i are significant
    """
    if not raw_results:
        return {}

    # Sort by p-value
    sorted_items = sorted(raw_results.items(), key=lambda x: x[1]["p_value"])
    m = len(sorted_items)

    # Find BH cutoff
    max_significant_rank = -1
    for rank_idx, (name, data) in enumerate(sorted_items):
        rank = rank_idx + 1  # 1-based
        bh_threshold = (rank / m) * fdr_threshold
        if data["p_value"] <= bh_threshold:
            max_significant_rank = rank_idx

    # Build params dict — significant indicators keep their IC, others get 0
    params = {}
    for rank_idx, (name, data) in enumerate(sorted_items):
        ic = data["ic"] if rank_idx <= max_significant_rank else 0.0
        params[name] = {"mean": data["mean"], "std": data["std"], "ic": ic}

    return params


def fit_indicator_params_filtered(
    indicator_series: dict[str, list[float]],
    forward_returns: list[float],
    min_obs: int = 20,
    collinearity_threshold: float = 0.80,
) -> dict[str, dict]:
    """Fit params, then remove collinear indicators (keep higher IC)."""
    params = fit_indicator_params(indicator_series, forward_returns, min_obs)

    from tools.multicollinearity import find_collinear_pairs, drop_collinear
    pairs = find_collinear_pairs(indicator_series, threshold=collinearity_threshold, min_obs=min_obs)
    if pairs:
        params = drop_collinear(params, pairs)

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
    "rsi_14", "macd_histogram", "bb_bandwidth", "bb_position",
    "obv_slope", "roc_7d", "roc_1d", "roc_30d",
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

# Lead indicators — computed and stored but NOT used in scoring until
# proven via backtest. Including unproven indicators in IC fitting
# destabilizes weight distribution across all indicators in the dimension.
# liq_density has no data in backtests (live-only), so it introduces pure noise.
DERIVATIVES_LEAD_CANDIDATES = [
    "funding_accel", "oi_accel", "vol_price_div", "liq_density",
]
