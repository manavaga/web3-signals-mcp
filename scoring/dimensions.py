# scoring/dimensions.py
"""Per-dimension scoring — pure functions, no side effects.

Each function: (agent_data, config) → DimensionScore
All formulas ported from the original system's agent scoring logic.
"""
from __future__ import annotations
from typing import Any, Optional
from scoring.types import DimensionScore

NO_DATA_KEYWORDS = ["no data", "unavailable", "n/a", "none", "empty", "no_data"]
FULL_DATA_KEYWORDS = ["rsi", "macd", "funding", "fear_greed", "order_book", "volume"]


def detect_data_tier(score: float, detail: str) -> str:
    detail_lower = detail.lower()
    if detail_lower.startswith("error:"):
        return "none"
    if any(kw in detail_lower for kw in NO_DATA_KEYWORDS):
        return "none"
    if any(kw in detail_lower for kw in FULL_DATA_KEYWORDS):
        return "full"
    return "partial"


def _clamp(val: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, val))


# --- Technical ---

def _rsi_score(rsi: float, oversold: int = 30, overbought: int = 70) -> float:
    if rsi <= oversold:
        return 95.0 - (rsi / oversold) * 20.0
    elif rsi >= overbought:
        return 25.0 - ((rsi - overbought) / (100 - overbought)) * 20.0
    else:
        return 65.0 - (rsi - oversold) / (overbought - oversold) * 30.0


def _macd_score(histogram: float, price: float) -> float:
    macd_pct = (histogram / price) * 100 if price > 0 else 0
    if macd_pct > 0:
        intensity = min(macd_pct / 2.0, 1.0)
        return 50 + intensity * 40
    else:
        intensity = min(abs(macd_pct) / 2.0, 1.0)
        return 50 - intensity * 40


def _bb_bandwidth_score(bandwidth: float) -> float:
    """BB bandwidth: high bandwidth = high volatility = bearish (IC -0.22 to -0.29).

    Low bandwidth = compression = potential breakout = bullish.
    Typical crypto bandwidth: 0.02 (tight) to 0.15 (wide).
    """
    if bandwidth < 0.03:
        return 75.0
    elif bandwidth > 0.12:
        return 25.0
    else:
        return 75.0 - (bandwidth - 0.03) / 0.09 * 50.0


def _trend_score(price: float, ma7: float, ma30: float) -> float:
    above_7d = price > ma7
    above_30d = price > ma30
    if above_7d and above_30d:
        return 85.0
    elif above_7d:
        return 60.0
    elif not above_7d and not above_30d:
        return 15.0
    elif not above_7d:
        return 35.0
    return 50.0


def _obv_score(obv_slope: float) -> float:
    """OBV slope: data shows high OBV slope → price DOWN (IC negative).

    Inverted from traditional interpretation — selling into strength."""
    return 50 - min(max(obv_slope * 400, -40), 40)


def _roc_score(roc_7d: float) -> float:
    return 50 + min(max(roc_7d * 3, -35), 35)


def _squeeze_score(squeeze_on: bool, squeeze_momentum: float) -> float:
    if squeeze_on:
        return 50.0
    return 50 + min(max(squeeze_momentum * 5, -30), 30)


def _macd_zscore_score(zscore: float) -> float:
    """MACD z-score: how extreme current MACD is vs recent history.

    Positive zscore = MACD above its recent mean = bullish momentum.
    IC +0.14 to +0.17 across multiple assets — independent predictor.
    """
    return 50 + min(max(zscore * 15, -35), 35)


def score_technical(data: Optional[dict], cfg: dict, regime: str = "") -> DimensionScore:
    """Score technical indicators, regime-aware.

    Key data-driven changes:
    - bb_bandwidth replaces bb_position (IC -0.22 vs IC ≈ 0)
    - OBV slope inverted (high OBV → price DOWN per IC data)
    - macd_zscore added (IC +0.14, independent predictor)
    - Regime-specific weights: trending uses momentum, ranging uses mean-reversion
    - RSI/bandwidth clamped in trending regimes to suppress counter-trend signals
    """
    if not data:
        return DimensionScore(name="technical", score=50.0, detail="no data", tier="none")

    rsi = data.get("rsi_14", 50.0)
    hist = data.get("macd_histogram", 0.0)
    price = data.get("price", 1.0)
    bb_bandwidth = data.get("bb_bandwidth", 0.06)
    ma7 = data.get("ma7", price)
    ma30 = data.get("ma30", price)
    vol_status = data.get("volume_status", "normal")

    obv_slope = data.get("obv_slope", 0.0)
    roc_7d = data.get("roc_7d", 0.0)
    squeeze_on = data.get("squeeze_on", False)
    squeeze_momentum = data.get("squeeze_momentum", 0.0)
    macd_zscore = data.get("macd_zscore", 0.0)

    # Regime-specific weights from config, falling back to defaults
    # Trending: momentum indicators (trend, roc) weighted higher
    # Ranging: mean-reversion indicators (rsi, bb_bandwidth) weighted higher
    regime_weights = cfg.get("regime_scoring_weights", {})
    if regime in ("trending_up", "trending_down") and "trending" in regime_weights:
        default_weights = regime_weights["trending"]
    elif regime == "ranging" and "ranging" in regime_weights:
        default_weights = regime_weights["ranging"]
    else:
        default_weights = {
            "rsi": 0.15, "macd": 0.10, "bb_bandwidth": 0.15, "trend": 0.15,
            "obv": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "macd_zscore": 0.10,
        }
    weights = cfg.get("scoring_weights", default_weights)

    oversold = cfg.get("rsi_oversold", 30)
    overbought = cfg.get("rsi_overbought", 70)
    spike_bonus = cfg.get("volume_spike_bonus", 10)

    rsi_s = _clamp(_rsi_score(rsi, oversold, overbought), 5, 95)
    macd_s = _clamp(_macd_score(hist, price), 10, 90)
    bw_s = _clamp(_bb_bandwidth_score(bb_bandwidth), 10, 90)
    trend_s = _trend_score(price, ma7, ma30)
    obv_s = _clamp(_obv_score(obv_slope), 10, 90)
    roc_s = _clamp(_roc_score(roc_7d), 15, 85)
    sq_s = _clamp(_squeeze_score(squeeze_on, squeeze_momentum), 20, 80)
    mz_s = _clamp(_macd_zscore_score(macd_zscore), 15, 85)

    # Regime-aware: suppress mean-reversion signals that fight the trend
    if regime == "trending_down":
        rsi_s = min(rsi_s, 50.0)
        bw_s = min(bw_s, 50.0)
    elif regime == "trending_up":
        rsi_s = max(rsi_s, 50.0)
        bw_s = max(bw_s, 50.0)

    components = [
        (rsi_s, weights.get("rsi", 0.15)),
        (macd_s, weights.get("macd", 0.10)),
        (bw_s, weights.get("bb_bandwidth", 0.15)),
        (trend_s, weights.get("trend", 0.15)),
        (obv_s, weights.get("obv", 0.15)),
        (roc_s, weights.get("roc_7d", 0.10)),
        (sq_s, weights.get("squeeze", 0.10)),
        (mz_s, weights.get("macd_zscore", 0.10)),
    ]
    total_w = sum(w for _, w in components)
    score = sum(s * w for s, w in components) / total_w if total_w > 0 else 50.0

    if vol_status == "spike":
        score += spike_bonus

    score = _clamp(score)
    detail = (f"RSI={rsi:.0f}, MACD_hist={hist:.4f}, BB_bw={bb_bandwidth:.4f}, "
              f"trend={'up' if price > ma30 else 'down'}, "
              f"OBV_slope={obv_slope:.3f}, ROC_7d={roc_7d:.1f}%, "
              f"squeeze={'on' if squeeze_on else 'off'}, MACD_z={macd_zscore:.2f}")
    tier = "full" if rsi != 50 or hist != 0 else "partial"

    return DimensionScore(name="technical", score=score, detail=detail, tier=tier)


# --- Derivatives ---

def score_derivatives(data: Optional[dict], cfg: dict) -> DimensionScore:
    if not data:
        return DimensionScore(name="derivatives", score=50.0, detail="no data", tier="none")

    ls = data.get("long_short_ratio", 0.5)
    fr = data.get("funding_rate", 0.0)
    oi_chg = data.get("oi_change_pct", 0.0)
    liq_imb = data.get("liq_imbalance", 0.0)
    taker = data.get("taker_buy_sell_ratio", 1.0)

    weights = cfg.get("scoring_weights", {})
    overcrowded = cfg.get("ls_overcrowded", 0.65)
    shorts_dom = cfg.get("ls_shorts_dominating", 0.55)
    funding_extreme = cfg.get("funding_extreme", 0.001)
    liq_thresh = cfg.get("liq_imbalance_threshold", 0.3)

    # L/S score
    mid = (shorts_dom + overcrowded) / 2
    if ls > overcrowded:
        intensity = min((ls - overcrowded) / 0.15, 1.0)
        ls_score = 35 - intensity * 25
    elif ls < shorts_dom:
        intensity = min((shorts_dom - ls) / 0.15, 1.0)
        ls_score = 65 + intensity * 25
    else:
        if ls > mid:
            ls_score = 50 - (ls - mid) / (overcrowded - mid) * 15
        else:
            ls_score = 50 + (mid - ls) / (mid - shorts_dom) * 15

    # Funding score
    if fr > funding_extreme:
        intensity = min((fr - funding_extreme) / 0.002, 1.0)
        funding_score = 20 - intensity * 15
    elif fr > 0.0005:
        intensity = (fr - 0.0005) / 0.0005
        funding_score = 35 - intensity * 15
    elif fr >= 0:
        intensity = fr / 0.0005 if 0.0005 > 0 else 0
        funding_score = 50 - intensity * 15
    elif fr > -funding_extreme:
        intensity = abs(fr) / funding_extreme
        funding_score = 50 + intensity * 30
    else:
        intensity = min((abs(fr) - funding_extreme) / 0.002, 1.0)
        funding_score = 80 + intensity * 15

    # OI score
    if oi_chg > 0 and fr > 0.0005:
        intensity = min(oi_chg / 10, 1.0)
        oi_score = 35 - intensity * 20
    elif oi_chg > 0:
        intensity = min(oi_chg / 10, 1.0)
        oi_score = 55 + intensity * 20
    elif oi_chg < -5:
        intensity = min(abs(oi_chg) / 15, 1.0)
        oi_score = 55 + intensity * 15
    else:
        oi_score = 50.0

    # Liquidation score
    if abs(liq_imb) > liq_thresh:
        intensity = min(abs(liq_imb), 1.0)
        if liq_imb > 0:
            liq_score = 35 - intensity * 25
        else:
            liq_score = 65 + intensity * 25
    else:
        liq_score = 50.0

    # Taker score
    if taker > 1.0:
        intensity = min((taker - 1.0) / 0.3, 1.0)
        taker_score = 55 + intensity * 35
    else:
        intensity = min((1.0 - taker) / 0.3, 1.0)
        taker_score = 45 - intensity * 35

    w = weights
    score = (w.get("long_short", 0.20) * ls_score +
             w.get("funding", 0.25) * funding_score +
             w.get("open_interest", 0.15) * oi_score +
             w.get("liquidations", 0.20) * liq_score +
             w.get("taker_ratio", 0.20) * taker_score)

    score = _clamp(score)
    detail = f"L/S={ls:.2f}, FR={fr:.6f}, OI_chg={oi_chg:.1f}%, taker={taker:.2f}"
    return DimensionScore(name="derivatives", score=score, detail=detail, tier="full")


# --- Market ---

def _fg_score(fg: int) -> float:
    return _clamp(90.0 - (fg / 100.0) * 80.0, 10, 90)


def _volume_score(ratio: float) -> float:
    if ratio > 3.0:
        return 85.0
    elif ratio > 2.0:
        return 70.0 + (ratio - 2.0) * 15.0
    elif ratio > 1.0:
        return 55.0 + (ratio - 1.0) * 15.0
    elif ratio > 0.5:
        return 35.0 + (ratio - 0.5) * 40.0
    else:
        return 20.0 + ratio * 30.0


def _breadth_score(status: str) -> float:
    if status == "gainer":
        return 75.0
    elif status == "loser":
        return 25.0
    return 50.0


def _macro_score(status: str) -> float:
    return {"strong_risk_on": 80, "risk_on": 70, "neutral": 50, "unknown": 50,
            "risk_off": 30, "strong_risk_off": 20}.get(status, 50.0)


def _order_book_score(imbalance: float) -> float:
    if imbalance >= 1.0:
        intensity = min((imbalance - 1.0) / 1.5, 1.0)
        return 50.0 + intensity * 20.0
    else:
        intensity = min((1.0 - imbalance) / 0.7, 1.0)
        return 50.0 - intensity * 20.0


def _stablecoin_score(change_7d: float) -> float:
    """Stablecoin supply growth: positive = money entering crypto = bullish."""
    return _clamp(50 + min(max(change_7d * 5, -30), 30), 20, 80)


def _dxy_score(dxy_change: float) -> float:
    """DXY inverse correlation: DXY up = bearish for crypto."""
    return _clamp(50 - min(max(dxy_change * 15, -30), 30), 20, 80)


def _nasdaq_score(nasdaq_change: float) -> float:
    """NASDAQ correlation: NASDAQ up = bullish for crypto."""
    return _clamp(50 + min(max(nasdaq_change * 10, -30), 30), 20, 80)


def _vix_roc_score(vix_roc: float) -> float:
    """VIX rate of change: falling VIX = risk-on = bullish."""
    return _clamp(50 - min(max(vix_roc * 2, -25), 25), 25, 75)


def score_market(data: Optional[dict], cfg: dict) -> DimensionScore:
    if not data:
        return DimensionScore(name="market", score=50.0, detail="no data", tier="none")

    fg = data.get("fear_greed", 50)
    vol_ratio = data.get("volume_ratio", 1.0)
    macro = data.get("macro_status", "neutral")
    ob_imb = data.get("order_book_imbalance", 1.0)

    stable_change = data.get("stablecoin_supply_change_7d", 0.0)
    dxy_change = data.get("dxy_change", 0.0)
    nasdaq_change = data.get("nasdaq_change", 0.0)
    vix_roc = data.get("vix_roc", 0.0)

    weights = cfg.get("scoring_weights", {})

    fg_s = _fg_score(fg)
    vol_s = _volume_score(vol_ratio)
    mac_s = _macro_score(macro)
    ob_s = _order_book_score(ob_imb)
    stable_s = _stablecoin_score(stable_change)
    dxy_s = _dxy_score(dxy_change)
    nasdaq_s = _nasdaq_score(nasdaq_change)
    vix_s = _vix_roc_score(vix_roc)

    components = [
        (fg_s, weights.get("fear_greed", 0.15)),
        (vol_s, weights.get("volume", 0.10)),
        (mac_s, weights.get("macro", 0.15)),
        (ob_s, weights.get("order_book", 0.15)),
        (stable_s, weights.get("stablecoin", 0.15)),
        (dxy_s, weights.get("dxy", 0.10)),
        (nasdaq_s, weights.get("nasdaq", 0.10)),
        (vix_s, weights.get("vix_roc", 0.10)),
    ]
    total_w = sum(w for _, w in components)
    score = sum(s * w for s, w in components) / total_w if total_w > 0 else 50.0

    score = _clamp(score)
    detail = (f"F&G={fg}, vol_ratio={vol_ratio:.1f}, macro={macro}, OB_imb={ob_imb:.2f}, "
              f"stable_chg={stable_change:.1f}%, DXY={dxy_change:.1f}%, "
              f"NASDAQ={nasdaq_change:.1f}%, VIX_ROC={vix_roc:.1f}%")
    return DimensionScore(name="market", score=score, detail=detail, tier="full")
