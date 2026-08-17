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
    detected_regime: str = "unknown",
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

    # Conviction multiplier — DISABLED (backtest v2: hurts accuracy by 12pp)
    conviction_applied = False
    if CONVICTION_CFG.get("enabled", False):  # was True, disabled based on backtest v2
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

        # Regime-based abstain modifier — widen in ranging, tighten in trending
        regime_mod_cfg = ABSTAIN_CFG.get("regime_modifier", {})
        if regime_mod_cfg.get("enabled", False):
            regime_mult = float(regime_mod_cfg.get(detected_regime, 1.0))
            resolved_distance = resolved_distance * regime_mult

        # Phase A1: Asymmetric abstain zones
        asym_abstain = ABSTAIN_CFG.get("asymmetric", {})
        if asym_abstain.get("enabled", False):
            regime_mult_asym = float(regime_mod_cfg.get(detected_regime, 1.0)) if regime_mod_cfg.get("enabled", False) else 1.0
            bearish_dist = float(asym_abstain.get("bearish_min_distance", resolved_distance)) * regime_mult_asym
            bullish_dist = float(asym_abstain.get("bullish_min_distance", resolved_distance)) * regime_mult_asym
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
        "whale": agent_names_cfg.get("whale", "whale_agent"),
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

    # ================================================================
    # TRAIN/TEST TEMPORAL SPLIT
    # ================================================================
    split_ratio = 0.60
    split_idx = int(len(aligned) * split_ratio)
    split_timestamp = aligned[split_idx][0] if split_idx < len(aligned) else aligned[-1][0]
    print(f"\n  Train/Test split: {split_ratio:.0%}/{1-split_ratio:.0%}")
    print(f"    Train: {aligned[0][0].strftime('%Y-%m-%d %H:%M')} → {split_timestamp.strftime('%Y-%m-%d %H:%M')} ({split_idx} points)")
    print(f"    Test:  {split_timestamp.strftime('%Y-%m-%d %H:%M')} → {aligned[-1][0].strftime('%Y-%m-%d %H:%M')} ({len(aligned) - split_idx} points)")

    from signal_fusion.target_calculator import ATR_SL_MULTIPLIERS

    all_signals: List[Dict] = []
    prev_dims_by_asset: Dict[str, Dict] = {}  # Track previous dimensions for delta scoring
    prev_oi_by_asset.clear()  # Reset OI state for clean backtest run
    prev_btc_dom_val.clear()  # Reset BTC dominance state for clean backtest run
    # Round 2 experiment (RANK_DIRECTION=1): direction from cross-sectional RANK
    # instead of absolute score. Rationale: 176d backtest showed composite IC
    # +0.11 (real ranking alpha) but 99.6% bullish absolute calls at 31%
    # accuracy — the level is broken, the ordering is not. Top quartile =
    # bullish, bottom quartile = bearish, middle = neutral.
    _rank_direction = os.getenv("RANK_DIRECTION", "0") == "1"

    for idx, (ts, snapshot) in enumerate(aligned):
        # Detect regime once per time point (global, based on BTC)
        detected_regime, regime_shifts = detect_regime(snapshot)

        # Pass 1: compute composites for every asset at this time point
        results_this_ts = []
        for asset in assets_list:
            result = compute_composite(asset, snapshot, prev_dims_by_asset.get(asset), regime_shifts, detected_regime)
            # Store current dimensions as previous for next iteration
            prev_dims_by_asset[asset] = result.get("dimensions", {})
            results_this_ts.append((asset, result))

        # Optional rank-based direction overlay
        if _rank_direction and len(results_this_ts) >= 8:
            ranked = sorted(results_this_ts, key=lambda ar: ar[1]["composite_score"], reverse=True)
            n_side = max(1, len(ranked) // 4)
            for i, (_a, r) in enumerate(ranked):
                if i < n_side:
                    r["direction"], r["label"], r["abstain"] = "bullish", "RANK BUY", False
                elif i >= len(ranked) - n_side:
                    r["direction"], r["label"], r["abstain"] = "bearish", "RANK SELL", False
                else:
                    r["direction"], r["label"], r["abstain"] = "neutral", "RANK NEUTRAL", True

        # Pass 2: targets + bookkeeping per asset
        for asset, result in results_this_ts:
            # Calculate target/SL for directional signals
            target_data = {}
            if result["direction"] in ("bullish", "bearish") and not result.get("abstain"):
                market_snap = snapshot.get("market")
                tech_snap = snapshot.get("technical")
                entry_price = None
                atr_14 = None

                if market_snap:
                    mkt_data = market_snap.get("data", {})
                    pa = mkt_data.get("per_asset", {}).get(asset, {})
                    entry_price = pa.get("price")
                if entry_price is None and tech_snap:
                    tech_data = tech_snap.get("data", {})
                    entry_price = tech_data.get("by_asset", {}).get(asset, {}).get("price")
                if tech_snap:
                    tech_data = tech_snap.get("data", {})
                    atr_14 = tech_data.get("by_asset", {}).get(asset, {}).get("atr_14")

                if entry_price and float(entry_price) > 0:
                    entry_price = float(entry_price)
                    if atr_14 is None or float(atr_14) <= 0:
                        atr_14 = entry_price * 0.03  # fallback
                    else:
                        atr_14 = float(atr_14)

                    sl_mult = ATR_SL_MULTIPLIERS.get(asset, 2.0)
                    direction_map = "buy" if result["direction"] == "bullish" else "sell"

                    if direction_map == "buy":
                        stop_loss = entry_price - (atr_14 * sl_mult)
                        distance = result["composite_score"] - 50.0
                        atr_pct = (atr_14 / entry_price) * 100
                        move_fraction = distance / 10.0  # was 35.0
                        predicted_pct = move_fraction * atr_pct * 1.5  # was 0.5
                        min_pred = atr_pct * 0.3
                        if predicted_pct < min_pred:
                            predicted_pct = min_pred
                        predicted_pct = max(0.1, min(atr_pct * 2.0, predicted_pct))
                        target_price = entry_price * (1 + predicted_pct / 100)
                        risk = entry_price - stop_loss
                        if target_price - entry_price < risk * 0.5:  # was 1.5
                            target_price = entry_price + risk * 0.5
                    else:
                        stop_loss = entry_price + (atr_14 * sl_mult)
                        distance = 50.0 - result["composite_score"]
                        atr_pct = (atr_14 / entry_price) * 100
                        move_fraction = distance / 10.0  # was 35.0
                        predicted_pct = move_fraction * atr_pct * 1.5  # was 0.5
                        min_pred = atr_pct * 0.3
                        if predicted_pct < min_pred:
                            predicted_pct = min_pred
                        predicted_pct = max(0.1, min(atr_pct * 2.0, predicted_pct))
                        target_price = entry_price * (1 - predicted_pct / 100)
                        risk = stop_loss - entry_price
                        if entry_price - target_price < risk * 0.5:  # was 1.5
                            target_price = entry_price - risk * 0.5

                    target_data = {
                        "entry_price": round(entry_price, 2),
                        "target_price": round(target_price, 2),
                        "stop_loss": round(stop_loss, 2),
                        "predicted_move_pct": round(predicted_pct, 2),
                    }

            # Get F&G value for regime analysis
            fg_for_tag = None
            market_snap_data = snapshot.get("market")
            if market_snap_data:
                fg_for_tag = market_snap_data.get("data", {}).get("sentiment", {}).get("fear_greed_index")

            all_signals.append({
                "timestamp": ts,
                "asset": asset,
                "split": "train" if idx < split_idx else "test",
                "regime": detected_regime,
                "fg_value": float(fg_for_tag) if fg_for_tag is not None else None,
                **result,
                **target_data,
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
    # Test-only subsets for accuracy evaluation
    test_unique = [s for s in unique_signals if s["split"] == "test"]
    test_directional = [s for s in test_unique if s["direction"] != "neutral"]
    print(f"  Unique signals (deduped to 1/asset/12h): {len(unique_signals)}")
    print(f"  Directional signals (non-neutral):       {len(directional)}")
    print(f"  Test-only unique: {len(test_unique)}, test directional: {len(test_directional)}")

    all_evals = []
    now_ts = datetime.now(timezone.utc).timestamp()

    for wh in windows:
        label = window_labels[wh]
        evals = []

        for sig in test_directional:
            asset = sig["asset"]
            tl = price_timeline.get(asset, [])
            if not tl:
                continue

            target_time = sig["timestamp"] + timedelta(hours=wh)

            # Phase B3: Temporal gating — prevent look-ahead bias
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
                "regime": sig.get("regime", "unknown"),
                "fg_value": sig.get("fg_value"),
                "split": sig.get("split", "test"),
                "predicted_move_pct": sig.get("predicted_move_pct", 0),
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
    # PART 8: SIGNAL FLOW TIMELINE (per 12h bucket)
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 8: SIGNAL FLOW TIMELINE (per 12h bucket)")
    print(f"{'='*80}")
    print("Shows buy/sell/neutral signal counts per 12-hour window.\n")

    timeline_buckets: Dict[str, Dict[str, Any]] = {}
    for sig in all_signals:
        t = sig["timestamp"]
        bucket = t.strftime("%Y-%m-%d") + (" AM" if t.hour < 12 else " PM")
        if bucket not in timeline_buckets:
            timeline_buckets[bucket] = {
                "buy": 0, "sell": 0, "neutral": 0,
                "buy_assets": [], "sell_assets": [],
                "scores": [], "split": sig["split"],
            }
        b = timeline_buckets[bucket]
        d = sig["direction"]
        if d == "bullish":
            b["buy"] += 1
            if sig["asset"] not in b["buy_assets"]:
                b["buy_assets"].append(sig["asset"])
        elif d == "bearish":
            b["sell"] += 1
            if sig["asset"] not in b["sell_assets"]:
                b["sell_assets"].append(sig["asset"])
        else:
            b["neutral"] += 1
        b["scores"].append(sig["composite_score"])

    print(f"  {'Bucket':>16s}  {'Split':>5s}  {'Buy':>4s}  {'Sell':>4s}  {'Neut':>4s}  {'AvgSc':>5s}  Buy Assets")
    print(f"  {'─'*16}  {'─'*5}  {'─'*4}  {'─'*4}  {'─'*4}  {'─'*5}  {'─'*30}")
    for bucket in sorted(timeline_buckets.keys()):
        b = timeline_buckets[bucket]
        avg_sc = sum(b["scores"]) / len(b["scores"]) if b["scores"] else 0
        split_tag = "TRAIN" if b["split"] == "train" else "TEST"
        buy_list = ", ".join(b["buy_assets"][:6])
        if len(b["buy_assets"]) > 6:
            buy_list += f" +{len(b['buy_assets'])-6}"
        sell_note = f" | Sell: {', '.join(b['sell_assets'])}" if b["sell_assets"] else ""
        print(f"  {bucket:>16s}  {split_tag:>5s}  {b['buy']:4d}  {b['sell']:4d}  {b['neutral']:4d}  {avg_sc:5.1f}  {buy_list}{sell_note}")

    total_buy = sum(b["buy"] for b in timeline_buckets.values())
    total_sell = sum(b["sell"] for b in timeline_buckets.values())
    total_neutral = sum(b["neutral"] for b in timeline_buckets.values())
    total_all = total_buy + total_sell + total_neutral
    print(f"\n  Totals: {total_buy} buy ({total_buy/total_all*100:.0f}%), "
          f"{total_sell} sell ({total_sell/total_all*100:.0f}%), "
          f"{total_neutral} neutral ({total_neutral/total_all*100:.0f}%)")

    # ================================================================
    # PART 9: TARGET PRICE EVALUATION (test set only)
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 9: TARGET PRICE EVALUATION (test set only)")
    print(f"{'='*80}")
    print("For each directional signal, replay 48h price path: TP hit / SL hit / expired.\n")

    target_signals = [s for s in all_signals
                      if s["split"] == "test"
                      and s.get("entry_price") is not None
                      and s["direction"] != "neutral"]

    # Deduplicate to 1 per asset per 12h
    seen_target: set = set()
    unique_target: List[Dict] = []
    for sig in target_signals:
        bk = (sig["asset"], sig["timestamp"].strftime("%Y-%m-%d") +
              ("_AM" if sig["timestamp"].hour < 12 else "_PM"))
        if bk not in seen_target:
            seen_target.add(bk)
            unique_target.append(sig)

    outcomes: List[Dict] = []

    for sig in unique_target:
        asset = sig["asset"]
        tl = price_timeline.get(asset, [])
        if not tl:
            continue

        entry = sig["entry_price"]
        tp = sig["target_price"]
        sl = sig["stop_loss"]
        direction = sig["direction"]
        sig_time = sig["timestamp"]
        end_time = sig_time + timedelta(hours=48)

        # Walk price path
        outcome = "EXPIRED"
        final_price = entry
        time_to_outcome = 48.0

        for pt_time, pt_price in tl:
            if pt_time <= sig_time:
                continue
            if pt_time > end_time:
                break

            final_price = pt_price

            if direction == "bullish":
                if pt_price >= tp:
                    outcome = "TP_HIT"
                    time_to_outcome = (pt_time - sig_time).total_seconds() / 3600
                    final_price = tp
                    break
                elif pt_price <= sl:
                    outcome = "SL_HIT"
                    time_to_outcome = (pt_time - sig_time).total_seconds() / 3600
                    final_price = sl
                    break
            else:  # bearish
                if pt_price <= tp:
                    outcome = "TP_HIT"
                    time_to_outcome = (pt_time - sig_time).total_seconds() / 3600
                    final_price = tp
                    break
                elif pt_price >= sl:
                    outcome = "SL_HIT"
                    time_to_outcome = (pt_time - sig_time).total_seconds() / 3600
                    final_price = sl
                    break

        if direction == "bullish":
            pnl_pct = (final_price - entry) / entry * 100
        else:
            pnl_pct = (entry - final_price) / entry * 100

        outcomes.append({
            "asset": asset,
            "direction": direction,
            "outcome": outcome,
            "pnl_pct": round(pnl_pct, 2),
            "time_to_outcome_hours": round(time_to_outcome, 1),
            "entry": entry,
            "target": tp,
            "stop_loss": sl,
            "predicted_move_pct": sig.get("predicted_move_pct", 0),
            "composite_score": sig["composite_score"],
            "timestamp": sig_time,
        })

    if outcomes:
        tp_hits = [o for o in outcomes if o["outcome"] == "TP_HIT"]
        sl_hits = [o for o in outcomes if o["outcome"] == "SL_HIT"]
        expired = [o for o in outcomes if o["outcome"] == "EXPIRED"]

        win_rate = len(tp_hits) / len(outcomes) * 100
        loss_rate = len(sl_hits) / len(outcomes) * 100
        expire_rate = len(expired) / len(outcomes) * 100

        avg_win = sum(o["pnl_pct"] for o in tp_hits) / len(tp_hits) if tp_hits else 0
        avg_loss = sum(o["pnl_pct"] for o in sl_hits) / len(sl_hits) if sl_hits else 0
        avg_expired_pnl = sum(o["pnl_pct"] for o in expired) / len(expired) if expired else 0

        ev_per_trade = (len(tp_hits) * avg_win + len(sl_hits) * avg_loss +
                        len(expired) * avg_expired_pnl) / len(outcomes)

        avg_time_tp = sum(o["time_to_outcome_hours"] for o in tp_hits) / len(tp_hits) if tp_hits else 0
        avg_time_sl = sum(o["time_to_outcome_hours"] for o in sl_hits) / len(sl_hits) if sl_hits else 0

        print(f"  Total target evaluations: {len(outcomes)}")
        print(f"  TP Hit:   {len(tp_hits):3d} ({win_rate:5.1f}%)  avg P&L: {avg_win:+.2f}%  avg time: {avg_time_tp:.1f}h")
        print(f"  SL Hit:   {len(sl_hits):3d} ({loss_rate:5.1f}%)  avg P&L: {avg_loss:+.2f}%  avg time: {avg_time_sl:.1f}h")
        print(f"  Expired:  {len(expired):3d} ({expire_rate:5.1f}%)  avg P&L: {avg_expired_pnl:+.2f}%")
        print(f"\n  Expected Value per trade: {ev_per_trade:+.3f}%")
        print(f"  Effective win rate (TP + profitable expires): "
              f"{(len(tp_hits) + sum(1 for o in expired if o['pnl_pct'] > 0)) / len(outcomes) * 100:.1f}%")

        # Per-asset breakdown
        print(f"\n  Per-asset target accuracy:")
        print(f"  {'Asset':>6s}  {'Trades':>6s}  {'WinR':>5s}  {'AvgW':>6s}  {'AvgL':>6s}  {'EV':>7s}")
        print(f"  {'─'*6}  {'─'*6}  {'─'*5}  {'─'*6}  {'─'*6}  {'─'*7}")
        asset_outcomes: Dict[str, List[Dict]] = defaultdict(list)
        for o in outcomes:
            asset_outcomes[o["asset"]].append(o)
        for asset in sorted(asset_outcomes.keys()):
            ao = asset_outcomes[asset]
            a_tp = [o for o in ao if o["outcome"] == "TP_HIT"]
            a_sl = [o for o in ao if o["outcome"] == "SL_HIT"]
            a_wr = len(a_tp) / len(ao) * 100 if ao else 0
            a_avgw = sum(o["pnl_pct"] for o in a_tp) / len(a_tp) if a_tp else 0
            a_avgl = sum(o["pnl_pct"] for o in a_sl) / len(a_sl) if a_sl else 0
            a_ev = sum(o["pnl_pct"] for o in ao) / len(ao) if ao else 0
            marker = "🟢" if a_ev > 0 else "🔴"
            print(f"  {marker}{asset:>5s}  {len(ao):6d}  {a_wr:4.0f}%  {a_avgw:+5.2f}  {a_avgl:+5.2f}  {a_ev:+6.3f}%")

        # SL tightness analysis
        print(f"\n  Stop-loss tightness (SL hit rate by asset — high = SL too tight):")
        for asset in sorted(asset_outcomes.keys()):
            ao = asset_outcomes[asset]
            sl_rate = sum(1 for o in ao if o["outcome"] == "SL_HIT") / len(ao) * 100
            marker = "⚠️ " if sl_rate > 50 else "  "
            print(f"  {marker}{asset:>5s}: SL hit {sl_rate:.0f}% of {len(ao)} trades")
    else:
        print("  No target evaluations possible (no directional signals with price data)")

    # ================================================================
    # PART 10: PREDICTION QUALITY (test set only)
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 10: PREDICTION QUALITY (test set only)")
    print(f"{'='*80}")
    print("How accurate are predicted move percentages vs actual outcomes?\n")

    pred_evals: List[Dict] = []
    for sig in test_directional:
        asset = sig["asset"]
        tl = price_timeline.get(asset, [])
        if not tl:
            continue
        target_time = sig["timestamp"] + timedelta(hours=48)
        if target_time.timestamp() > now_ts:
            continue
        future_price = find_price_at_offset(tl, target_time, max_tolerance_hours=6.0)
        signal_price = find_price_at_offset(tl, sig["timestamp"], max_tolerance_hours=2.0)
        if future_price is None or signal_price is None or signal_price <= 0:
            continue
        actual_pct = (future_price - signal_price) / signal_price * 100
        predicted = sig.get("predicted_move_pct", 0)
        if sig["direction"] == "bearish":
            actual_for_comparison = -actual_pct
        else:
            actual_for_comparison = actual_pct

        pred_evals.append({
            "asset": asset,
            "direction": sig["direction"],
            "predicted_pct": predicted,
            "actual_pct": round(actual_pct, 2),
            "actual_directional": round(actual_for_comparison, 2),
            "error": round(abs(predicted - actual_for_comparison), 2),
            "direction_correct": actual_for_comparison > 0,
        })

    if pred_evals:
        mae = sum(e["error"] for e in pred_evals) / len(pred_evals)
        dir_correct = sum(1 for e in pred_evals if e["direction_correct"])
        dir_acc = dir_correct / len(pred_evals) * 100

        print(f"  Prediction evaluations: {len(pred_evals)}")
        print(f"  Mean Absolute Error:    {mae:.2f}%")
        print(f"  Directional accuracy:   {dir_acc:.1f}% ({dir_correct}/{len(pred_evals)})")

        # Calibration buckets
        cal_buckets: Dict[str, List[Dict]] = {"0-2%": [], "2-5%": [], "5%+": []}
        for e in pred_evals:
            p = abs(e["predicted_pct"])
            if p < 2:
                cal_buckets["0-2%"].append(e)
            elif p < 5:
                cal_buckets["2-5%"].append(e)
            else:
                cal_buckets["5%+"].append(e)

        print(f"\n  Calibration (predicted bucket vs actual):")
        print(f"  {'Predicted':>10s}  {'n':>4s}  {'Avg Pred':>8s}  {'Avg Actual':>10s}  {'MAE':>6s}  {'DirAcc':>6s}")
        print(f"  {'─'*10}  {'─'*4}  {'─'*8}  {'─'*10}  {'─'*6}  {'─'*6}")
        for cal_label, entries in cal_buckets.items():
            if entries:
                ap = sum(abs(e["predicted_pct"]) for e in entries) / len(entries)
                aa = sum(abs(e["actual_directional"]) for e in entries) / len(entries)
                am = sum(e["error"] for e in entries) / len(entries)
                da = sum(1 for e in entries if e["direction_correct"]) / len(entries) * 100
                print(f"  {cal_label:>10s}  {len(entries):4d}  {ap:7.2f}%  {aa:9.2f}%  {am:5.2f}%  {da:5.1f}%")
            else:
                print(f"  {cal_label:>10s}  {0:4d}  {'—':>8s}  {'—':>10s}  {'—':>6s}  {'—':>6s}")
    else:
        print("  No prediction evaluations possible")

    # ================================================================
    # PART 11: INPUT DATA QUALITY AUDIT
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 11: INPUT DATA QUALITY AUDIT")
    print(f"{'='*80}")
    print("Per-dimension score statistics and data completeness.\n")

    dim_stats: Dict[str, Dict[str, Any]] = {}
    for dim_name in ALL_ROLES:
        scores_list: List[float] = []
        no_data_count = 0
        full_count_d = 0
        partial_count_d = 0

        for sig in all_signals:
            dim = sig.get("dimensions", {}).get(dim_name, {})
            ds = dim.get("score")
            tier = dim.get("data_tier", "unknown")
            if ds is not None:
                scores_list.append(ds)
            if tier == "none":
                no_data_count += 1
            elif tier == "full":
                full_count_d += 1
            elif tier == "partial":
                partial_count_d += 1

        if scores_list:
            mean_s = sum(scores_list) / len(scores_list)
            sorted_s = sorted(scores_list)
            median_s = sorted_s[len(sorted_s) // 2]
            min_s = min(scores_list)
            max_s = max(scores_list)
            std_s = (sum((x - mean_s) ** 2 for x in scores_list) / len(scores_list)) ** 0.5
            cluster_pct = sum(1 for s in scores_list if 45 <= s <= 55) / len(scores_list) * 100
            spread = max_s - min_s

            dim_stats[dim_name] = {
                "mean": mean_s, "median": median_s, "min": min_s, "max": max_s,
                "std": std_s, "spread": spread, "cluster_pct": cluster_pct,
                "full": full_count_d, "partial": partial_count_d, "none": no_data_count,
                "total": len(scores_list),
            }

    print(f"  {'Dimension':>12s}  {'Mean':>5s}  {'Std':>5s}  {'Min':>5s}  {'Max':>5s}  {'Spread':>6s}  {'45-55%':>6s}  {'Full%':>5s}  {'None%':>5s}")
    print(f"  {'─'*12}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*6}  {'─'*6}  {'─'*5}  {'─'*5}")
    for dim_name in ALL_ROLES:
        ds_stat = dim_stats.get(dim_name)
        if ds_stat:
            full_pct = ds_stat["full"] / ds_stat["total"] * 100 if ds_stat["total"] > 0 else 0
            none_pct = ds_stat["none"] / ds_stat["total"] * 100 if ds_stat["total"] > 0 else 0
            cluster_warn = " ⚠️" if ds_stat["cluster_pct"] > 70 else ""
            spread_warn = " ⚠️" if ds_stat["spread"] < 20 else ""
            print(f"  {dim_name:>12s}  {ds_stat['mean']:5.1f}  {ds_stat['std']:5.1f}  {ds_stat['min']:5.1f}  {ds_stat['max']:5.1f}  {ds_stat['spread']:5.0f}{spread_warn}  {ds_stat['cluster_pct']:5.1f}{cluster_warn}  {full_pct:4.0f}%  {none_pct:4.0f}%")
        else:
            print(f"  {dim_name:>12s}  no data")

    # Cross-dimension correlation check
    print(f"\n  Cross-dimension independence (low correlation = good, dimensions add independent info):")
    dim_arrays: Dict[str, List[float]] = {}
    for dim_name in ALL_ROLES:
        dim_arrays[dim_name] = [
            sig.get("dimensions", {}).get(dim_name, {}).get("score", 50.0)
            for sig in all_signals
        ]

    for i, d1 in enumerate(ALL_ROLES):
        for d2 in ALL_ROLES[i+1:]:
            a1, a2 = dim_arrays[d1], dim_arrays[d2]
            n = len(a1)
            if n == 0:
                continue
            m1, m2 = sum(a1)/n, sum(a2)/n
            cov = sum((x-m1)*(y-m2) for x, y in zip(a1, a2)) / n
            s1 = (sum((x-m1)**2 for x in a1) / n) ** 0.5
            s2 = (sum((y-m2)**2 for y in a2) / n) ** 0.5
            corr = cov / (s1 * s2) if s1 > 0 and s2 > 0 else 0
            flag = " ⚠️ REDUNDANT" if abs(corr) > 0.7 else ""
            print(f"    {d1:>12s} × {d2:<12s}: r={corr:+.3f}{flag}")

    # ================================================================
    # PART 12: REGIME ANALYSIS (test set only)
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 12: REGIME ANALYSIS (test set only)")
    print(f"{'='*80}")
    print("Accuracy split by market regime and Fear & Greed level.\n")

    regime_evals: Dict[str, List] = defaultdict(list)
    fg_bucket_evals: Dict[str, List] = defaultdict(list)

    for ev in all_evals:
        regime = ev.get("regime", "unknown")
        regime_evals[regime].append(ev)

        fg = ev.get("fg_value")
        if fg is not None:
            if fg < 25:
                fg_bucket_evals["extreme_fear (<25)"].append(ev)
            elif fg < 45:
                fg_bucket_evals["fear (25-45)"].append(ev)
            elif fg < 55:
                fg_bucket_evals["neutral (45-55)"].append(ev)
            elif fg < 75:
                fg_bucket_evals["greed (55-75)"].append(ev)
            else:
                fg_bucket_evals["extreme_greed (75+)"].append(ev)

    print(f"  Accuracy by market regime:")
    print(f"  {'Regime':>12s}  {'n':>4s}  {'Gradient':>8s}  {'Binary':>6s}  {'Avg Move':>8s}")
    print(f"  {'─'*12}  {'─'*4}  {'─'*8}  {'─'*6}  {'─'*8}")
    for regime in ["trending", "ranging", "unknown"]:
        r_evals = regime_evals.get(regime, [])
        if r_evals:
            g = sum(e["gradient_score"] for e in r_evals) / len(r_evals)
            b = sum(1 for e in r_evals if e["binary_correct"]) / len(r_evals)
            m = sum(abs(e["pct_change"]) for e in r_evals) / len(r_evals)
            print(f"  {regime:>12s}  {len(r_evals):4d}  {g*100:7.1f}%  {b*100:5.1f}%  {m:7.2f}%")
        else:
            print(f"  {regime:>12s}  {0:4d}  {'—':>8s}  {'—':>6s}  {'—':>8s}")

    print(f"\n  Accuracy by Fear & Greed bucket:")
    print(f"  {'F&G Bucket':>22s}  {'n':>4s}  {'Gradient':>8s}  {'Binary':>6s}  {'Avg Move':>8s}")
    print(f"  {'─'*22}  {'─'*4}  {'─'*8}  {'─'*6}  {'─'*8}")
    for fg_label in ["extreme_fear (<25)", "fear (25-45)", "neutral (45-55)",
                    "greed (55-75)", "extreme_greed (75+)"]:
        fg_evals = fg_bucket_evals.get(fg_label, [])
        if fg_evals:
            g = sum(e["gradient_score"] for e in fg_evals) / len(fg_evals)
            b = sum(1 for e in fg_evals if e["binary_correct"]) / len(fg_evals)
            m = sum(abs(e["pct_change"]) for e in fg_evals) / len(fg_evals)
            print(f"  {fg_label:>22s}  {len(fg_evals):4d}  {g*100:7.1f}%  {b*100:5.1f}%  {m:7.2f}%")
        else:
            print(f"  {fg_label:>22s}  {0:4d}  {'—':>8s}  {'—':>6s}  {'—':>8s}")

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
