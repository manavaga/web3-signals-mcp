# tests/test_modifiers.py
from scoring.modifiers import (
    detect_regime, classify_fg, select_weights, apply_regime_shifts,
    apply_tier_redistribution, check_abstain, assign_label, calculate_targets
)
from scoring.types import RegimeContext


def test_detect_regime_trending_up():
    ctx = detect_regime(btc_price=90000, btc_ma30=82000, fg_value=45,
                        fg_thresholds={"extreme_fear": 20, "fear": 40, "neutral": 60, "greed": 80},
                        btc_adx=30.0)
    assert ctx.regime == "trending_up"


def test_detect_regime_trending_down():
    ctx = detect_regime(btc_price=75000, btc_ma30=82000, fg_value=25,
                        fg_thresholds={"extreme_fear": 20, "fear": 40, "neutral": 60, "greed": 80},
                        btc_adx=35.0)
    assert ctx.regime == "trending_down"


def test_detect_regime_ranging():
    ctx = detect_regime(btc_price=82500, btc_ma30=82000, fg_value=50,
                        fg_thresholds={"extreme_fear": 20, "fear": 40, "neutral": 60, "greed": 80},
                        btc_adx=15.0)
    assert ctx.regime == "ranging"


def test_detect_regime_volatile():
    ctx = detect_regime(btc_price=82500, btc_ma30=82000, fg_value=50,
                        fg_thresholds={"extreme_fear": 20, "fear": 40, "neutral": 60, "greed": 80},
                        btc_adx=22.0)
    assert ctx.regime == "volatile"


def test_classify_fg():
    assert classify_fg(15, {"extreme_fear": 20, "fear": 40, "neutral": 60, "greed": 80}) == "extreme_fear"
    assert classify_fg(50, {"extreme_fear": 20, "fear": 40, "neutral": 60, "greed": 80}) == "neutral"
    assert classify_fg(85, {"extreme_fear": 20, "fear": 40, "neutral": 60, "greed": 80}) == "extreme_greed"


def test_select_weights_bullish():
    raw_avg = 62.0
    w = select_weights(raw_avg, {"tech": 0.5, "market": 0.5}, {"tech": 0.4, "market": 0.6}, {"tech": 0.6, "market": 0.4})
    assert w == {"tech": 0.4, "market": 0.6}


def test_select_weights_bearish():
    w = select_weights(38.0, {"tech": 0.5, "market": 0.5}, {"tech": 0.4, "market": 0.6}, {"tech": 0.6, "market": 0.4})
    assert w == {"tech": 0.6, "market": 0.4}


def test_apply_regime_shifts():
    weights = {"tech": 0.5, "market": 0.5}
    shifts = {"tech": 1.2, "market": 0.8}
    result = apply_regime_shifts(weights, shifts)
    assert abs(sum(result.values()) - 1.0) < 0.001


def test_apply_tier_redistribution():
    weights = {"tech": 0.4, "market": 0.4, "deriv": 0.2}
    tiers = {"tech": "full", "market": "full", "deriv": "none"}
    multipliers = {"full": 1.0, "partial": 0.5, "none": 0.0}
    result = apply_tier_redistribution(weights, tiers, multipliers)
    assert result["deriv"] == 0.0
    assert abs(sum(result.values()) - 1.0) < 0.001


def test_check_abstain_within_band():
    assert check_abstain(48.0, 8, 10, 1.0) is True


def test_check_abstain_outside_band():
    assert check_abstain(38.0, 8, 10, 1.0) is False


def test_check_abstain_ranging_widens():
    assert check_abstain(38.0, 8, 10, 3.0) is True


def test_assign_label():
    labels = [
        {"name": "STRONG BUY", "min_score": 70},
        {"name": "MODERATE BUY", "min_score": 60},
        {"name": "NEUTRAL", "min_score": 42},
        {"name": "MODERATE SELL", "min_score": 30},
        {"name": "STRONG SELL", "min_score": 0},
    ]
    assert assign_label(75.0, labels) == ("STRONG BUY", "bullish")
    assert assign_label(63.0, labels) == ("MODERATE BUY", "bullish")
    assert assign_label(50.0, labels) == ("NEUTRAL", "neutral")
    assert assign_label(35.0, labels) == ("MODERATE SELL", "bearish")
    assert assign_label(10.0, labels) == ("STRONG SELL", "bearish")


def test_calculate_targets_bullish():
    targets = calculate_targets(
        entry_price=84000.0, composite=65.0, direction="bullish",
        atr_14=2100.0, sl_multiplier=2.0,
        cfg={"move_distance_divisor": 10.0, "move_atr_multiplier": 1.5,
             "move_max_atr_factor": 2.0, "move_min_floor_atr_factor": 0.3,
             "min_rr_ratio": 0.5, "timeframe_hours": 48}
    )
    assert targets is not None
    assert targets.target_price > targets.entry_price
    assert targets.stop_loss < targets.entry_price
    assert targets.risk_reward_ratio > 0
    assert targets.confidence == "medium"  # distance=15 > 12


def test_calculate_targets_neutral_returns_none():
    assert calculate_targets(84000, 50, "neutral", 2100, 2.0, {}) is None


def test_volatility_scaled_sl_high_vol():
    """High-volatility regime should produce tighter SL multiplier."""
    cfg = {"min_rr_ratio": 1.5, "timeframe_hours": 48}

    # Normal volatility (no percentile)
    targets_normal = calculate_targets(
        entry_price=100, composite=65, direction="bullish",
        atr_14=3.0, sl_multiplier=1.5, cfg=cfg,
    )

    # High volatility (90th percentile)
    targets_high = calculate_targets(
        entry_price=100, composite=65, direction="bullish",
        atr_14=3.0, sl_multiplier=1.5, cfg=cfg, atr_percentile=0.90,
    )

    assert targets_normal is not None and targets_high is not None
    # High-vol should have tighter multiplier -> closer SL
    sl_dist_normal = abs(targets_normal.entry_price - targets_normal.stop_loss)
    sl_dist_high = abs(targets_high.entry_price - targets_high.stop_loss)
    assert sl_dist_high < sl_dist_normal, \
        f"High-vol SL ({sl_dist_high}) should be tighter than normal ({sl_dist_normal})"


def test_volatility_scaled_sl_low_vol():
    """Low-volatility regime should produce wider SL multiplier."""
    cfg = {"min_rr_ratio": 1.5, "timeframe_hours": 48}

    # Normal
    targets_normal = calculate_targets(
        entry_price=100, composite=65, direction="bullish",
        atr_14=1.0, sl_multiplier=1.5, cfg=cfg,
    )

    # Low volatility (10th percentile)
    targets_low = calculate_targets(
        entry_price=100, composite=65, direction="bullish",
        atr_14=1.0, sl_multiplier=1.5, cfg=cfg, atr_percentile=0.10,
    )

    assert targets_normal is not None and targets_low is not None
    sl_dist_normal = abs(targets_normal.entry_price - targets_normal.stop_loss)
    sl_dist_low = abs(targets_low.entry_price - targets_low.stop_loss)
    assert sl_dist_low > sl_dist_normal, \
        f"Low-vol SL ({sl_dist_low}) should be wider than normal ({sl_dist_normal})"


def test_volatility_scaling_no_effect_mid_range():
    """Mid-range ATR percentile should not adjust multiplier."""
    cfg = {"min_rr_ratio": 1.5, "timeframe_hours": 48}

    targets_none = calculate_targets(
        entry_price=100, composite=65, direction="bullish",
        atr_14=2.0, sl_multiplier=1.5, cfg=cfg,
    )
    targets_mid = calculate_targets(
        entry_price=100, composite=65, direction="bullish",
        atr_14=2.0, sl_multiplier=1.5, cfg=cfg, atr_percentile=0.50,
    )

    assert targets_none is not None and targets_mid is not None
    assert targets_none.stop_loss == targets_mid.stop_loss
