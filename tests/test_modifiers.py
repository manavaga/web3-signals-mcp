# tests/test_modifiers.py
from scoring.modifiers import (
    detect_regime, classify_fg, select_weights, apply_regime_shifts,
    apply_tier_redistribution, check_abstain, assign_label, calculate_targets
)
from scoring.types import RegimeContext


def test_detect_regime_trending():
    ctx = detect_regime(btc_price=90000, btc_ma30=82000, fg_value=45, fg_thresholds={"extreme_fear": 20, "fear": 40, "neutral": 60, "greed": 80}, trending_threshold=0.08, ranging_threshold=0.03)
    assert ctx.regime == "trending"
    assert ctx.btc_pct_from_ma30 > 0.08


def test_detect_regime_ranging():
    ctx = detect_regime(btc_price=82500, btc_ma30=82000, fg_value=50, fg_thresholds={"extreme_fear": 20, "fear": 40, "neutral": 60, "greed": 80}, trending_threshold=0.08, ranging_threshold=0.03)
    assert ctx.regime == "ranging"


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
