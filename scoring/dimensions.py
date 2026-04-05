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


def _bb_score(position: float) -> float:
    if position < 0:
        return 90.0
    elif position > 1:
        return 10.0
    else:
        return 85.0 - position * 70.0


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
    return 50 + min(max(obv_slope * 400, -40), 40)


def _mfi_score(mfi: float) -> float:
    return 90 - (mfi / 100) * 80


def _roc_score(roc_7d: float) -> float:
    return 50 + min(max(roc_7d * 3, -35), 35)


def _squeeze_score(squeeze_on: bool, squeeze_momentum: float) -> float:
    if squeeze_on:
        return 50.0
    return 50 + min(max(squeeze_momentum * 5, -30), 30)


def _stochrsi_score(stoch_rsi: float) -> float:
    return 90 - stoch_rsi * 80


def score_technical(data: Optional[dict], cfg: dict) -> DimensionScore:
    if not data:
        return DimensionScore(name="technical", score=50.0, detail="no data", tier="none")

    rsi = data.get("rsi_14", 50.0)
    hist = data.get("macd_histogram", 0.0)
    price = data.get("price", 1.0)
    bb_pos = data.get("bb_position", 0.5)
    ma7 = data.get("ma7", price)
    ma30 = data.get("ma30", price)
    vol_status = data.get("volume_status", "normal")

    # New indicator values (with safe defaults for backward compatibility)
    obv_slope = data.get("obv_slope", 0.0)
    mfi = data.get("mfi", 50.0)
    roc_7d = data.get("roc_7d", 0.0)
    squeeze_on = data.get("squeeze_on", False)
    squeeze_momentum = data.get("squeeze_momentum", 0.0)
    stoch_rsi = data.get("stoch_rsi", 0.5)

    default_weights = {
        "rsi": 0.10, "macd": 0.10, "bollinger": 0.10, "trend": 0.15,
        "obv": 0.15, "mfi": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "stoch_rsi": 0.05,
    }
    weights = cfg.get("scoring_weights", default_weights)
    oversold = cfg.get("rsi_oversold", 30)
    overbought = cfg.get("rsi_overbought", 70)
    spike_bonus = cfg.get("volume_spike_bonus", 10)

    rsi_s = _clamp(_rsi_score(rsi, oversold, overbought), 5, 95)
    macd_s = _clamp(_macd_score(hist, price), 10, 90)
    bb_s = _clamp(_bb_score(bb_pos), 10, 90)
    trend_s = _trend_score(price, ma7, ma30)
    obv_s = _clamp(_obv_score(obv_slope), 10, 90)
    mfi_s = _clamp(_mfi_score(mfi), 10, 90)
    roc_s = _clamp(_roc_score(roc_7d), 15, 85)
    sq_s = _clamp(_squeeze_score(squeeze_on, squeeze_momentum), 20, 80)
    stochrsi_s = _clamp(_stochrsi_score(stoch_rsi), 10, 90)

    components = [
        (rsi_s, weights.get("rsi", 0.10)),
        (macd_s, weights.get("macd", 0.10)),
        (bb_s, weights.get("bollinger", 0.10)),
        (trend_s, weights.get("trend", 0.15)),
        (obv_s, weights.get("obv", 0.15)),
        (mfi_s, weights.get("mfi", 0.15)),
        (roc_s, weights.get("roc_7d", 0.10)),
        (sq_s, weights.get("squeeze", 0.10)),
        (stochrsi_s, weights.get("stoch_rsi", 0.05)),
    ]
    total_w = sum(w for _, w in components)
    score = sum(s * w for s, w in components) / total_w if total_w > 0 else 50.0

    if vol_status == "spike":
        score += spike_bonus

    score = _clamp(score)
    detail = (f"RSI={rsi:.0f}, MACD_hist={hist:.4f}, BB_pos={bb_pos:.2f}, "
              f"trend={'up' if price > ma30 else 'down'}, "
              f"OBV_slope={obv_slope:.3f}, MFI={mfi:.0f}, ROC_7d={roc_7d:.1f}%, "
              f"StochRSI={stoch_rsi:.2f}, squeeze={'on' if squeeze_on else 'off'}")
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


def score_market(data: Optional[dict], cfg: dict) -> DimensionScore:
    if not data:
        return DimensionScore(name="market", score=50.0, detail="no data", tier="none")

    fg = data.get("fear_greed", 50)
    vol_ratio = data.get("volume_ratio", 1.0)
    breadth = data.get("breadth_status", "neutral")
    macro = data.get("macro_status", "neutral")
    ob_imb = data.get("order_book_imbalance", 1.0)

    weights = cfg.get("scoring_weights", {})

    fg_s = _fg_score(fg)
    vol_s = _volume_score(vol_ratio)
    br_s = _breadth_score(breadth)
    mac_s = _macro_score(macro)
    ob_s = _order_book_score(ob_imb)

    score = (weights.get("fear_greed", 0.25) * fg_s +
             weights.get("volume", 0.15) * vol_s +
             weights.get("breadth", 0.15) * br_s +
             weights.get("macro", 0.20) * mac_s +
             weights.get("order_book", 0.25) * ob_s)

    score = _clamp(score)
    detail = f"F&G={fg}, vol_ratio={vol_ratio:.1f}, macro={macro}, OB_imb={ob_imb:.2f}"
    return DimensionScore(name="market", score=score, detail=detail, tier="full")
