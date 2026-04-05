# tests/test_abstain_sweep.py
"""Tests for Task 14: abstain threshold calibration sweep."""
from tools.abstain_sweep import (
    sweep_abstain_thresholds,
    DEFAULT_BEARISH_RANGE,
    DEFAULT_BULLISH_RANGE,
    DEFAULT_REGIME_MULT_RANGE,
)


def _make_signals(n: int, composite: float, ret_24h: float, ret_48h: float):
    """Helper: create uniform signal lists."""
    return (
        [composite] * n,
        [ret_24h] * n,
        [ret_48h] * n,
    )


def test_sweep_finds_optimal_thresholds():
    """Sweep returns a result with valid structure and positive combined score."""
    # Clear bullish signals (composite=65) that are correct (positive returns)
    composites = [65.0] * 50 + [35.0] * 50
    returns_24h = [3.0] * 50 + [-3.0] * 50  # Correct directions
    returns_48h = [4.0] * 50 + [-4.0] * 50

    result = sweep_abstain_thresholds(
        composite_scores=composites,
        forward_returns_24h=returns_24h,
        forward_returns_48h=returns_48h,
        noise_threshold=1.0,
        strong_threshold=3.0,
        atr_pct=2.0,
    )

    assert result["combined_score"] > 0
    assert "best_bearish_distance" in result
    assert "best_bullish_distance" in result
    assert "best_regime_multiplier" in result
    assert result["best_bearish_distance"] in DEFAULT_BEARISH_RANGE
    assert result["best_bullish_distance"] in DEFAULT_BULLISH_RANGE
    assert result["best_regime_multiplier"] in DEFAULT_REGIME_MULT_RANGE
    assert 0 <= result["accuracy_24h"] <= 1.0
    assert 0 <= result["coverage"] <= 1.0
    assert 0 <= result["abstain_miss_rate"] <= 1.0


def test_sweep_respects_miss_rate_constraint():
    """With all strong moves, tight thresholds that abstain will have high miss rate."""
    # All signals have big moves — abstaining is costly
    composites = [52.0] * 100  # Close to 50 (many will be abstained)
    returns_24h = [5.0] * 100  # But all have big moves

    result = sweep_abstain_thresholds(
        composite_scores=composites,
        forward_returns_24h=returns_24h,
        forward_returns_48h=returns_24h,
        noise_threshold=1.0,
        strong_threshold=3.0,
        atr_pct=2.0,
    )

    # The optimizer should prefer tighter thresholds (smaller distances)
    # to avoid missing the big moves, OR wider to let signals through
    # The combined score penalizes high miss rates via (1 - miss_rate)
    assert result["combined_score"] > 0


def test_sweep_covers_all_combos():
    """Default sweep tests 9 * 9 * 5 = 405 combinations."""
    composites, returns_24h, returns_48h = _make_signals(20, 55.0, 1.5, 2.0)

    result = sweep_abstain_thresholds(
        composite_scores=composites,
        forward_returns_24h=returns_24h,
        forward_returns_48h=returns_48h,
        noise_threshold=1.0,
        strong_threshold=3.0,
        atr_pct=2.0,
    )

    expected_combos = len(DEFAULT_BEARISH_RANGE) * len(DEFAULT_BULLISH_RANGE) * len(DEFAULT_REGIME_MULT_RANGE)
    assert result["combos_tested"] == expected_combos
    assert expected_combos == 405


def test_sweep_tight_thresholds_increase_coverage():
    """Tighter thresholds (smaller distances) should yield higher coverage."""
    composites = [55.0] * 50 + [45.0] * 50
    returns_24h = [2.0] * 50 + [-2.0] * 50
    returns_48h = returns_24h

    # Sweep with only tight thresholds
    result_tight = sweep_abstain_thresholds(
        composite_scores=composites,
        forward_returns_24h=returns_24h,
        forward_returns_48h=returns_48h,
        noise_threshold=1.0,
        strong_threshold=3.0,
        atr_pct=2.0,
        bearish_range=[2],
        bullish_range=[2],
        regime_mult_range=[1.0],
    )

    # Sweep with only wide thresholds
    result_wide = sweep_abstain_thresholds(
        composite_scores=composites,
        forward_returns_24h=returns_24h,
        forward_returns_48h=returns_48h,
        noise_threshold=1.0,
        strong_threshold=3.0,
        atr_pct=2.0,
        bearish_range=[12],
        bullish_range=[12],
        regime_mult_range=[1.0],
    )

    # Tight thresholds = fewer abstains = higher coverage
    assert result_tight["coverage"] >= result_wide["coverage"]


def test_sweep_wide_thresholds_increase_accuracy():
    """Wider thresholds should yield higher accuracy (only strong signals pass)."""
    # Mix of close-to-50 (noisy) and far-from-50 (clear) signals
    composites = [52.0] * 30 + [70.0] * 30 + [48.0] * 20 + [30.0] * 20
    # Close signals are random, far signals are correct
    returns_24h = [-1.0] * 30 + [4.0] * 30 + [1.0] * 20 + [-4.0] * 20
    returns_48h = returns_24h

    # Wide thresholds filter out the noisy close-to-50 signals
    result_wide = sweep_abstain_thresholds(
        composite_scores=composites,
        forward_returns_24h=returns_24h,
        forward_returns_48h=returns_48h,
        noise_threshold=1.0,
        strong_threshold=3.0,
        atr_pct=2.0,
        bearish_range=[10],
        bullish_range=[10],
        regime_mult_range=[1.0],
    )

    # Tight thresholds let everything through (including noisy signals)
    result_tight = sweep_abstain_thresholds(
        composite_scores=composites,
        forward_returns_24h=returns_24h,
        forward_returns_48h=returns_48h,
        noise_threshold=1.0,
        strong_threshold=3.0,
        atr_pct=2.0,
        bearish_range=[2],
        bullish_range=[2],
        regime_mult_range=[1.0],
    )

    # Wide thresholds should have >= accuracy since noisy signals are filtered
    assert result_wide["accuracy_24h"] >= result_tight["accuracy_24h"]


def test_sweep_empty_input():
    """Empty input returns zero scores."""
    result = sweep_abstain_thresholds(
        composite_scores=[],
        forward_returns_24h=[],
        forward_returns_48h=[],
        noise_threshold=1.0,
        strong_threshold=3.0,
        atr_pct=2.0,
    )
    assert result["combined_score"] == 0.0
    assert result["combos_tested"] == 0


def test_sweep_custom_ranges():
    """Custom ranges are respected."""
    composites, returns_24h, returns_48h = _make_signals(20, 60.0, 2.0, 3.0)

    result = sweep_abstain_thresholds(
        composite_scores=composites,
        forward_returns_24h=returns_24h,
        forward_returns_48h=returns_48h,
        noise_threshold=1.0,
        strong_threshold=3.0,
        atr_pct=2.0,
        bearish_range=[3, 5],
        bullish_range=[4, 6],
        regime_mult_range=[1.0, 1.5],
    )

    # 2 * 2 * 2 = 8 combos
    assert result["combos_tested"] == 8
    assert result["best_bearish_distance"] in [3, 5]
    assert result["best_bullish_distance"] in [4, 6]
    assert result["best_regime_multiplier"] in [1.0, 1.5]
