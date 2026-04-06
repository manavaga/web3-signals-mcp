# tools/indicators.py
"""Reusable indicator computation functions for backtesting.

These are the SAME formulas used by agents/technical.py and agents/market.py,
extracted into pure functions that work on lists of floats. No API calls, no
side effects.

Used by tools/backtest.py to compute indicators from historical candle data.
"""
from __future__ import annotations

import math


# ---------------------------------------------------------------------------
# Core indicators (matching agents/technical.py exactly)
# ---------------------------------------------------------------------------

def calc_rsi(closes: list[float], period: int = 14) -> float:
    """Relative Strength Index (Wilder smoothing)."""
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    if len(gains) < period:
        return 50.0
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_ema(values: list[float], period: int) -> list[float]:
    """Exponential Moving Average."""
    if len(values) < period:
        return values[:]
    multiplier = 2.0 / (period + 1)
    ema = [sum(values[:period]) / period]
    for val in values[period:]:
        ema.append((val - ema[-1]) * multiplier + ema[-1])
    return ema


def calc_macd(
    ema_fast: list[float],
    ema_slow: list[float],
    signal_period: int,
    fast_period: int,
    slow_period: int,
) -> tuple[float, float, float]:
    """MACD line, signal line, histogram."""
    offset = slow_period - fast_period
    aligned_fast = ema_fast[offset:] if offset > 0 else ema_fast
    min_len = min(len(aligned_fast), len(ema_slow))
    macd_line = [aligned_fast[i] - ema_slow[i] for i in range(min_len)]
    if not macd_line:
        return 0.0, 0.0, 0.0
    signal = calc_ema(macd_line, signal_period)
    if not signal:
        return macd_line[-1], 0.0, macd_line[-1]
    return macd_line[-1], signal[-1], macd_line[-1] - signal[-1]


def calc_bollinger(
    closes: list[float], period: int = 20, std_dev: float = 2.0
) -> dict:
    """Bollinger Bands with position and bandwidth."""
    if len(closes) < period:
        return {
            "upper": 0, "lower": 0, "middle": 0,
            "position": 0.5, "bandwidth": 0, "squeeze": False,
        }
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    sd = math.sqrt(variance)
    upper = middle + std_dev * sd
    lower = middle - std_dev * sd
    bandwidth = (upper - lower) / middle if middle > 0 else 0
    band_range = upper - lower
    position = (closes[-1] - lower) / band_range if band_range > 0 else 0.5
    squeeze = bandwidth < 0.04
    return {
        "upper": upper, "lower": lower, "middle": middle,
        "position": position, "bandwidth": bandwidth, "squeeze": squeeze,
    }


def calc_atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float:
    """Average True Range."""
    trs = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


def calc_adx(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float:
    """Average Directional Index — measures trend strength (0-100).

    ADX > 25 = trending market, ADX < 20 = ranging market.
    Uses Wilder smoothing (same as ATR/RSI).
    """
    if len(highs) < period + 2:
        return 25.0  # neutral default
    plus_dm = []
    minus_dm = []
    trs = []
    for i in range(1, len(highs)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < period:
        return 25.0
    # Wilder smoothing
    atr_s = sum(trs[:period]) / period
    plus_s = sum(plus_dm[:period]) / period
    minus_s = sum(minus_dm[:period]) / period
    dx_values = []
    for i in range(period, len(trs)):
        atr_s = (atr_s * (period - 1) + trs[i]) / period
        plus_s = (plus_s * (period - 1) + plus_dm[i]) / period
        minus_s = (minus_s * (period - 1) + minus_dm[i]) / period
        plus_di = (plus_s / atr_s * 100) if atr_s > 0 else 0
        minus_di = (minus_s / atr_s * 100) if atr_s > 0 else 0
        di_sum = plus_di + minus_di
        dx = (abs(plus_di - minus_di) / di_sum * 100) if di_sum > 0 else 0
        dx_values.append(dx)
    if len(dx_values) < period:
        return sum(dx_values) / len(dx_values) if dx_values else 25.0
    adx = sum(dx_values[:period]) / period
    for i in range(period, len(dx_values)):
        adx = (adx * (period - 1) + dx_values[i]) / period
    return adx


def calc_volume_profile(volumes: list[float], ma_period: int = 20) -> dict:
    """Volume ratio and status vs moving average."""
    if len(volumes) < ma_period + 1:
        return {"ratio": 1.0, "status": "normal"}
    vol_ma = sum(volumes[-ma_period - 1:-1]) / ma_period
    ratio = volumes[-1] / vol_ma if vol_ma > 0 else 1.0
    if ratio >= 2.0:
        status = "spike"
    elif ratio >= 1.5:
        status = "elevated"
    elif ratio < 0.5:
        status = "low"
    else:
        status = "normal"
    return {"ratio": ratio, "status": status}


def calc_obv_slope(closes: list[float], volumes: list[float]) -> float:
    """OBV slope (normalized rate of change over last 5 periods)."""
    obv = 0
    obv_values = []
    for i in range(len(closes)):
        if i == 0:
            obv_values.append(0)
            continue
        if closes[i] > closes[i - 1]:
            obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i]
        obv_values.append(obv)
    if len(obv_values) >= 6 and obv_values[-6] != 0:
        return (obv_values[-1] - obv_values[-6]) / abs(obv_values[-6])
    return 0.0


def calc_mfi(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    period: int = 14,
) -> float:
    """Money Flow Index (0-100)."""
    if len(closes) < period + 1:
        return 50.0
    typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    raw_money_flow = [tp * v for tp, v in zip(typical_prices, volumes)]
    positive_flow = sum(
        raw_money_flow[i]
        for i in range(-period, 0)
        if typical_prices[i] > typical_prices[i - 1]
    )
    negative_flow = sum(
        raw_money_flow[i]
        for i in range(-period, 0)
        if typical_prices[i] <= typical_prices[i - 1]
    )
    return 100 - (100 / (1 + positive_flow / max(negative_flow, 1e-10)))


def calc_roc(closes: list[float]) -> tuple[float, float, float]:
    """Rate of Change at 1d, 7d, 30d."""
    roc_1d = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
    roc_7d = (closes[-1] - closes[-8]) / closes[-8] * 100 if len(closes) >= 8 else 0
    roc_30d = (closes[-1] - closes[-31]) / closes[-31] * 100 if len(closes) >= 31 else 0
    return roc_1d, roc_7d, roc_30d


def calc_stoch_rsi(closes: list[float], period: int = 14) -> float:
    """Stochastic RSI (0-1 scale)."""
    if len(closes) < period * 2:
        return 0.5
    rsi_series = []
    for end in range(period + 1, len(closes) + 1):
        window = closes[max(0, end - period * 2) : end]
        rsi_val = calc_rsi(window, period)
        rsi_series.append(rsi_val)
    if len(rsi_series) < period:
        return 0.5
    recent = rsi_series[-period:]
    rsi_min = min(recent)
    rsi_max = max(recent)
    current_rsi = rsi_series[-1]
    if rsi_max == rsi_min:
        return 0.5
    return (current_rsi - rsi_min) / (rsi_max - rsi_min)


def calc_squeeze(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    bb_period: int = 20,
    bb_std: float = 2.0,
) -> tuple[bool, float]:
    """BB/Keltner Channel squeeze detection."""
    if len(closes) < bb_period:
        return False, 0.0
    window = closes[-bb_period:]
    sma_20 = sum(window) / bb_period
    variance = sum((x - sma_20) ** 2 for x in window) / bb_period
    sd = math.sqrt(variance)
    bb_upper = sma_20 + bb_std * sd
    bb_lower = sma_20 - bb_std * sd
    atr_20 = calc_atr(highs, lows, closes, bb_period)
    kc_upper = sma_20 + 1.5 * atr_20
    kc_lower = sma_20 - 1.5 * atr_20
    squeeze_on = (bb_lower > kc_lower) and (bb_upper < kc_upper)
    squeeze_momentum = (closes[-1] - sma_20) / atr_20 if atr_20 > 0 else 0.0
    return squeeze_on, squeeze_momentum


def calc_zscores(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    bb_period: int = 20,
    bb_std: float = 2.0,
) -> dict[str, float]:
    """Z-scores for RSI, MACD histogram, BB position over 50-period rolling window."""
    min_needed = 50
    if len(closes) < min_needed:
        return {"rsi_zscore": 0.0, "macd_zscore": 0.0, "bb_zscore": 0.0}

    rsi_vals = []
    macd_hist_vals = []
    bb_pos_vals = []

    for end in range(len(closes) - min_needed, len(closes)):
        window = closes[: end + 1]
        if len(window) < max(rsi_period + 1, macd_slow + 1, bb_period):
            continue

        rsi_vals.append(calc_rsi(window, rsi_period))

        ema_f = calc_ema(window, macd_fast)
        ema_s = calc_ema(window, macd_slow)
        _, _, hist = calc_macd(ema_f, ema_s, macd_signal, macd_fast, macd_slow)
        macd_hist_vals.append(hist)

        bb = calc_bollinger(window, bb_period, bb_std)
        bb_pos_vals.append(bb["position"])

    def _zscore(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean_v = sum(values) / len(values)
        std_v = math.sqrt(sum((x - mean_v) ** 2 for x in values) / len(values))
        if std_v == 0:
            return 0.0
        return (values[-1] - mean_v) / std_v

    return {
        "rsi_zscore": _zscore(rsi_vals),
        "macd_zscore": _zscore(macd_hist_vals),
        "bb_zscore": _zscore(bb_pos_vals),
    }


# ---------------------------------------------------------------------------
# High-level: compute all technical indicators from a candle slice
# ---------------------------------------------------------------------------

def compute_technical_indicators(
    candles: list[dict],
    cfg: dict | None = None,
) -> dict:
    """Compute all technical indicators from candle data.

    Args:
        candles: List of dicts with keys: open, high, low, close, volume.
                 Must be in chronological order.
        cfg: Optional config dict (rsi_period, macd_fast, etc.)

    Returns: dict compatible with score_technical() input format.
    """
    if not cfg:
        cfg = {}

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    if len(closes) < 2:
        return {}

    rsi_period = cfg.get("rsi_period", 14)
    macd_fast = cfg.get("macd_fast", 12)
    macd_slow = cfg.get("macd_slow", 26)
    macd_signal_period = cfg.get("macd_signal", 9)
    bb_period = cfg.get("bb_period", 20)
    bb_std = cfg.get("bb_std_dev", 2)
    volume_ma = cfg.get("volume_ma_period", 20)
    mfi_period = cfg.get("mfi_period", 14)

    rsi = calc_rsi(closes, rsi_period)
    ema_fast = calc_ema(closes, macd_fast)
    ema_slow = calc_ema(closes, macd_slow)
    macd_line, signal_line, histogram = calc_macd(
        ema_fast, ema_slow, macd_signal_period, macd_fast, macd_slow
    )
    bb = calc_bollinger(closes, bb_period, bb_std)
    atr_period = cfg.get("atr_period", 14)
    atr = calc_atr(highs, lows, closes, atr_period)
    adx = calc_adx(highs, lows, closes, atr_period)
    vol_profile = calc_volume_profile(volumes, volume_ma)
    obv_slope = calc_obv_slope(closes, volumes)
    roc_1d, roc_7d, roc_30d = calc_roc(closes)
    squeeze_on, squeeze_momentum = calc_squeeze(highs, lows, closes, bb_period, bb_std)
    zscores = calc_zscores(
        closes, highs, lows, rsi_period, macd_fast, macd_slow,
        macd_signal_period, bb_period, bb_std,
    )

    price = closes[-1]
    ma7 = sum(closes[-7:]) / min(7, len(closes))
    ma30 = sum(closes[-30:]) / min(30, len(closes))

    # Swing high/low from recent 20 candles (for S/R-based TP/SL)
    recent_highs = highs[-20:] if len(highs) >= 20 else highs
    recent_lows = lows[-20:] if len(lows) >= 20 else lows
    swing_high = max(recent_highs) if recent_highs else price
    swing_low = min(recent_lows) if recent_lows else price

    return {
        "price": price,
        "rsi_14": rsi,
        "macd_line": macd_line,
        "macd_signal": signal_line,
        "macd_histogram": histogram,
        "bb_upper": bb["upper"],
        "bb_lower": bb["lower"],
        "bb_middle": bb["middle"],
        "bb_bandwidth": bb["bandwidth"],
        "swing_high": swing_high,
        "swing_low": swing_low,
        "atr_14": atr,
        "adx_14": adx,
        "atr_pct": (atr / price * 100) if price > 0 else 0,
        "ma7": ma7,
        "ma30": ma30,
        "volume_ratio": vol_profile["ratio"],
        "volume_status": vol_profile["status"],
        "obv_slope": obv_slope,
        "roc_7d": roc_7d,
        "squeeze_on": squeeze_on,
        "squeeze_momentum": squeeze_momentum,
        "rsi_zscore": zscores["rsi_zscore"],
        "macd_zscore": zscores["macd_zscore"],
        "bb_zscore": zscores["bb_zscore"],
    }


# ---------------------------------------------------------------------------
# Market indicators from historical data
# ---------------------------------------------------------------------------

def compute_market_indicators(
    macro_rows: dict[str, list[dict]],
    fg_entries: list[dict],
    day_idx: int,
) -> dict:
    """Compute market dimension indicators from historical macro/F&G data.

    Args:
        macro_rows: {"sp500": [...], "dxy": [...], "nasdaq": [...], "vix": [...]}
                    Each entry has: {"date": str, "close": float, "change_pct": float}
        fg_entries: [{"date": str, "value": int}, ...]
        day_idx: Index into the F&G entries list (0-based, chronological)

    Returns: dict compatible with score_market() input format.
    """
    # Fear & Greed
    fg_value = 50
    if fg_entries and day_idx < len(fg_entries):
        fg_value = fg_entries[day_idx].get("value", 50)

    # Macro data
    def _get_macro_change(source: str) -> float:
        entries = macro_rows.get(source, [])
        if entries and day_idx < len(entries):
            return entries[day_idx].get("change_pct", 0.0)
        return 0.0

    def _get_macro_close(source: str) -> float:
        entries = macro_rows.get(source, [])
        if entries and day_idx < len(entries):
            return entries[day_idx].get("close", 0.0)
        return 0.0

    sp500_change = _get_macro_change("sp500")
    dxy_change = _get_macro_change("dxy")
    nasdaq_change = _get_macro_change("nasdaq")
    vix_roc = _get_macro_change("vix")

    # Compute macro_status
    vix_close = _get_macro_close("vix")
    if vix_close > 25 or sp500_change < -1.5:
        macro_status = "strong_risk_off"
    elif vix_close < 18 and sp500_change > 0.5:
        macro_status = "strong_risk_on"
    elif sp500_change > 0:
        macro_status = "risk_on"
    elif sp500_change < 0:
        macro_status = "risk_off"
    else:
        macro_status = "neutral"

    return {
        "fear_greed": fg_value,
        "macro_status": macro_status,
        "sp500_change": sp500_change,
        "dxy_change": dxy_change,
        "nasdaq_change": nasdaq_change,
        "vix_roc": vix_roc,
        "btc_dominance": 50.0,  # Not available historically, use neutral
        "breadth_status": "neutral",
        "volume_ratio": 1.0,  # Will be overridden per-asset if available
        "order_book_imbalance": 1.0,  # Not available historically
        "stablecoin_supply_total": 0,
        "stablecoin_supply_change_7d": 0.0,
    }
