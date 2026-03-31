#!/usr/bin/env python3
"""
Backtest script — Re-scores ALL historical agent data using CURRENT YAML config.

This is the proper backtest: it loads raw agent snapshots (technical, whale,
derivatives, narrative, market) from the API history, then re-scores them
using the scoring engine with the current YAML profile. This means YAML
parameter changes are immediately reflected in backtest results.

Usage:
    python3 backtest.py                              # default API
    python3 backtest.py --api-url https://your.app   # custom API URL
"""
from __future__ import annotations

import os
import sys
import json
import urllib.request
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path so we can import the engine
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from shared.profile_loader import load_profile

API_BASE = os.getenv("API_BASE_URL", "https://web3-signals-api-production.up.railway.app")

# ---------------------------------------------------------------------------
# Load YAML profile (the config we're testing)
# ---------------------------------------------------------------------------
PROFILE_PATH = PROJECT_ROOT / "signal_fusion" / "profiles" / "default.yaml"
PROFILE = load_profile(PROFILE_PATH)

# ---------------------------------------------------------------------------
# Gradient scoring (from YAML accuracy config)
# ---------------------------------------------------------------------------
ACCURACY_CFG = PROFILE.get("accuracy", {
    "noise_threshold_pct": 2.0,
    "strong_threshold_pct": 5.0,
    "gradient": {
        "strong_correct": 1.0, "correct": 0.7,
        "weak_correct": 0.4, "weak_wrong": 0.2, "wrong": 0.0,
    },
})


def gradient_score(direction: str, pct_change: float, asset: str = "") -> float:
    """Calculate gradient accuracy score (0.0-1.0).

    Phase B1: Now supports per-asset volatility-adjusted thresholds.
    If per_asset_thresholds is enabled and the asset has custom thresholds,
    use those instead of the global defaults.
    """
    # Per-asset thresholds (Phase B1)
    per_asset_cfg = ACCURACY_CFG.get("per_asset_thresholds", {})
    if per_asset_cfg.get("enabled", False) and asset:
        asset_cfg = per_asset_cfg.get("assets", {}).get(asset.upper(), {})
        noise_pct = float(asset_cfg.get("noise_threshold_pct",
                          ACCURACY_CFG.get("noise_threshold_pct", 2.0)))
        strong_pct = float(asset_cfg.get("strong_threshold_pct",
                           ACCURACY_CFG.get("strong_threshold_pct", 5.0)))
    else:
        noise_pct = float(ACCURACY_CFG.get("noise_threshold_pct", 2.0))
        strong_pct = float(ACCURACY_CFG.get("strong_threshold_pct", 5.0))

    g = ACCURACY_CFG.get("gradient", {})
    effective = pct_change if direction == "bullish" else -pct_change

    if effective >= strong_pct:
        return float(g.get("strong_correct", 1.0))
    elif effective >= noise_pct:
        return float(g.get("correct", 0.7))
    elif effective >= 0:
        return float(g.get("weak_correct", 0.4))
    elif effective >= -noise_pct:
        return float(g.get("weak_wrong", 0.2))
    else:
        return float(g.get("wrong", 0.0))


def gradient_score_custom(direction: str, pct_change: float,
                           noise: float, strong: float) -> float:
    g = ACCURACY_CFG.get("gradient", {})
    effective = pct_change if direction == "bullish" else -pct_change
    if effective >= strong:
        return float(g.get("strong_correct", 1.0))
    elif effective >= noise:
        return float(g.get("correct", 0.7))
    elif effective >= 0:
        return float(g.get("weak_correct", 0.4))
    elif effective >= -noise:
        return float(g.get("weak_wrong", 0.2))
    else:
        return float(g.get("wrong", 0.0))


def binary_correct(direction: str, pct_change: float) -> bool:
    return (pct_change > 0) if direction == "bullish" else (pct_change < 0)


# ---------------------------------------------------------------------------
# Per-dimension scorers (replicate engine.py logic, driven by YAML)
# ---------------------------------------------------------------------------
SCORING_CFG = PROFILE.get("scoring", {})

# ---------------------------------------------------------------------------
# Asset tier helpers (for per-tier scoring overrides)
# ---------------------------------------------------------------------------
TIER_CFG = PROFILE.get("asset_tiers", {})


def get_asset_tier(asset: str) -> str:
    """Determine which tier an asset belongs to. Default: 'contrarian'."""
    if not TIER_CFG.get("enabled", False):
        return "contrarian"
    for tier_name, tier_def in TIER_CFG.get("tiers", {}).items():
        if asset in [a.upper() for a in tier_def.get("assets", [])]:
            return tier_name
    return "contrarian"


def merge_rules(base: Dict, overrides: Dict) -> Dict:
    """Shallow merge: for each key in overrides, if both are dicts, merge sub-keys."""
    merged = dict(base)
    for key, val in overrides.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **val}
        else:
            merged[key] = val
    return merged


def score_technical(asset: str, data: Dict[str, Any]) -> Tuple[float, str]:
    """Score technical dimension for one asset using YAML rules."""
    rules = SCORING_CFG.get("technical", {})

    # Apply asset tier overrides (momentum vs contrarian)
    if TIER_CFG.get("enabled", False):
        tier = get_asset_tier(asset)
        overrides = TIER_CFG.get("technical_overrides", {}).get(tier, {})
        if overrides:
            rules = merge_rules(rules, overrides)

    by_asset = data.get("by_asset", {})
    asset_data = by_asset.get(asset, {})
    if not asset_data:
        return 50.0, "no data"

    score = 0.0
    details: List[str] = []

    # RSI
    rsi_rules = rules.get("rsi", {})
    rsi = asset_data.get("rsi_14")
    if rsi is not None:
        oversold = float(rsi_rules.get("oversold_below", 30))
        overbought = float(rsi_rules.get("overbought_above", 70))
        if rsi < oversold:
            score += float(rsi_rules.get("oversold_score", 30))
            details.append(f"RSI {rsi:.0f} oversold")
        elif rsi > overbought:
            score += float(rsi_rules.get("overbought_score", 10))
            details.append(f"RSI {rsi:.0f} overbought")
        else:
            ratio = (rsi - oversold) / (overbought - oversold) if overbought > oversold else 0.5
            min_s = float(rsi_rules.get("neutral_min_score", 15))
            max_s = float(rsi_rules.get("neutral_max_score", 40))
            score += min_s + ratio * (max_s - min_s)
            details.append(f"RSI {rsi:.0f}")

    # MACD
    macd_rules = rules.get("macd", {})
    macd_val = asset_data.get("macd_line")
    macd_signal = asset_data.get("macd_signal")
    if macd_val is not None and macd_signal is not None:
        if macd_val > macd_signal:
            score += float(macd_rules.get("bullish_cross_points", 20))
            details.append("MACD bullish")
        else:
            score += float(macd_rules.get("bearish_cross_points", 0))
            details.append("MACD bearish")

    # Moving averages
    ma_rules = rules.get("ma", {})
    price = asset_data.get("price")
    ma7 = asset_data.get("ma_7d")
    ma30 = asset_data.get("ma_30d")
    if price is not None and ma7 is not None:
        if price > ma7:
            score += float(ma_rules.get("above_ma7_points", 10))
        else:
            score += float(ma_rules.get("below_ma7_points", 0))
    if price is not None and ma30 is not None:
        if price > ma30:
            score += float(ma_rules.get("above_ma30_points", 10))
            details.append("above MA30")
        else:
            score += float(ma_rules.get("below_ma30_points", 0))

    # Trend
    trend_rules = rules.get("trend", {})
    trend_30d = asset_data.get("trend_30d", "")
    trend_7d = asset_data.get("trend_7d", "")
    trend = trend_30d if trend_30d else trend_7d
    if trend == "bullish":
        score += float(trend_rules.get("bullish_points", 20))
        details.append("trend bullish")
    elif trend == "bearish":
        score += float(trend_rules.get("bearish_points", 0))
        details.append("trend bearish")
    else:
        score += float(trend_rules.get("neutral_points", 10))

    return min(100.0, max(0.0, score)), "; ".join(details) if details else "no tech data"


def score_whale(asset: str, data: Dict[str, Any]) -> Tuple[float, str]:
    """Score whale dimension for one asset using YAML rules.

    Phase B2: Now supports volume_ratio scoring mode (USD-weighted).
    Falls back to count-based ratio if USD amounts unavailable.
    """
    rules = SCORING_CFG.get("whale", {})
    base_score = float(rules.get("base_score", 50))
    score = base_score
    details: List[str] = []

    by_asset = data.get("by_asset", {})
    asset_moves = by_asset.get(asset, [])
    accum_count = sum(1 for m in asset_moves if isinstance(m, dict) and m.get("action") == "accumulate")
    sell_count = sum(1 for m in asset_moves if isinstance(m, dict) and m.get("action") == "sell")

    scoring_mode = str(rules.get("scoring_mode", "ratio"))
    directional = accum_count + sell_count

    if scoring_mode == "volume_ratio" and directional >= int(rules.get("min_directional_moves", 2)):
        # Phase B2: Volume-weighted ratio scoring
        # A $500M accumulation dominates over twenty $100K transfers
        accum_volume = sum(
            float(m.get("amount_usd", 0))
            for m in asset_moves
            if isinstance(m, dict) and m.get("action") == "accumulate"
        )
        sell_volume = sum(
            float(m.get("amount_usd", 0))
            for m in asset_moves
            if isinstance(m, dict) and m.get("action") == "sell"
        )
        total_vol = accum_volume + sell_volume
        if total_vol > 0:
            ratio = accum_volume / total_vol
            max_pts = float(rules.get("ratio_max_points", 60))
            score = ratio * max_pts
            details.append(
                f"${accum_volume/1e6:.1f}M accumulate, ${sell_volume/1e6:.1f}M sell "
                f"(vol ratio {ratio:.0%})")
        else:
            # Fallback to count-based if no USD amounts available
            ratio = accum_count / directional
            max_pts = float(rules.get("ratio_max_points", 60))
            score = ratio * max_pts
            details.append(f"{accum_count} accumulate, {sell_count} sell (count ratio {ratio:.0%})")
    elif scoring_mode == "ratio" and directional >= int(rules.get("min_directional_moves", 2)):
        ratio = accum_count / directional
        max_pts = float(rules.get("ratio_max_points", 60))
        score = ratio * max_pts
        details.append(f"{accum_count} accumulate, {sell_count} sell (ratio {ratio:.0%})")
    elif directional > 0:
        score += accum_count * float(rules.get("accumulate_points", 10))
        score += sell_count * float(rules.get("sell_points", -10))
        details.append(f"{accum_count} accumulate, {sell_count} sell")

    summary = data.get("summary", {})
    net_dir = summary.get("net_exchange_direction", "")
    if net_dir == "net_outflow":
        score += float(rules.get("exchange_outflow_bonus", 10))
        details.append("exchange outflow")
    elif net_dir == "net_inflow":
        score += float(rules.get("exchange_inflow_penalty", -10))
        details.append("exchange inflow")

    wallet_signals = summary.get("whale_wallet_signals", [])
    for ws in wallet_signals:
        if "accumulating" in ws.lower():
            score += float(rules.get("whale_wallet_accumulating_bonus", 8))
        elif "reducing" in ws.lower():
            score += float(rules.get("whale_wallet_reducing_penalty", -8))

    score = max(float(rules.get("min_score", 0)), min(float(rules.get("max_score", 100)), score))
    return score, "; ".join(details) if details else "no whale activity"


def score_derivatives(asset: str, data: Dict[str, Any]) -> Tuple[float, str]:
    """Score derivatives dimension for one asset using YAML rules."""
    rules = SCORING_CFG.get("derivatives", {})
    by_asset = data.get("by_asset", {})
    asset_data = by_asset.get(asset, {})
    if not asset_data:
        return 50.0, "no data"

    score = 0.0
    details: List[str] = []

    # Long/short ratio
    ls_rules = rules.get("long_short", {})
    ls_ratio = asset_data.get("long_short_ratio")
    if ls_ratio is not None:
        sweet_min = float(ls_rules.get("sweet_spot_min", 0.55))
        sweet_max = float(ls_rules.get("sweet_spot_max", 0.65))
        overcrowded = float(ls_rules.get("overcrowded_above", 0.70))
        contrarian = float(ls_rules.get("contrarian_below", 0.45))

        # Check for very_overcrowded first (Step 3 feature)
        very_overcrowded = float(ls_rules.get("very_overcrowded_above", 999))
        if ls_ratio > very_overcrowded:
            score += float(ls_rules.get("very_overcrowded_score", 3))
            details.append(f"L/S {ls_ratio:.2f} very overcrowded")
        elif sweet_min <= ls_ratio <= sweet_max:
            score += float(ls_rules.get("sweet_spot_score", 40))
            details.append(f"L/S {ls_ratio:.2f} sweet spot")
        elif ls_ratio > overcrowded:
            score += float(ls_rules.get("overcrowded_score", 10))
            details.append(f"L/S {ls_ratio:.2f} overcrowded")
        elif ls_ratio < contrarian:
            score += float(ls_rules.get("contrarian_score", 35))
            details.append(f"L/S {ls_ratio:.2f} contrarian")
        else:
            score += float(ls_rules.get("default_score", 25))
            details.append(f"L/S {ls_ratio:.2f}")

    # Funding rate
    fund_rules = rules.get("funding", {})
    funding = asset_data.get("funding_rate")
    funding_tier = None  # Track for combo scoring
    if funding is not None:
        if funding < 0:
            score += float(fund_rules.get("negative_score", 35))
            details.append(f"funding {funding:.5f} negative")
            funding_tier = "negative"
        elif funding < float(fund_rules.get("low_threshold", 0.0002)):
            score += float(fund_rules.get("low_score", 30))
            details.append("low funding")
            funding_tier = "low"
        elif funding < float(fund_rules.get("moderate_threshold", 0.0005)):
            score += float(fund_rules.get("moderate_score", 15))
            funding_tier = "moderate"
        else:
            score += float(fund_rules.get("high_score", 5))
            details.append("high funding")
            funding_tier = "high"

    # Open interest — compare to previous value (mirrors engine.py KV logic)
    oi_rules = rules.get("open_interest", {})
    oi = asset_data.get("open_interest_usd") or asset_data.get("open_interest")
    if oi is not None:
        prev_oi = prev_oi_by_asset.get(asset)
        prev_oi_by_asset[asset] = float(oi)

        if prev_oi is not None and prev_oi > 0:
            oi_change_pct = ((float(oi) - prev_oi) / prev_oi) * 100
            threshold = float(oi_rules.get("change_threshold_pct", 5))
            if oi_change_pct > threshold:
                score += float(oi_rules.get("rising_score", 25))
                details.append(f"OI +{oi_change_pct:.1f}%")
            elif oi_change_pct < -threshold:
                score += float(oi_rules.get("falling_score", 10))
                details.append(f"OI {oi_change_pct:.1f}%")
            else:
                score += float(oi_rules.get("stable_score", 15))
        else:
            score += float(oi_rules.get("stable_score", 15))

    # --- Combo scoring (Step 3 feature, YAML-driven) ---
    if ls_ratio is not None and funding_tier is not None:
        # Overcrowded longs + high funding = crash risk
        overcrowded_threshold = float(ls_rules.get("overcrowded_above", 0.70))
        combo_penalty = float(rules.get("combo_overcrowded_high_funding_penalty", 0))
        if ls_ratio > overcrowded_threshold and funding_tier == "high" and combo_penalty != 0:
            score += combo_penalty
            details.append("combo: overcrowded+high_funding")

        # Contrarian (heavy shorts) + negative funding = squeeze setup
        contrarian_threshold = float(ls_rules.get("contrarian_below", 0.45))
        combo_bonus = float(rules.get("combo_contrarian_negative_funding_bonus", 0))
        if ls_ratio < contrarian_threshold and funding_tier == "negative" and combo_bonus != 0:
            score += combo_bonus
            details.append("combo: contrarian+neg_funding")

    # --- Taker buy/sell ratio (Phase C1) ---
    tr_rules = rules.get("taker_ratio", {})
    if tr_rules.get("enabled", False):
        taker_ratio = asset_data.get("taker_buy_sell_ratio")
        if taker_ratio is not None:
            bullish_t = float(tr_rules.get("bullish_threshold", 1.1))
            bearish_t = float(tr_rules.get("bearish_threshold", 0.9))
            max_pts = float(tr_rules.get("max_points", 10))
            if taker_ratio > bullish_t:
                # Aggressive buyers — bullish signal, scale by intensity
                intensity = min((taker_ratio - bullish_t) / (bullish_t * 0.5), 1.0)
                pts = float(tr_rules.get("bullish_score", 8)) * max(intensity, 0.5)
                score += min(pts, max_pts)
                details.append(f"taker {taker_ratio:.3f} bullish")
            elif taker_ratio < bearish_t:
                # Aggressive sellers — bearish signal, scale by intensity
                intensity = min((bearish_t - taker_ratio) / (bearish_t * 0.5), 1.0)
                pts = float(tr_rules.get("bearish_score", -8)) * max(intensity, 0.5)
                score += max(pts, -max_pts)
                details.append(f"taker {taker_ratio:.3f} bearish")
            else:
                score += float(tr_rules.get("neutral_score", 0))

    return min(100.0, max(0.0, score)), "; ".join(details) if details else "no deriv data"


def score_narrative(asset: str, data: Dict[str, Any]) -> Tuple[float, str]:
    """Score narrative dimension for one asset using YAML rules."""
    rules = SCORING_CFG.get("narrative", {})
    by_asset = data.get("by_asset", {})
    asset_data = by_asset.get(asset, {})
    if not asset_data:
        return 50.0, "no data"

    details: List[str] = []

    # Base score (Step 2 feature)
    score = float(rules.get("narrative_base_score", 0))

    # Component 1: Volume score
    raw_score = float(asset_data.get("normalised_score", 0.0))
    volume_mult = float(rules.get("volume_multiplier", 30))

    # Volume inversion (Step 2 feature): high buzz = low score
    volume_invert = rules.get("volume_invert", False)
    if volume_invert:
        volume_pts = (1.0 - raw_score) * volume_mult
    else:
        volume_pts = raw_score * volume_mult
    score += volume_pts

    if raw_score > 0:
        total_mentions = int(asset_data.get("total_mentions", 0))
        inv_tag = " [inv]" if volume_invert else ""
        details.append(f"vol {raw_score:.2f}{inv_tag} ({total_mentions} mentions)")

    # Quiet bonus (Step 2 feature): low mentions = opportunity
    quiet_threshold = float(rules.get("quiet_threshold", 0))
    quiet_bonus = float(rules.get("quiet_bonus", 0))
    if quiet_threshold > 0 and raw_score < quiet_threshold:
        score += quiet_bonus
        if quiet_bonus != 0:
            details.append("quiet")

    # Component 2: LLM sentiment
    llm_data = asset_data.get("llm_sentiment")
    llm_max = float(rules.get("llm_max_points", 25))
    llm_min_conf = float(rules.get("llm_min_confidence", 0.3))
    if llm_data and isinstance(llm_data, dict):
        llm_sent = float(llm_data.get("sentiment", 0.0))
        llm_conf = float(llm_data.get("confidence", 0.0))
        if llm_conf >= llm_min_conf:
            llm_pts = (llm_sent + 1.0) / 2.0 * llm_max
            score += llm_pts
            tone = llm_data.get("tone", "neutral")
            details.append(f"LLM {tone}")

    # Component 3: Community sentiment
    community = asset_data.get("community_sentiment")
    community_max = float(rules.get("community_max_points", 15))
    if community and isinstance(community, dict):
        cs_score = community.get("score")
        if cs_score is not None:
            community_pts = (float(cs_score) + 1.0) / 2.0 * community_max
            score += community_pts

    # Component 4: Trending bonus (can be negative in Step 2)
    trending = asset_data.get("trending_coingecko", False)
    trending_bonus = float(rules.get("trending_bonus", 10))
    if trending:
        score += trending_bonus
        details.append("trending" if trending_bonus > 0 else "trending [contrarian]")

    # Component 5: Influencer bonus
    inf_count = int(asset_data.get("influencer_mentions", 0))
    inf_threshold = int(rules.get("influencer_threshold", 2))
    inf_bonus = float(rules.get("influencer_bonus", 10))
    if inf_count >= inf_threshold:
        score += inf_bonus
        details.append(f"{inf_count} influencers")

    # Component 6: Multi-source confirmation
    sources_with_data = int(asset_data.get("sources_with_data", 0))
    multi_threshold = int(rules.get("multi_source_threshold", 3))
    multi_bonus = float(rules.get("multi_source_bonus", 10))
    if sources_with_data >= multi_threshold:
        score += multi_bonus

    max_score = float(rules.get("max_score", 100))
    return min(max_score, max(0.0, score)), "; ".join(details) if details else "low buzz"


def score_market(asset: str, data: Dict[str, Any]) -> Tuple[float, str]:
    """Score market dimension for one asset using YAML rules. Bipolar (centered at 50)."""
    rules = SCORING_CFG.get("market", {})
    per_asset = data.get("per_asset", {})
    asset_data = per_asset.get(asset, {})
    details: List[str] = []
    score = float(rules.get("base_score", 0.0))  # Bipolar: start at 50

    # Price change
    pc_rules = rules.get("price_change", {})
    change_24h = asset_data.get("change_24h_pct")
    if change_24h is not None:
        strong_pos = float(pc_rules.get("strong_positive_above", 5.0))
        pos = float(pc_rules.get("positive_above", 0.0))
        mild_neg = float(pc_rules.get("mild_negative_above", -5.0))

        if change_24h > strong_pos:
            score += float(pc_rules.get("strong_positive_score", -5))
            details.append(f"+{change_24h:.1f}% strong")
        elif change_24h > pos:
            score += float(pc_rules.get("positive_score", 0))
            details.append(f"+{change_24h:.1f}%")
        elif change_24h > mild_neg:
            score += float(pc_rules.get("mild_negative_score", 5))
            details.append(f"{change_24h:.1f}%")
        else:
            score += float(pc_rules.get("strong_negative_score", 8))
            details.append(f"{change_24h:.1f}% drop")

    # Volume spike
    vol_rules = rules.get("volume", {})
    vol_ratio = asset_data.get("volume_spike_ratio")
    if vol_ratio is not None:
        spike = float(vol_rules.get("spike_multiplier_above", 2.0))
        elevated = float(vol_rules.get("elevated_multiplier_above", 1.5))
        if vol_ratio > spike:
            score += float(vol_rules.get("spike_score", 30))
            details.append(f"{vol_ratio:.1f}x vol spike")
        elif vol_ratio > elevated:
            score += float(vol_rules.get("elevated_score", 20))
        else:
            score += float(vol_rules.get("normal_score", 10))

    # Fear & Greed (global)
    fg_rules = rules.get("fear_greed", {})
    sentiment = data.get("sentiment", {})
    fg_value = sentiment.get("fear_greed_index")
    if fg_value is not None:
        fg = float(fg_value)
        if fg < float(fg_rules.get("extreme_fear_below", 25)):
            score += float(fg_rules.get("extreme_fear_score", 30))
            details.append(f"F&G {fg:.0f} extreme fear")
        elif fg < float(fg_rules.get("fear_below", 45)):
            score += float(fg_rules.get("fear_score", 25))
            details.append(f"F&G {fg:.0f} fear")
        elif fg < float(fg_rules.get("neutral_below", 55)):
            score += float(fg_rules.get("neutral_score", 15))
        elif fg < float(fg_rules.get("greed_below", 75)):
            score += float(fg_rules.get("greed_score", 10))
        else:
            score += float(fg_rules.get("extreme_greed_score", 5))
            details.append(f"F&G {fg:.0f} extreme greed")

    # BTC Dominance (global, scored differently for BTC vs alts)
    btcd_rules = rules.get("btc_dominance", {})
    if btcd_rules.get("enabled", False):
        global_market = data.get("global_market", {})
        btc_dom = global_market.get("btc_dominance") if global_market else None
        if btc_dom is not None:
            prev_btc_dom = prev_btc_dom_val.get("__global__")
            prev_btc_dom_val["__global__"] = float(btc_dom)

            is_btc = (asset == "BTC")
            threshold = float(btcd_rules.get("change_threshold_pct", 0.3))

            if prev_btc_dom is not None and prev_btc_dom > 0:
                btcd_change = btc_dom - prev_btc_dom
                if btcd_change > threshold:
                    key = "btc_rising_score" if is_btc else "alt_rising_score"
                    score += float(btcd_rules.get(key, 10))
                    tag = "bullish" if is_btc else "bearish"
                    details.append(f"BTC.D +{btcd_change:.1f}% {tag}")
                elif btcd_change < -threshold:
                    key = "btc_falling_score" if is_btc else "alt_falling_score"
                    score += float(btcd_rules.get(key, 10))
                    tag = "bearish" if is_btc else "alt season"
                    details.append(f"BTC.D {btcd_change:.1f}% {tag}")
                else:
                    key = "btc_stable_score" if is_btc else "alt_stable_score"
                    score += float(btcd_rules.get(key, 10))
            else:
                key = "btc_stable_score" if is_btc else "alt_stable_score"
                score += float(btcd_rules.get(key, 10))

    # Trend awareness penalty: fear + price drop confirming = genuine downtrend
    ta_rules = rules.get("trend_awareness", {})
    if ta_rules.get("enabled", False):
        fg_t = float(ta_rules.get("fg_threshold", 35))
        drop_t = float(ta_rules.get("drop_threshold", -2.0))
        max_pen = float(ta_rules.get("max_penalty", -30))

        sentiment = data.get("sentiment", {})
        fg_val = sentiment.get("fear_greed_index")
        chg = asset_data.get("change_24h_pct")

        if fg_val is not None and chg is not None:
            fg_f = float(fg_val)
            chg_f = float(chg)
            if fg_f < fg_t and chg_f < drop_t:
                fg_intensity = (fg_t - fg_f) / fg_t
                drop_intensity = min(abs(chg_f) / 10.0, 1.0)
                penalty = fg_intensity * drop_intensity * max_pen
                score += penalty
                details.append(f"downtrend penalty {penalty:.0f}")

    return min(100.0, max(0.0, score)), "; ".join(details) if details else "no market data"


# ---------------------------------------------------------------------------
# BTC dominance state tracking (mirrors engine.py's KV storage approach)
# ---------------------------------------------------------------------------
prev_btc_dom_val: Dict[str, float] = {}

def score_trend(asset: str, data: Dict[str, Any], market_data: Optional[Dict[str, Any]] = None) -> Tuple[float, str]:
    """Score trend dimension for one asset using YAML rules. PRO-TREND (not contrarian)."""
    rules = SCORING_CFG.get("trend", {})
    by_asset = data.get("by_asset", {})
    asset_data = by_asset.get(asset, {})
    details: List[str] = []
    score = 50.0  # Start neutral

    # Market data for price change
    market_asset_data = {}
    if market_data:
        market_asset_data = market_data.get("per_asset", {}).get(asset, {})

    # Component 1: MA Alignment
    ma_rules = rules.get("ma_alignment", {})
    price = asset_data.get("price")
    ma_7d = asset_data.get("ma_7d")
    ma_30d = asset_data.get("ma_30d")

    if price is not None and ma_7d is not None and ma_30d is not None:
        if price > ma_7d and ma_7d > ma_30d:
            score += float(ma_rules.get("bullish_chain_score", 15))
            details.append("MA bullish chain")
        elif price < ma_7d and ma_7d < ma_30d:
            score += float(ma_rules.get("bearish_chain_score", -15))
            details.append("MA bearish chain")
        elif price > ma_30d:
            score += float(ma_rules.get("partial_bullish_score", 8))
            details.append("above MA30")
        elif price < ma_30d:
            score += float(ma_rules.get("partial_bearish_score", -8))
            details.append("below MA30")

    # Component 2: RSI Momentum (pro-trend)
    rsi_rules = rules.get("rsi_momentum", {})
    rsi = asset_data.get("rsi_14")
    if rsi is not None:
        if rsi > float(rsi_rules.get("strong_bullish_above", 65)):
            score += float(rsi_rules.get("strong_bullish_score", 12))
            details.append(f"RSI {rsi:.0f} strong momentum")
        elif rsi > float(rsi_rules.get("bullish_above", 55)):
            score += float(rsi_rules.get("bullish_score", 6))
            details.append(f"RSI {rsi:.0f} momentum")
        elif rsi < float(rsi_rules.get("strong_bearish_below", 35)):
            score += float(rsi_rules.get("strong_bearish_score", -12))
            details.append(f"RSI {rsi:.0f} strong downward")
        elif rsi < float(rsi_rules.get("bearish_below", 45)):
            score += float(rsi_rules.get("bearish_score", -6))
            details.append(f"RSI {rsi:.0f} downward")

    # Component 3: Price Change Direction (pro-trend)
    pc_rules = rules.get("price_change", {})
    change_24h = market_asset_data.get("change_24h_pct")
    if change_24h is not None:
        if change_24h > float(pc_rules.get("strong_positive_above", 5.0)):
            score += float(pc_rules.get("strong_positive_score", 10))
            details.append(f"+{change_24h:.1f}% strong up")
        elif change_24h > float(pc_rules.get("positive_above", 1.0)):
            score += float(pc_rules.get("positive_score", 5))
            details.append(f"+{change_24h:.1f}%")
        elif change_24h < float(pc_rules.get("strong_negative_below", -5.0)):
            score += float(pc_rules.get("strong_negative_score", -10))
            details.append(f"{change_24h:.1f}% strong down")
        elif change_24h < float(pc_rules.get("negative_below", -1.0)):
            score += float(pc_rules.get("negative_score", -5))
            details.append(f"{change_24h:.1f}%")

    # Component 4: Trend Strength (distance from MA30)
    strength_rules = rules.get("trend_strength", {})
    if price is not None and ma_30d is not None and ma_30d > 0:
        pct_from_ma = ((price - ma_30d) / ma_30d) * 100
        strong_above = float(strength_rules.get("strong_above_pct", 10))
        strong_below = float(strength_rules.get("strong_below_pct", -10))
        max_bonus = float(strength_rules.get("max_bonus", 8))
        max_penalty = float(strength_rules.get("max_penalty", -8))
        if pct_from_ma > 0:
            intensity = min(pct_from_ma / strong_above, 1.0)
            score += intensity * max_bonus
        else:
            intensity = min(abs(pct_from_ma) / abs(strong_below), 1.0)
            score += intensity * max_penalty

    score = max(0.0, min(100.0, score))
    return score, "; ".join(details) if details else "no trend data"


SCORERS = {
    "whale": score_whale,
    "technical": score_technical,
    "derivatives": score_derivatives,
    "narrative": score_narrative,
    "market": score_market,
    "trend": score_trend,
}


# ---------------------------------------------------------------------------
# Data tier detection (replicate engine.py logic)
# ---------------------------------------------------------------------------
REWEIGHT_CFG = PROFILE.get("reweighting", {})
REWEIGHT_ENABLED = REWEIGHT_CFG.get("enabled", False)
TIER_MULTIPLIERS = REWEIGHT_CFG.get("tier_multipliers", {"full": 1.0, "partial": 0.5, "none": 0.0})
AGENT_REWEIGHT_RULES = REWEIGHT_CFG.get("agents", {})


def detect_data_tier(role: str, score: float, detail: str) -> str:
    rules = AGENT_REWEIGHT_RULES.get(role, {})
    detail_lower = detail.lower()

    if detail_lower.startswith("error:"):
        return "none"

    no_data_kws = [kw.lower() for kw in rules.get("no_data_keywords", ["no data", "no scorer"])]
    if any(kw in detail_lower for kw in no_data_kws):
        return "none"

    none_below = rules.get("none_if_score_below")
    if none_below is not None and score <= float(none_below):
        return "none"

    full_data_kws = [kw.lower() for kw in rules.get("full_data_keywords", [])]
    if full_data_kws:
        if any(kw in detail_lower for kw in full_data_kws):
            return "full"
        return "partial"

    partial_below = rules.get("partial_if_score_below")
    if partial_below is not None and score < float(partial_below):
        return "partial"

    partial_kws = [kw.lower() for kw in rules.get("partial_keywords", [])]
    if partial_kws and all(
        any(pk in part.lower() for pk in partial_kws)
        for part in detail.split("; ") if part.strip()
    ) and detail.strip():
        return "partial"

    return "full"


# ---------------------------------------------------------------------------
# Composite scoring (replicate engine.py fuse() logic)
# ---------------------------------------------------------------------------
# Direction-aware asymmetric weighting
ASYM_CFG = PROFILE.get("weights_asymmetric", {})
ASYM_ENABLED = ASYM_CFG.get("enabled", False)
WEIGHTS_DEFAULT = ASYM_CFG.get("default", PROFILE.get("weights", {}))
WEIGHTS_BULLISH = ASYM_CFG.get("bullish", WEIGHTS_DEFAULT)
WEIGHTS_BEARISH = ASYM_CFG.get("bearish", WEIGHTS_DEFAULT)
if not ASYM_ENABLED:
    WEIGHTS_DEFAULT = PROFILE.get("weights", {})
    WEIGHTS_BULLISH = WEIGHTS_DEFAULT
    WEIGHTS_BEARISH = WEIGHTS_DEFAULT

LABEL_CFG = PROFILE.get("labels", [])
CONVICTION_CFG = PROFILE.get("conviction", {})
ABSTAIN_CFG = PROFILE.get("abstain", {})
SCALING_CFG = PROFILE.get("accuracy_scaling", {})
ALL_ROLES = ["whale", "technical", "derivatives", "narrative", "market", "trend"]


def classify(score: float) -> Tuple[str, str]:
    for entry in LABEL_CFG:
        if score >= float(entry.get("min_score", 0)):
            return entry.get("name", "UNKNOWN"), entry.get("direction", "neutral")
    return "STRONG SELL", "sell"


# ---------------------------------------------------------------------------
# OI state tracking (mirrors engine.py's KV storage approach)
# ---------------------------------------------------------------------------
prev_oi_by_asset: Dict[str, float] = {}

DELTA_CFG = PROFILE.get("delta_scoring", {})

# Lazy-init delta scorer
_delta_scorer = None
def _get_delta_scorer():
    global _delta_scorer
    if _delta_scorer is None and DELTA_CFG.get("enabled", False):
        from signal_fusion.delta import DeltaScorer
        _delta_scorer = DeltaScorer(PROFILE)
    return _delta_scorer


REGIME_CFG = PROFILE.get("regime_weighting", {})
CONFIDENCE_CFG = PROFILE.get("confidence", {})


def detect_regime(snapshot: Dict[str, Optional[Dict[str, Any]]]) -> Tuple[str, Dict[str, float]]:
    """Detect market regime (trending/ranging/unknown) from BTC data."""
    if not REGIME_CFG.get("enabled", False):
        return "unknown", {}

    det_cfg = REGIME_CFG.get("detection", {})
    trending_t = float(det_cfg.get("trending_threshold", 0.08))
    ranging_t = float(det_cfg.get("ranging_threshold", 0.03))

    tech_data = snapshot.get("technical")
    market_data = snapshot.get("market")

    btc_price = None
    btc_ma30 = None
    btc_ma7 = None

    if market_data:
        btc_price = market_data.get("data", {}).get("per_asset", {}).get("BTC", {}).get("price")
    if tech_data:
        btc_ma30 = tech_data.get("data", {}).get("by_asset", {}).get("BTC", {}).get("ma_30d")
        btc_ma7 = tech_data.get("data", {}).get("by_asset", {}).get("BTC", {}).get("ma_7d")

    if btc_price is None or btc_ma30 is None or btc_ma30 <= 0:
        return "unknown", {}

    pct_from_ma30 = abs((btc_price - btc_ma30) / btc_ma30)
    ma_aligned = True
    if det_cfg.get("require_ma_alignment", True) and btc_ma7 is not None:
        price_above = btc_price > btc_ma30
        ma7_above = btc_ma7 > btc_ma30
        ma_aligned = (price_above == ma7_above)

    if pct_from_ma30 > trending_t and ma_aligned:
        shifts = {k: float(v) for k, v in REGIME_CFG.get("trending", {}).items()}
        return "trending", shifts
    elif pct_from_ma30 < ranging_t:
        shifts = {k: float(v) for k, v in REGIME_CFG.get("ranging", {}).items()}
        return "ranging", shifts

    return "unknown", {}


def compute_composite(
    asset: str,
    agent_snapshots: Dict[str, Optional[Dict[str, Any]]],
    prev_dimensions: Optional[Dict[str, Dict[str, Any]]] = None,
    regime_shifts: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Re-score a single asset using current YAML config. Returns signal dict."""

    raw_scores: Dict[str, Tuple[float, str]] = {}
    for role in ALL_ROLES:
        # Trend dimension reads from technical agent (no dedicated trend agent)
        if role == "trend":
            agent_data = agent_snapshots.get("technical")
        else:
            agent_data = agent_snapshots.get(role)
        if agent_data is None:
            raw_scores[role] = (50.0, "no data")
        else:
            data = agent_data.get("data", {})
            scorer = SCORERS.get(role)
            if scorer:
                try:
                    if role == "trend":
                        # Trend scorer needs market data too
                        market_snap = agent_snapshots.get("market")
                        market_data = market_snap.get("data", {}) if market_snap else None
                        raw_scores[role] = scorer(asset, data, market_data)
                    else:
                        raw_scores[role] = scorer(asset, data)
                except Exception as e:
                    raw_scores[role] = (50.0, f"error: {e}")
            else:
                raw_scores[role] = (50.0, "no scorer")

    # Data tier detection
    data_tiers: Dict[str, str] = {}
    for role in ALL_ROLES:
        if not REWEIGHT_ENABLED:
            data_tiers[role] = "full"
        else:
            s, d = raw_scores[role]
            data_tiers[role] = detect_data_tier(role, s, d)

    # Direction-aware weight selection
    raw_avg = sum(raw_scores[r][0] for r in ALL_ROLES) / len(ALL_ROLES)
    if ASYM_ENABLED:
        if raw_avg > 50:
            selected_weights = WEIGHTS_BULLISH
        elif raw_avg < 50:
            selected_weights = WEIGHTS_BEARISH
        else:
            selected_weights = WEIGHTS_DEFAULT
    else:
        selected_weights = WEIGHTS_DEFAULT

    # Adjusted weights
    base_weights = {role: float(selected_weights.get(role, 0.0)) for role in ALL_ROLES}

    # Accuracy scaling: multiply each dimension's weight by directional accuracy
    if SCALING_CFG.get("enabled", False):
        multipliers = SCALING_CFG.get("multipliers", {})
        min_mult = float(SCALING_CFG.get("min_multiplier", 0.15))
        direction_lean = "bullish" if raw_avg > 50 else "bearish"
        for role in ALL_ROLES:
            role_mults = multipliers.get(role, {})
            accuracy = float(role_mults.get(direction_lean, 0.50))
            accuracy = max(accuracy, min_mult)
            base_weights[role] *= accuracy
        # Renormalize to sum to 1.0
        total_w = sum(base_weights.values())
        if total_w > 0:
            for role in ALL_ROLES:
                base_weights[role] = base_weights[role] / total_w

    # Regime-aware weight shifts (after accuracy scaling, before tier multipliers)
    if regime_shifts:
        for role in ALL_ROLES:
            shift = float(regime_shifts.get(role, 1.0))
            base_weights[role] *= shift
        total_w = sum(base_weights.values())
        if total_w > 0:
            for role in ALL_ROLES:
                base_weights[role] = base_weights[role] / total_w

    adjusted_weights: Dict[str, float] = {}
    total_freed = 0.0
    full_data_roles: List[str] = []

    for role in ALL_ROLES:
        tier = data_tiers[role]
        mult = float(TIER_MULTIPLIERS.get(tier, 1.0))
        effective_w = base_weights[role] * mult
        adjusted_weights[role] = effective_w
        freed = base_weights[role] - effective_w
        total_freed += freed
        if mult >= 1.0:
            full_data_roles.append(role)

    if total_freed > 0 and full_data_roles:
        full_data_sum = sum(base_weights[r] for r in full_data_roles)
        if full_data_sum > 0:
            for role in full_data_roles:
                adjusted_weights[role] += total_freed * (base_weights[role] / full_data_sum)

    # Phase C3: Data quality gating — reduce weight of no-data dimensions
    dq_cfg = PROFILE.get("data_quality_gating", {})
    if dq_cfg.get("enabled", False):
        no_data_penalty = float(dq_cfg.get("no_data_weight_penalty", 0.3))
        dims_with_data = sum(1 for r in ALL_ROLES if data_tiers[r] != "none")

        # Reduce weight of no-data dimensions
        for role in ALL_ROLES:
            if data_tiers[role] == "none":
                adjusted_weights[role] *= no_data_penalty

        # Renormalize weights to sum to 1.0
        total_w = sum(adjusted_weights.values())
        if total_w > 0:
            for role in ALL_ROLES:
                adjusted_weights[role] = adjusted_weights[role] / total_w

    # Compute composite
    dimensions: Dict[str, Dict[str, Any]] = {}
    composite = 0.0

    for role in ALL_ROLES:
        s, detail = raw_scores[role]
        label_name, direction = classify(s)
        adj_w = adjusted_weights[role]
        dimensions[role] = {
            "score": round(s, 1),
            "label": label_name,
            "detail": detail,
            "weight": round(adj_w, 3),
            "data_tier": data_tiers[role],
        }
        composite += s * adj_w

    composite = round(composite, 1)

    # Phase C2: Cross-dimensional features — score adjustments based on
    # multi-dimensional patterns (applied AFTER composite, BEFORE abstain)
    cross_dim_cfg = PROFILE.get("cross_dimensional", {})
    cross_dim_adjustment = 0.0
    if cross_dim_cfg.get("enabled", False):
        # OI-Price Divergence: derivatives high (bullish setup) + market low (price dropping)
        oi_div = cross_dim_cfg.get("oi_price_divergence", {})
        if oi_div.get("enabled", False):
            deriv_score = raw_scores.get("derivatives", (50, ""))[0]
            market_score = raw_scores.get("market", (50, ""))[0]
            if (deriv_score > float(oi_div.get("derivatives_high_threshold", 60)) and
                    market_score < float(oi_div.get("market_low_threshold", 45))):
                cross_dim_adjustment += float(oi_div.get("penalty", -4))

        # Whale-Derivatives Bearish Confluence
        wd_bear = cross_dim_cfg.get("whale_derivatives_bearish", {})
        if wd_bear.get("enabled", False):
            whale_score = raw_scores.get("whale", (50, ""))[0]
            deriv_score = raw_scores.get("derivatives", (50, ""))[0]
            if (whale_score < float(wd_bear.get("whale_low_threshold", 35)) and
                    deriv_score < float(wd_bear.get("derivatives_low_threshold", 35))):
                cross_dim_adjustment += float(wd_bear.get("penalty", -5))

        # Multi-Dimension Bearish Agreement
        multi_bear = cross_dim_cfg.get("multi_dim_bearish", {})
        if multi_bear.get("enabled", False):
            bearish_thresh = float(multi_bear.get("bearish_threshold", 45))
            min_agree = int(multi_bear.get("min_agreeing", 4))
            bearish_dims = sum(1 for r in ALL_ROLES if raw_scores[r][0] < bearish_thresh)
            if bearish_dims >= min_agree:
                cross_dim_adjustment += float(multi_bear.get("penalty", -6))

        # Technical-Market Bearish Alignment
        tm_bear = cross_dim_cfg.get("tech_market_bearish", {})
        if tm_bear.get("enabled", False):
            tech_score = raw_scores.get("technical", (50, ""))[0]
            market_score = raw_scores.get("market", (50, ""))[0]
            if (tech_score < float(tm_bear.get("technical_threshold", 40)) and
                    market_score < float(tm_bear.get("market_threshold", 42))):
                cross_dim_adjustment += float(tm_bear.get("penalty", -3))

        if cross_dim_adjustment != 0:
            composite = round(max(0.0, min(100.0, composite + cross_dim_adjustment)), 1)

    # Phase C3 continued: Data quality gate — require minimum dimensions with data
    data_quality_abstain = False
    if dq_cfg.get("enabled", False):
        min_dims = int(dq_cfg.get("min_dimensions_with_data", 3))
        dims_with_data = sum(1 for r in ALL_ROLES if data_tiers[r] != "none")
        if dims_with_data < min_dims:
            data_quality_abstain = True

    # Conviction multiplier
    conviction_applied = False
    if CONVICTION_CFG.get("enabled", True):
        min_agreeing = int(CONVICTION_CFG.get("min_agreeing_dimensions", 3))
        boost_factor = float(CONVICTION_CFG.get("boost_factor", 1.25))
        center = 50.0

        bullish_count = sum(1 for r in ALL_ROLES if raw_scores[r][0] > 55)
        bearish_count = sum(1 for r in ALL_ROLES if raw_scores[r][0] < 45)

        if bullish_count >= min_agreeing and composite > center:
            distance = composite - center
            composite = round(center + distance * boost_factor, 1)
            conviction_applied = True
        elif bearish_count >= min_agreeing and composite < center:
            distance = center - composite
            composite = round(center - distance * boost_factor, 1)
            conviction_applied = True

        composite = round(max(0.0, min(100.0, composite)), 1)

    # Delta scoring (Step 6 feature)
    ds = _get_delta_scorer()
    if ds and ds.is_enabled() and prev_dimensions is not None:
        delta_composite, _ = ds.compute_delta_composite(asset, dimensions, prev_dimensions)
        if delta_composite is not None:
            composite = ds.blend(composite, delta_composite)

    # Confidence scoring: multi-factor quality gate
    confidence_score = None
    confidence_suppressed = False
    if CONFIDENCE_CFG.get("enabled", False):
        factors_cfg = CONFIDENCE_CFG.get("factors", {})
        conf_threshold = float(CONFIDENCE_CFG.get("threshold", 35))

        # Factor 1: Dimension agreement
        da_weight = float(factors_cfg.get("dimension_agreement", {}).get("weight", 0.35))
        if composite > 50:
            agreeing = sum(1 for r in ALL_ROLES if raw_scores[r][0] > 50)
        else:
            agreeing = sum(1 for r in ALL_ROLES if raw_scores[r][0] < 50)
        da_score = (agreeing / len(ALL_ROLES)) * 100

        # Factor 2: Signal strength
        ss_cfg = factors_cfg.get("signal_strength", {})
        ss_weight = float(ss_cfg.get("weight", 0.25))
        max_dist = float(ss_cfg.get("max_distance", 20))
        ss_score = min(abs(composite - 50) / max_dist, 1.0) * 100

        # Factor 3: Data quality
        dq_weight = float(factors_cfg.get("data_quality", {}).get("weight", 0.25))
        full_count = sum(1 for r in ALL_ROLES if data_tiers.get(r) == "full")
        dq_score = (full_count / len(ALL_ROLES)) * 100

        # Factor 4: Velocity alignment (simplified — use 50 as default)
        va_weight = float(factors_cfg.get("velocity_alignment", {}).get("weight", 0.15))
        va_score = 50.0  # No velocity data in backtest; neutral contribution

        confidence_score = round(
            da_score * da_weight + ss_score * ss_weight +
            dq_score * dq_weight + va_score * va_weight, 1
        )
        if confidence_score < conf_threshold:
            confidence_suppressed = True

    # Abstain check (Step 4 feature) — with DYNAMIC zones based on F&G
    abstain_applied = False
    if data_quality_abstain:
        # Phase C3: Data quality gate — insufficient dimensions with data
        abstain_applied = True
        label_name = "INSUFFICIENT DATA"
        direction = "neutral"
    elif confidence_suppressed:
        # Confidence gate overrides: force to neutral
        abstain_applied = True
        label_name = "INSUFFICIENT EDGE"
        direction = "neutral"
    elif ABSTAIN_CFG.get("enabled", False):
        base_distance = float(ABSTAIN_CFG.get("min_distance_from_center", 8))
        resolved_distance = base_distance

        # Dynamic abstain: narrow the band in extreme conditions
        dynamic_cfg = ABSTAIN_CFG.get("dynamic", {})
        if dynamic_cfg.get("enabled", False):
            # Extract F&G from market agent data
            market_snap = agent_snapshots.get("market")
            fg_val = None
            if market_snap:
                fg_val = market_snap.get("data", {}).get("sentiment", {}).get("fear_greed_index")
            if fg_val is not None:
                fg_val = float(fg_val)
                for zone in dynamic_cfg.get("zones", []):
                    if zone.get("fg_min", 0) <= fg_val < zone.get("fg_max", 100):
                        resolved_distance = float(zone.get("threshold", base_distance))
                        break
                if fg_val >= 100:
                    zones = dynamic_cfg.get("zones", [])
                    if zones:
                        resolved_distance = float(zones[-1].get("threshold", base_distance))

        # Phase A1: Asymmetric abstain zones
        asym_abstain = ABSTAIN_CFG.get("asymmetric", {})
        if asym_abstain.get("enabled", False):
            bearish_dist = float(asym_abstain.get("bearish_min_distance", resolved_distance))
            bullish_dist = float(asym_abstain.get("bullish_min_distance", resolved_distance))
            if composite < 50.0 and (50.0 - composite) < bearish_dist:
                abstain_applied = True
            elif composite > 50.0 and (composite - 50.0) < bullish_dist:
                abstain_applied = True
            elif composite == 50.0:
                abstain_applied = True
        else:
            if abs(composite - 50.0) < resolved_distance:
                abstain_applied = True

        if abstain_applied:
            label_name = ABSTAIN_CFG.get("abstain_label", "INSUFFICIENT EDGE")
            direction = "neutral"
        else:
            label_name, direction = classify(composite)
    else:
        label_name, direction = classify(composite)

    # Normalize direction labels to bullish/bearish/neutral for accuracy eval
    # (classify() returns "buy"/"sell"/"neutral" from YAML labels)
    if direction == "buy":
        direction = "bullish"
    elif direction == "sell":
        direction = "bearish"

    return {
        "composite_score": composite,
        "label": label_name,
        "direction": direction,
        "dimensions": dimensions,
        "data_tiers": data_tiers,
        "conviction_boost": conviction_applied,
        "abstain": abstain_applied,
    }


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def api_get(path: str) -> Any:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  API error on {path}: {e}")
        return None


def load_agent_history(agent_name: str) -> List[Dict]:
    all_rows = []
    offset = 0
    batch = 200
    while True:
        data = api_get(f"/api/history?agent={agent_name}&limit={batch}&offset={offset}")
        if not data or not data.get("rows"):
            break
        rows = data["rows"]
        all_rows.extend(rows)
        if len(rows) < batch:
            break
        offset += batch
        if offset > 5000:
            break
    return all_rows


def parse_timestamp(ts_str: str) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Build aligned snapshots — match agent data by timestamp
# ---------------------------------------------------------------------------
def build_aligned_snapshots(
    agent_histories: Dict[str, List[Dict]],
) -> List[Tuple[datetime, Dict[str, Optional[Dict[str, Any]]]]]:
    """
    Align agent snapshots by timestamp. For each unique ~15min window,
    find the closest snapshot from each agent.
    """
    # Collect all timestamps from all agents
    all_timestamps: List[datetime] = []
    agent_indexed: Dict[str, List[Tuple[datetime, Dict]]] = {}

    for agent_name, rows in agent_histories.items():
        indexed = []
        for row in rows:
            ts = parse_timestamp(row.get("timestamp", ""))
            if ts is not None:
                indexed.append((ts, row.get("data", {})))
                all_timestamps.append(ts)
        indexed.sort(key=lambda x: x[0])
        agent_indexed[agent_name] = indexed

    if not all_timestamps:
        return []

    # Deduplicate to ~15min buckets using market agent timestamps as anchor
    market_ts = agent_indexed.get("market", [])
    if not market_ts:
        # Fall back to any agent
        for v in agent_indexed.values():
            if v:
                market_ts = v
                break

    # For each market timestamp, find closest snapshot from each agent
    aligned = []
    max_gap = timedelta(minutes=30)

    for ts, _ in market_ts:
        snapshot: Dict[str, Optional[Dict[str, Any]]] = {}
        for agent_name, indexed in agent_indexed.items():
            # Find closest
            best = None
            best_delta = max_gap
            for a_ts, a_data in indexed:
                delta = abs(a_ts - ts)
                if delta < best_delta:
                    best_delta = delta
                    best = a_data
            snapshot[agent_name] = best
        aligned.append((ts, snapshot))

    return aligned


# ---------------------------------------------------------------------------
# Build price timeline from market agent data
# ---------------------------------------------------------------------------
def build_price_timeline(market_rows: List[Dict]) -> Dict[str, List[Tuple[datetime, float]]]:
    timeline: Dict[str, List[Tuple[datetime, float]]] = defaultdict(list)

    for row in market_rows:
        ts = parse_timestamp(row.get("timestamp", ""))
        if ts is None:
            continue
        data = row.get("data", {})
        per_asset = data.get("data", {}).get("per_asset", {})
        if not per_asset:
            continue

        for asset, asset_data in per_asset.items():
            price = asset_data.get("price")
            if price is not None:
                timeline[asset].append((ts, float(price)))

    for asset in timeline:
        timeline[asset].sort(key=lambda x: x[0])

    return dict(timeline)


def find_price_at_offset(timeline: List[Tuple[datetime, float]],
                          target_time: datetime,
                          max_tolerance_hours: float = 4.0) -> Optional[float]:
    if not timeline:
        return None
    best_price = None
    best_delta = timedelta(hours=max_tolerance_hours)
    for ts, price in timeline:
        delta = abs(ts - target_time)
        if delta < best_delta:
            best_delta = delta
            best_price = price
    return best_price


# ---------------------------------------------------------------------------
# Main backtest
# ---------------------------------------------------------------------------
def run_backtest():
    print("=" * 80)
    print("BACKTEST: Re-scoring historical data with CURRENT YAML config")
    print(f"Profile: {PROFILE.get('name', 'unknown')}")
    print(f"Conviction: {'enabled' if CONVICTION_CFG.get('enabled', True) else 'DISABLED'}")
    print(f"Abstain: {'enabled' if ABSTAIN_CFG.get('enabled', False) else 'disabled'}")
    print(f"Reweighting: {'enabled' if REWEIGHT_ENABLED else 'disabled'}")
    print(f"Asymmetric weights: {'ENABLED' if ASYM_ENABLED else 'disabled'}")
    print("=" * 80)

    # Map role names to agent storage names
    agent_names_cfg = PROFILE.get("agent_names", {})
    role_to_agent = {
        "exchange_flow": agent_names_cfg.get("exchange_flow", "exchange_flow_agent"),
        "technical": agent_names_cfg.get("technical", "technical_agent"),
        "derivatives": agent_names_cfg.get("derivatives", "derivatives_agent"),
        "narrative": agent_names_cfg.get("narrative", "narrative_agent"),
        "market": agent_names_cfg.get("market", "market_agent"),
    }

    # Load all agent histories
    agent_histories: Dict[str, List[Dict]] = {}
    for role, agent_name in role_to_agent.items():
        print(f"  Loading {agent_name}...", end=" ", flush=True)
        rows = load_agent_history(agent_name)
        agent_histories[role] = rows
        print(f"{len(rows)} snapshots")

    # Build price timeline from market agent
    print("\n  Building price timeline...", end=" ", flush=True)
    price_timeline = build_price_timeline(agent_histories.get("market", []))
    assets_with_prices = list(price_timeline.keys())
    print(f"{len(assets_with_prices)} assets")

    # Align snapshots
    print("  Aligning agent snapshots...", end=" ", flush=True)
    # Restructure: agent histories keyed by role name for alignment
    aligned = build_aligned_snapshots(agent_histories)
    print(f"{len(aligned)} aligned time points")

    if not aligned:
        print("ERROR: No aligned snapshots.")
        return

    # Date range
    first_ts = aligned[0][0]
    last_ts = aligned[-1][0]
    days_span = (last_ts - first_ts).total_seconds() / 86400
    print(f"\n  Date range: {first_ts.strftime('%Y-%m-%d %H:%M')} → {last_ts.strftime('%Y-%m-%d %H:%M')} ({days_span:.1f} days)")

    # ================================================================
    # Re-score all assets at each time point
    # ================================================================
    print(f"\n  Re-scoring {len(aligned)} time points × {len(PROFILE.get('assets', []))} assets...")
    all_assets = [a.upper() for a in PROFILE.get("assets", [])]
    # Phase A2: Asset blacklist — filter out anti-predictive assets
    blacklist_cfg = PROFILE.get("asset_blacklist", {})
    if blacklist_cfg.get("enabled", False):
        blacklisted = {a.upper() for a in blacklist_cfg.get("assets", [])}
        assets_list = [a for a in all_assets if a not in blacklisted]
        print(f"  Asset blacklist: excluding {blacklisted} → {len(assets_list)} assets remaining")
    else:
        assets_list = all_assets

    all_signals: List[Dict] = []
    prev_dims_by_asset: Dict[str, Dict] = {}  # Track previous dimensions for delta scoring
    prev_oi_by_asset.clear()  # Reset OI state for clean backtest run
    prev_btc_dom_val.clear()  # Reset BTC dominance state for clean backtest run
    for ts, snapshot in aligned:
        # Detect regime once per time point (global, based on BTC)
        _, regime_shifts = detect_regime(snapshot)
        for asset in assets_list:
            result = compute_composite(asset, snapshot, prev_dims_by_asset.get(asset), regime_shifts)
            # Store current dimensions as previous for next iteration
            prev_dims_by_asset[asset] = result.get("dimensions", {})
            all_signals.append({
                "timestamp": ts,
                "asset": asset,
                **result,
            })

    print(f"  Generated {len(all_signals)} re-scored signals")

    # ================================================================
    # PART 1: Signal distribution analysis
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 1: SIGNAL DISTRIBUTION (RE-SCORED)")
    print(f"{'='*80}")

    scores = [s["composite_score"] for s in all_signals]
    directions = [s["direction"] for s in all_signals]

    neutral_count = sum(1 for d in directions if d == "neutral")
    bullish_count = sum(1 for d in directions if d == "bullish")
    bearish_count = sum(1 for d in directions if d == "bearish")
    abstain_count = sum(1 for s in all_signals if s.get("abstain", False))
    total = len(all_signals)

    print(f"\n  Total re-scored signals: {total}")
    print(f"  Neutral:   {neutral_count} ({neutral_count/total*100:.1f}%)")
    print(f"  Bullish:   {bullish_count} ({bullish_count/total*100:.1f}%)")
    print(f"  Bearish:   {bearish_count} ({bearish_count/total*100:.1f}%)")
    if abstain_count:
        print(f"  Abstained: {abstain_count} ({abstain_count/total*100:.1f}%)")

    # Score histogram
    print(f"\n  Score distribution (buckets of 5):")
    for lo in range(20, 85, 5):
        hi = lo + 5
        count = sum(1 for s in scores if lo <= s < hi)
        pct = count / total * 100
        bar = "█" * int(pct)
        if count > 0:
            print(f"    {lo:3d}-{hi:3d}: {count:5d} ({pct:5.1f}%) {bar}")

    avg_score = sum(scores) / len(scores)
    median_score = sorted(scores)[len(scores) // 2]
    min_score = min(scores)
    max_score = max(scores)
    print(f"\n  Mean:   {avg_score:.1f}  |  Median: {median_score:.1f}  |  Min: {min_score:.1f}  |  Max: {max_score:.1f}")

    # Show YAML weights for reference
    if ASYM_ENABLED:
        print(f"\n  Asymmetric weighting: ENABLED")
        print(f"    Default:  " + ", ".join(f"{r}={WEIGHTS_DEFAULT.get(r, 0)}" for r in ALL_ROLES))
        print(f"    Bullish:  " + ", ".join(f"{r}={WEIGHTS_BULLISH.get(r, 0)}" for r in ALL_ROLES))
        print(f"    Bearish:  " + ", ".join(f"{r}={WEIGHTS_BEARISH.get(r, 0)}" for r in ALL_ROLES))
    else:
        print(f"\n  YAML weights: " + ", ".join(f"{r}={WEIGHTS_DEFAULT.get(r, 0)}" for r in ALL_ROLES))

    # ================================================================
    # PART 2: Forward-looking accuracy backtest
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 2: FORWARD-LOOKING ACCURACY (RE-SCORED)")
    print(f"{'='*80}")
    print("For each directional signal, look up actual price N hours later.\n")

    windows = [24, 48]
    window_labels = {24: "24h", 48: "48h"}

    # Deduplicate: one signal per asset per ~12h window
    seen_buckets = set()
    unique_signals = []
    for sig in all_signals:
        bucket_key = (sig["asset"], sig["timestamp"].strftime("%Y-%m-%d") +
                      ("_AM" if sig["timestamp"].hour < 12 else "_PM"))
        if bucket_key not in seen_buckets:
            seen_buckets.add(bucket_key)
            unique_signals.append(sig)

    directional = [s for s in unique_signals if s["direction"] != "neutral"]
    print(f"  Unique signals (deduped to 1/asset/12h): {len(unique_signals)}")
    print(f"  Directional signals (non-neutral):       {len(directional)}")

    all_evals = []

    for wh in windows:
        label = window_labels[wh]
        evals = []

        for sig in directional:
            asset = sig["asset"]
            tl = price_timeline.get(asset, [])
            if not tl:
                continue

            target_time = sig["timestamp"] + timedelta(hours=wh)

            # Phase B3: Temporal gating — prevent look-ahead bias
            # Only score signals whose target evaluation time is in the past.
            # I5 audit found backtest.py lacked this check that retroactive_accuracy.py
            # correctly implements. Without this, future prices could leak into scoring.
            now_ts = datetime.now(timezone.utc).timestamp()
            if target_time.timestamp() > now_ts:
                continue

            future_price = find_price_at_offset(tl, target_time, max_tolerance_hours=6.0)
            if future_price is None:
                continue

            signal_price = find_price_at_offset(tl, sig["timestamp"], max_tolerance_hours=2.0)
            if signal_price is None or signal_price <= 0:
                continue

            pct_change = (future_price - signal_price) / signal_price * 100
            g_score = gradient_score(sig["direction"], pct_change, asset=asset)
            b_correct = binary_correct(sig["direction"], pct_change)

            ev = {
                "asset": asset,
                "window": label,
                "window_hours": wh,
                "direction": sig["direction"],
                "score": sig["composite_score"],
                "label": sig["label"],
                "pct_change": round(pct_change, 2),
                "gradient_score": g_score,
                "binary_correct": b_correct,
                "timestamp": sig["timestamp"],
                "conviction_boost": sig.get("conviction_boost", False),
                "dimensions": sig.get("dimensions", {}),
                "data_tiers": sig.get("data_tiers", {}),
            }
            evals.append(ev)
            all_evals.append(ev)

        if not evals:
            print(f"\n  {label}: insufficient data")
            continue

        avg_g = sum(e["gradient_score"] for e in evals) / len(evals)
        b_acc = sum(1 for e in evals if e["binary_correct"]) / len(evals)
        avg_move = sum(abs(e["pct_change"]) for e in evals) / len(evals)

        bullish_evals = [e for e in evals if e["direction"] == "bullish"]
        bearish_evals = [e for e in evals if e["direction"] == "bearish"]

        print(f"\n  ┌─── {label} WINDOW (n={len(evals)}) ───┐")
        print(f"  │  Gradient accuracy: {avg_g*100:5.1f}%")
        print(f"  │  Binary accuracy:   {b_acc*100:5.1f}%")
        print(f"  │  Avg |price move|:  {avg_move:5.2f}%")
        if bullish_evals:
            bg = sum(e["gradient_score"] for e in bullish_evals) / len(bullish_evals)
            print(f"  │  Bullish signals:   n={len(bullish_evals):3d}  gradient={bg*100:.1f}%")
        if bearish_evals:
            sg = sum(e["gradient_score"] for e in bearish_evals) / len(bearish_evals)
            print(f"  │  Bearish signals:   n={len(bearish_evals):3d}  gradient={sg*100:.1f}%")
        print(f"  └{'─'*35}┘")

    if not all_evals:
        print("\nERROR: No evaluations possible.")
        return

    # ================================================================
    # PART 3: Accuracy by asset
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 3: ACCURACY BY ASSET")
    print(f"{'='*80}")

    asset_evals = defaultdict(list)
    for ev in all_evals:
        asset_evals[ev["asset"]].append(ev)

    asset_accuracy = {}
    for asset, evals in sorted(asset_evals.items()):
        if len(evals) >= 3:
            avg_g = sum(e["gradient_score"] for e in evals) / len(evals)
            avg_p = sum(abs(e["pct_change"]) for e in evals) / len(evals)
            asset_accuracy[asset] = (avg_g, avg_p, len(evals))

    sorted_by_accuracy = sorted(asset_accuracy.items(), key=lambda x: x[1][0], reverse=True)
    print(f"\n  {'Asset':>6s}  {'Gradient':>8s}  {'Avg Move':>8s}  {'Signals':>7s}")
    print(f"  {'─'*6}  {'─'*8}  {'─'*8}  {'─'*7}")
    for asset, (acc, avg_p, n) in sorted_by_accuracy:
        marker = "🟢" if acc >= 0.55 else "🟡" if acc >= 0.40 else "🔴"
        print(f"  {marker}{asset:>5s}  {acc*100:7.1f}%  {avg_p:7.2f}%  {n:7d}")

    # ================================================================
    # PART 4: Conviction analysis
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 4: CONVICTION ANALYSIS")
    print(f"{'='*80}")

    confidence_buckets = {
        "high (|Δ|>15)": [], "medium (|Δ| 10-15)": [],
        "low (|Δ| 5-10)": [], "very low (|Δ| 0-5)": [],
    }

    for ev in all_evals:
        dist = abs(ev["score"] - 50)
        if dist > 15:
            confidence_buckets["high (|Δ|>15)"].append(ev)
        elif dist > 10:
            confidence_buckets["medium (|Δ| 10-15)"].append(ev)
        elif dist > 5:
            confidence_buckets["low (|Δ| 5-10)"].append(ev)
        else:
            confidence_buckets["very low (|Δ| 0-5)"].append(ev)

    for label, entries in confidence_buckets.items():
        if not entries:
            print(f"  {label:>25s}: no data")
            continue
        avg_g = sum(e["gradient_score"] for e in entries) / len(entries)
        avg_move = sum(abs(e["pct_change"]) for e in entries) / len(entries)
        print(f"  {label:>25s}: gradient={avg_g*100:5.1f}%  avg_move={avg_move:5.2f}%  n={len(entries)}")

    boosted = [e for e in all_evals if e.get("conviction_boost")]
    unboosted = [e for e in all_evals if not e.get("conviction_boost")]
    if boosted and unboosted:
        g_boost = sum(e["gradient_score"] for e in boosted) / len(boosted)
        g_noboost = sum(e["gradient_score"] for e in unboosted) / len(unboosted)
        print(f"\n  Conviction boosted:    gradient={g_boost*100:.1f}%  n={len(boosted)}")
        print(f"  Not boosted:           gradient={g_noboost*100:.1f}%  n={len(unboosted)}")

    # ================================================================
    # PART 5: Gradient score distribution
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 5: GRADIENT SCORE DISTRIBUTION")
    print(f"{'='*80}")

    buckets = {
        "1.0 (strong correct)": 0, "0.7 (correct)": 0,
        "0.4 (weak correct)": 0, "0.2 (weak wrong)": 0, "0.0 (wrong)": 0,
    }
    for ev in all_evals:
        gs = ev["gradient_score"]
        if gs >= 0.95:
            buckets["1.0 (strong correct)"] += 1
        elif gs >= 0.65:
            buckets["0.7 (correct)"] += 1
        elif gs >= 0.35:
            buckets["0.4 (weak correct)"] += 1
        elif gs >= 0.15:
            buckets["0.2 (weak wrong)"] += 1
        else:
            buckets["0.0 (wrong)"] += 1

    total_evals = len(all_evals)
    for label, count in buckets.items():
        pct = count / total_evals * 100
        bar = "█" * int(pct / 2)
        print(f"  {label:>25s}: {count:4d} ({pct:5.1f}%) {bar}")

    # ================================================================
    # PART 6: Per-dimension signal quality
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 6: PER-DIMENSION SIGNAL QUALITY")
    print(f"{'='*80}")
    print("When a specific dimension scores bullish/bearish, how accurate is the composite?\n")

    evals_with_dims = [e for e in all_evals if e.get("dimensions")]
    if evals_with_dims:
        for dim_name in ALL_ROLES:
            dim_bullish = []
            dim_bearish = []

            for ev in evals_with_dims:
                dim = ev["dimensions"].get(dim_name, {})
                dim_score = dim.get("score", 50)
                if dim_score is None:
                    continue
                if dim_score >= 55:
                    dim_bullish.append(ev)
                elif dim_score < 45:
                    dim_bearish.append(ev)

            parts = []
            if dim_bullish:
                avg = sum(e["gradient_score"] for e in dim_bullish) / len(dim_bullish)
                parts.append(f"bullish={avg*100:.0f}% (n={len(dim_bullish)})")
            if dim_bearish:
                avg = sum(e["gradient_score"] for e in dim_bearish) / len(dim_bearish)
                parts.append(f"bearish={avg*100:.0f}% (n={len(dim_bearish)})")

            if parts:
                print(f"  {dim_name:>12s}: {', '.join(parts)}")
            else:
                print(f"  {dim_name:>12s}: insufficient data")

    # ================================================================
    # PART 7: SPEARMAN IC (Information Coefficient) ANALYSIS
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 7: SPEARMAN IC (INFORMATION COEFFICIENT)")
    print(f"{'='*80}")
    print("Rank correlation between dimension/composite scores and future returns.\n")
    print("IC > +0.05 = STRONG predictor | IC > 0 = OK | IC < 0 = ANTI-PREDICTIVE\n")

    # Use only 24h-window evals for IC (most reliable forward-looking metric)
    ic_evals = [e for e in all_evals if e.get("window_hours") == 24 and e.get("dimensions")]

    if len(ic_evals) >= 10:
        try:
            from scipy.stats import spearmanr  # type: ignore
        except ImportError:
            # Pure Python Spearman fallback
            def _rank(data):
                indexed = sorted(enumerate(data), key=lambda x: x[1])
                ranks = [0.0] * len(data)
                i = 0
                while i < len(indexed):
                    j = i
                    while j < len(indexed) - 1 and indexed[j + 1][1] == indexed[j][1]:
                        j += 1
                    avg_rank = (i + j) / 2.0 + 1
                    for k in range(i, j + 1):
                        ranks[indexed[k][0]] = avg_rank
                    i = j + 1
                return ranks

            def spearmanr(x, y):
                n = len(x)
                rx, ry = _rank(x), _rank(y)
                mx, my = sum(rx) / n, sum(ry) / n
                num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
                dx = sum((a - mx) ** 2 for a in rx) ** 0.5
                dy = sum((b - my) ** 2 for b in ry) ** 0.5
                rho = num / (dx * dy) if dx * dy > 0 else 0.0
                return rho, 0.0  # pvalue placeholder

        # Group by timestamp to create cross-asset slices
        # IC should be computed cross-sectionally: at each time point,
        # rank assets by score, rank by return, correlate
        time_buckets: Dict[str, List[Dict]] = defaultdict(list)
        for ev in ic_evals:
            # Bucket to ~12h granularity
            t = ev["timestamp"]
            bucket_key = t.strftime("%Y-%m-%d") + ("_AM" if t.hour < 12 else "_PM")
            time_buckets[bucket_key].append(ev)

        # Need at least 3 assets per slice for meaningful correlation
        valid_slices = {k: v for k, v in time_buckets.items() if len(v) >= 3}

        if valid_slices:
            # Per-dimension IC: average Spearman rho across slices
            dim_ics: Dict[str, List[float]] = defaultdict(list)
            composite_ics: List[float] = []

            for bucket_key, slice_evals in sorted(valid_slices.items()):
                returns = [e["pct_change"] for e in slice_evals]

                # Composite IC
                comp_scores = [e["score"] for e in slice_evals]
                if len(set(comp_scores)) > 1 and len(set(returns)) > 1:
                    rho, _ = spearmanr(comp_scores, returns)
                    if not (rho != rho):  # check for NaN
                        composite_ics.append(rho)

                # Per-dimension IC
                for dim_name in ALL_ROLES:
                    dim_scores = []
                    dim_returns = []
                    for ev in slice_evals:
                        dim = ev["dimensions"].get(dim_name, {})
                        ds = dim.get("score")
                        if ds is not None:
                            dim_scores.append(ds)
                            dim_returns.append(ev["pct_change"])

                    if len(dim_scores) >= 3 and len(set(dim_scores)) > 1 and len(set(dim_returns)) > 1:
                        rho, _ = spearmanr(dim_scores, dim_returns)
                        if not (rho != rho):  # NaN check
                            dim_ics[dim_name].append(rho)

            # Print results
            print(f"  Cross-asset slices: {len(valid_slices)} (need ≥3 assets each)")
            print(f"\n  {'Dimension':>12s}  {'IC (avg)':>8s}  {'IC (med)':>8s}  {'Slices':>6s}  {'Status':>12s}")
            print(f"  {'─'*12}  {'─'*8}  {'─'*8}  {'─'*6}  {'─'*12}")

            for dim_name in ALL_ROLES:
                ics = dim_ics.get(dim_name, [])
                if ics:
                    avg_ic = sum(ics) / len(ics)
                    sorted_ics = sorted(ics)
                    med_ic = sorted_ics[len(sorted_ics) // 2]
                    if avg_ic > 0.05:
                        status = "🟢 STRONG"
                    elif avg_ic > 0:
                        status = "🟡 OK"
                    else:
                        status = "🔴 ANTI-PRED"
                    print(f"  {dim_name:>12s}  {avg_ic:>+8.4f}  {med_ic:>+8.4f}  {len(ics):>6d}  {status}")
                else:
                    print(f"  {dim_name:>12s}  {'N/A':>8s}  {'N/A':>8s}  {'0':>6s}  ⚪ NO DATA")

            # Composite IC
            if composite_ics:
                avg_comp = sum(composite_ics) / len(composite_ics)
                sorted_comp = sorted(composite_ics)
                med_comp = sorted_comp[len(sorted_comp) // 2]
                if avg_comp > 0.05:
                    status = "🟢 STRONG"
                elif avg_comp > 0:
                    status = "🟡 OK"
                else:
                    status = "🔴 ANTI-PRED"
                print(f"\n  {'COMPOSITE':>12s}  {avg_comp:>+8.4f}  {med_comp:>+8.4f}  {len(composite_ics):>6d}  {status}")

                # Also compute ICIR (IC / std(IC)) for composite
                if len(composite_ics) > 1:
                    ic_std = (sum((x - avg_comp)**2 for x in composite_ics) / (len(composite_ics) - 1)) ** 0.5
                    icir = avg_comp / ic_std if ic_std > 0 else 0
                    print(f"  {'ICIR':>12s}  {icir:>+8.4f}  (IC/std — >0.5 is good)")
            else:
                print(f"\n  COMPOSITE: insufficient data for IC computation")
        else:
            print("  Insufficient cross-asset slices (need ≥3 assets per time bucket)")
    else:
        print(f"  Only {len(ic_evals)} 24h evals with dimensions (need ≥10 for IC)")
        print("  Cannot compute Information Coefficient without more data.")

    # ================================================================
    # SUMMARY
    # ================================================================
    print(f"\n{'='*80}")
    print("📊 SUMMARY")
    print(f"{'='*80}")

    if all_evals:
        overall_g = sum(e["gradient_score"] for e in all_evals) / len(all_evals)
        overall_b = sum(1 for e in all_evals if e["binary_correct"]) / len(all_evals)
        avg_move = sum(abs(e["pct_change"]) for e in all_evals) / len(all_evals)

        print(f"""
  Overall gradient accuracy: {overall_g*100:.1f}%
  Overall binary accuracy:   {overall_b*100:.1f}%
  Average absolute move:     {avg_move:.2f}%
  Total evaluations:         {len(all_evals)}
  Re-scored signals:         {len(all_signals)}
  Days of data:              {days_span:.1f}

  Config: conviction={'enabled' if CONVICTION_CFG.get('enabled', True) else 'DISABLED'}
          abstain={'enabled' if ABSTAIN_CFG.get('enabled', False) else 'disabled'}
          asymmetric_weights={'ENABLED' if ASYM_ENABLED else 'disabled'}
          weights_default={dict(WEIGHTS_DEFAULT)}

  Baseline (old YAML): 25.6%
  Previous best:       52.5%
  Target:              >60%

  Scale:
    >60% = Good (beating random by 2x)
    50%  = Mediocre (coin flip)
    40%  = Below random (~30%)
    <30% = Harmful (contrarian indicator)
""")


if __name__ == "__main__":
    if "--api-url" in sys.argv:
        idx = sys.argv.index("--api-url")
        if idx + 1 < len(sys.argv):
            API_BASE = sys.argv[idx + 1]
    run_backtest()
