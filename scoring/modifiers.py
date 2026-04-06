# scoring/modifiers.py
"""Scoring modifiers — regime, abstain, targets, labels.

All pure functions. No state, no side effects.
"""
from __future__ import annotations
from typing import Optional
from scoring.types import RegimeContext, TargetLevels


def classify_fg(fg_value: int, thresholds: dict[str, int]) -> str:
    if fg_value <= thresholds["extreme_fear"]:
        return "extreme_fear"
    elif fg_value <= thresholds["fear"]:
        return "fear"
    elif fg_value <= thresholds["neutral"]:
        return "neutral"
    elif fg_value <= thresholds["greed"]:
        return "greed"
    return "extreme_greed"


def detect_regime(btc_price: float, btc_ma30: float, fg_value: int,
                  fg_thresholds: dict, trending_threshold: float = 0.08,
                  ranging_threshold: float = 0.03,
                  btc_adx: float = 25.0, btc_ma7: float = 0.0,
                  adx_trending: float = 25.0,
                  adx_ranging: float = 20.0) -> RegimeContext:
    """4-regime detection using ADX + price vs MA30.

    Regimes:
      trending_up   — ADX >= adx_trending AND price > MA30
      trending_down — ADX >= adx_trending AND price <= MA30
      ranging       — ADX < adx_ranging
      volatile      — ADX between ranging and trending (transitional)
    """
    pct_from_ma30 = abs((btc_price - btc_ma30) / btc_ma30) if btc_ma30 > 0 else 0
    price_above_ma30 = btc_price > btc_ma30

    if btc_adx >= adx_trending:
        regime = "trending_up" if price_above_ma30 else "trending_down"
    elif btc_adx < adx_ranging:
        regime = "ranging"
    else:
        regime = "volatile"

    fg_regime = classify_fg(fg_value, fg_thresholds)
    return RegimeContext(regime=regime, fg_value=fg_value, fg_regime=fg_regime,
                         btc_pct_from_ma30=round(pct_from_ma30, 4))


def select_weights(raw_avg: float, weights_default: dict[str, float],
                   weights_bullish: dict[str, float],
                   weights_bearish: dict[str, float]) -> dict[str, float]:
    if raw_avg > 50:
        return dict(weights_bullish)
    elif raw_avg < 50:
        return dict(weights_bearish)
    return dict(weights_default)


def apply_regime_shifts(weights: dict[str, float],
                        shifts: dict[str, float]) -> dict[str, float]:
    adjusted = {}
    for dim, w in weights.items():
        adjusted[dim] = w * shifts.get(dim, 1.0)
    total = sum(adjusted.values())
    if total > 0:
        adjusted = {k: v / total for k, v in adjusted.items()}
    return adjusted


def apply_tier_redistribution(weights: dict[str, float],
                               tiers: dict[str, str],
                               multipliers: dict[str, float]) -> dict[str, float]:
    effective = {}
    freed = 0.0
    full_dims = []

    for dim, w in weights.items():
        tier = tiers.get(dim, "partial")
        mult = multipliers.get(tier, 0.5)
        eff_w = w * mult
        freed += w - eff_w
        effective[dim] = eff_w
        if tier == "full":
            full_dims.append(dim)

    if full_dims and freed > 0:
        full_total = sum(effective[d] for d in full_dims)
        if full_total > 0:
            for d in full_dims:
                share = effective[d] / full_total
                effective[d] += freed * share

    total = sum(effective.values())
    if total > 0:
        effective = {k: v / total for k, v in effective.items()}

    return effective


def check_abstain(composite: float, bearish_dist: float, bullish_dist: float,
                  regime_multiplier: float = 1.0) -> bool:
    eff_bearish = bearish_dist * regime_multiplier
    eff_bullish = bullish_dist * regime_multiplier

    if composite == 50:
        return True
    if composite < 50 and (50 - composite) < eff_bearish:
        return True
    if composite > 50 and (composite - 50) < eff_bullish:
        return True
    return False


def assign_label(composite: float, labels: list[dict]) -> tuple[str, str]:
    for entry in labels:
        if composite >= entry["min_score"]:
            name = entry["name"]
            if "BUY" in name:
                return name, "bullish"
            elif "SELL" in name:
                return name, "bearish"
            return name, "neutral"
    return "STRONG SELL", "bearish"


def calculate_targets(entry_price: float, composite: float, direction: str,
                      atr_14: float, sl_multiplier: float,
                      cfg: dict,
                      sr_levels: Optional[dict] = None,
                      atr_percentile: Optional[float] = None) -> Optional[TargetLevels]:
    """Calculate TP/SL using support/resistance levels when available.

    S/R levels (from price structure):
    - ma7, ma30: dynamic support/resistance
    - bb_upper, bb_lower: volatility-based levels
    - swing_high, swing_low: recent swing points
    - ATR: fallback when no S/R data

    atr_percentile: 0-1 value indicating where current ATR sits relative to
        historical range. >0.75 = high volatility (tighten SL), <0.25 = low
        volatility (widen SL). None = no adjustment.
    """
    if direction == "neutral":
        return None

    # Adjust SL multiplier based on ATR percentile (volatility regime)
    if atr_percentile is not None:
        if atr_percentile > 0.75:
            # High volatility: tighten multiplier (stops would be too wide)
            sl_multiplier *= 0.8
        elif atr_percentile < 0.25:
            # Low volatility: widen multiplier (avoid noise stop-outs)
            sl_multiplier *= 1.3

    # --- STOP LOSS: nearest S/R level against direction, or ATR-based ---
    atr_sl = atr_14 * sl_multiplier

    if sr_levels and direction == "bullish":
        # SL below nearest support (ma7, ma30, bb_lower, swing_low)
        supports = []
        for key in ("ma7", "ma30", "bb_lower", "swing_low"):
            level = sr_levels.get(key, 0)
            if 0 < level < entry_price:
                supports.append(level)
        if supports:
            # Use the nearest support below price, minus a small ATR buffer
            nearest_support = max(supports)  # Highest support below price
            stop_loss = nearest_support - atr_14 * 0.3  # Small buffer below support
        else:
            stop_loss = entry_price - atr_sl
    elif sr_levels and direction == "bearish":
        # SL above nearest resistance (ma7, ma30, bb_upper, swing_high)
        resistances = []
        for key in ("ma7", "ma30", "bb_upper", "swing_high"):
            level = sr_levels.get(key, 0)
            if level > entry_price:
                resistances.append(level)
        if resistances:
            nearest_resistance = min(resistances)
            stop_loss = nearest_resistance + atr_14 * 0.3
        else:
            stop_loss = entry_price + atr_sl
    else:
        # Fallback: ATR-based
        if direction == "bullish":
            stop_loss = entry_price - atr_sl
        else:
            stop_loss = entry_price + atr_sl

    # --- TARGET PRICE: nearest S/R level in signal direction ---
    min_rr = cfg.get("min_rr_ratio", 1.5)
    risk = abs(entry_price - stop_loss)
    min_reward = risk * min_rr

    if sr_levels and direction == "bullish":
        # Target at nearest resistance above price
        targets = []
        for key in ("ma30", "bb_upper", "swing_high"):
            level = sr_levels.get(key, 0)
            if level > entry_price + min_reward:
                targets.append(level)
        if targets:
            target_price = min(targets)  # Nearest achievable resistance
        else:
            target_price = entry_price + min_reward
    elif sr_levels and direction == "bearish":
        targets = []
        for key in ("ma30", "bb_lower", "swing_low"):
            level = sr_levels.get(key, 0)
            if 0 < level < entry_price - min_reward:
                targets.append(level)
        if targets:
            target_price = max(targets)
        else:
            target_price = entry_price - min_reward
    else:
        # Fallback: ATR-based target
        atr_pct = (atr_14 / entry_price) * 100 if entry_price > 0 else 3.0
        atr_mult = cfg.get("move_atr_multiplier", 2.5)
        max_factor = cfg.get("move_max_atr_factor", 3.0)
        distance = composite - 50.0
        divisor = cfg.get("move_distance_divisor", 10.0)
        move_fraction = distance / divisor
        predicted_pct = move_fraction * atr_pct * atr_mult
        max_move = atr_pct * max_factor
        predicted_pct = max(-max_move, min(max_move, predicted_pct))

        min_floor = cfg.get("move_min_floor_atr_factor", 0.5)
        min_prediction = atr_pct * min_floor
        if abs(predicted_pct) < min_prediction:
            predicted_pct = min_prediction if direction == "bullish" else -min_prediction

        ml_target = entry_price * (1 + predicted_pct / 100)
        if direction == "bullish":
            target_price = max(ml_target, entry_price + min_reward)
        else:
            target_price = min(ml_target, entry_price - min_reward)

    # Ensure minimum R:R
    reward = abs(target_price - entry_price)
    if reward < min_reward and risk > 0:
        if direction == "bullish":
            target_price = entry_price + min_reward
        else:
            target_price = entry_price - min_reward
        reward = min_reward

    rr_ratio = reward / risk if risk > 0 else 0
    predicted_pct = (target_price - entry_price) / entry_price * 100

    score_distance = abs(composite - 50.0)
    if score_distance > 20:
        confidence = "high"
    elif score_distance > 12:
        confidence = "medium"
    else:
        confidence = "low"

    return TargetLevels(
        entry_price=round(entry_price, 2),
        target_price=round(target_price, 2),
        stop_loss=round(stop_loss, 2),
        risk_reward_ratio=round(rr_ratio, 2),
        predicted_move_pct=round(predicted_pct, 2),
        confidence=confidence,
        timeframe_hours=cfg.get("timeframe_hours", 48),
    )
