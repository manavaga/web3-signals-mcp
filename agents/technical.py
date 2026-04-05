# agents/technical.py
"""Technical analysis agent — RSI, MACD, BB, ATR, volume, pivots.

Data source: Binance spot klines (free, no API key).
"""
from __future__ import annotations
import logging
import math
from urllib.request import urlopen
from urllib.error import URLError
import json
from typing import Any
from agents.base import BaseAgent

logger = logging.getLogger(__name__)

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


class TechnicalAgent(BaseAgent):
    def __init__(self, config: dict, symbols: dict[str, str]):
        super().__init__("technical_agent")
        self.config = config
        self.symbols = symbols

    def empty_data(self) -> dict[str, Any]:
        return {asset: {} for asset in self.symbols}

    def collect(self) -> tuple[dict[str, Any], list[str]]:
        results = {}
        errors = []
        limit = max(self.config.get("binance_kline_limit", 60), 60)  # Need >=60 for z-scores

        for asset, symbol in self.symbols.items():
            try:
                candles = self._fetch_klines(symbol, "1d", limit)
                if len(candles) < 30:
                    errors.append(f"{asset}: insufficient candles ({len(candles)})")
                    results[asset] = {}
                    continue

                closes = [c["close"] for c in candles]
                highs = [c["high"] for c in candles]
                lows = [c["low"] for c in candles]
                volumes = [c["volume"] for c in candles]

                rsi = self._calc_rsi(closes, self.config.get("rsi_period", 14))
                ema_fast = self._calc_ema(closes, self.config.get("macd_fast", 12))
                ema_slow = self._calc_ema(closes, self.config.get("macd_slow", 26))
                macd_line, signal_line, histogram = self._calc_macd(
                    ema_fast, ema_slow, self.config.get("macd_signal", 9),
                    self.config.get("macd_fast", 12), self.config.get("macd_slow", 26))
                bb = self._calc_bollinger(closes, self.config.get("bb_period", 20),
                                          self.config.get("bb_std_dev", 2))
                atr = self._calc_atr(highs, lows, closes, self.config.get("atr_period", 14))
                vol_profile = self._calc_volume_profile(volumes, self.config.get("volume_ma_period", 20))
                pivots = self._calc_pivots(highs[-1], lows[-1], closes[-1])

                # ADX for regime detection
                adx = self._calc_adx(highs, lows, closes, self.config.get("atr_period", 14))

                # New indicators
                obv_slope = self._calc_obv_slope(closes, volumes)
                mfi = self._calc_mfi(highs, lows, closes, volumes,
                                     self.config.get("mfi_period", 14))
                roc_1d, roc_7d, roc_30d = self._calc_roc(closes)
                stoch_rsi = self._calc_stoch_rsi(closes, self.config.get("rsi_period", 14))
                bb_period = self.config.get("bb_period", 20)
                bb_std = self.config.get("bb_std_dev", 2)
                squeeze_on, squeeze_momentum = self._calc_squeeze(
                    highs, lows, closes, bb_period, bb_std)
                zscores = self._calc_zscores(
                    closes, highs, lows,
                    self.config.get("rsi_period", 14),
                    self.config.get("macd_fast", 12),
                    self.config.get("macd_slow", 26),
                    self.config.get("macd_signal", 9),
                    bb_period, bb_std)

                price = closes[-1]
                ma7 = sum(closes[-7:]) / 7
                ma30 = sum(closes[-30:]) / 30

                results[asset] = {
                    "price": price,
                    "rsi_14": rsi,
                    "macd_line": macd_line,
                    "macd_signal": signal_line,
                    "macd_histogram": histogram,
                    "bb_upper": bb["upper"],
                    "bb_lower": bb["lower"],
                    "bb_middle": bb["middle"],
                    "bb_position": bb["position"],
                    "bb_bandwidth": bb["bandwidth"],
                    "bb_squeeze": bb["squeeze"],
                    "atr_14": atr,
                    "atr_pct": (atr / price * 100) if price > 0 else 0,
                    "adx_14": adx,
                    "ma7": ma7,
                    "ma30": ma30,
                    "volume_ratio": vol_profile["ratio"],
                    "volume_status": vol_profile["status"],
                    "pivot": pivots["pivot"],
                    "r1": pivots["r1"],
                    "r2": pivots["r2"],
                    "s1": pivots["s1"],
                    "s2": pivots["s2"],
                    # New indicators
                    "obv_slope": obv_slope,
                    "mfi": mfi,
                    "roc_1d": roc_1d,
                    "roc_7d": roc_7d,
                    "roc_30d": roc_30d,
                    "stoch_rsi": stoch_rsi,
                    "squeeze_on": squeeze_on,
                    "squeeze_momentum": squeeze_momentum,
                    "rsi_zscore": zscores["rsi_zscore"],
                    "macd_zscore": zscores["macd_zscore"],
                    "bb_zscore": zscores["bb_zscore"],
                }
            except Exception as e:
                logger.error(f"Technical agent error for {asset}: {e}")
                errors.append(f"{asset}: {e}")
                results[asset] = {}

        return results, errors

    def _fetch_klines(self, symbol: str, interval: str, limit: int) -> list[dict]:
        url = f"{BINANCE_KLINES}?symbol={symbol}&interval={interval}&limit={limit}"
        resp = urlopen(url, timeout=15)
        raw = json.loads(resp.read())
        return [
            {"open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
             "close": float(c[4]), "volume": float(c[5])}
            for c in raw
        ]

    def _calc_rsi(self, closes: list[float], period: int) -> float:
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

    def _calc_ema(self, values: list[float], period: int) -> list[float]:
        if len(values) < period:
            return values[:]
        multiplier = 2.0 / (period + 1)
        ema = [sum(values[:period]) / period]
        for val in values[period:]:
            ema.append((val - ema[-1]) * multiplier + ema[-1])
        return ema

    def _calc_macd(self, ema_fast, ema_slow, signal_period, fast_period, slow_period):
        offset = slow_period - fast_period
        aligned_fast = ema_fast[offset:] if offset > 0 else ema_fast
        min_len = min(len(aligned_fast), len(ema_slow))
        macd_line = [aligned_fast[i] - ema_slow[i] for i in range(min_len)]
        if not macd_line:
            return 0.0, 0.0, 0.0
        signal = self._calc_ema(macd_line, signal_period)
        if not signal:
            return macd_line[-1], 0.0, macd_line[-1]
        return macd_line[-1], signal[-1], macd_line[-1] - signal[-1]

    def _calc_bollinger(self, closes, period, std_dev):
        if len(closes) < period:
            return {"upper": 0, "lower": 0, "middle": 0, "position": 0.5,
                    "bandwidth": 0, "squeeze": False}
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
        return {"upper": upper, "lower": lower, "middle": middle,
                "position": position, "bandwidth": bandwidth, "squeeze": squeeze}

    def _calc_atr(self, highs, lows, closes, period):
        trs = []
        for i in range(1, len(highs)):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        if len(trs) < period:
            return sum(trs) / len(trs) if trs else 0
        atr = sum(trs[:period]) / period
        for i in range(period, len(trs)):
            atr = (atr * (period - 1) + trs[i]) / period
        return atr

    def _calc_adx(self, highs, lows, closes, period):
        from tools.indicators import calc_adx
        return calc_adx(highs, lows, closes, period)

    def _calc_volume_profile(self, volumes, ma_period):
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

    # --- New indicators ---

    def _calc_obv_slope(self, closes: list[float], volumes: list[float]) -> float:
        """On-Balance Volume slope (normalized rate of change over last 5 periods)."""
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

    def _calc_mfi(self, highs: list[float], lows: list[float],
                  closes: list[float], volumes: list[float], period: int = 14) -> float:
        """Money Flow Index (0-100, volume-weighted RSI)."""
        if len(closes) < period + 1:
            return 50.0
        typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
        raw_money_flow = [tp * v for tp, v in zip(typical_prices, volumes)]
        positive_flow = sum(
            raw_money_flow[i] for i in range(-period, 0)
            if typical_prices[i] > typical_prices[i - 1]
        )
        negative_flow = sum(
            raw_money_flow[i] for i in range(-period, 0)
            if typical_prices[i] <= typical_prices[i - 1]
        )
        mfi = 100 - (100 / (1 + positive_flow / max(negative_flow, 1e-10)))
        return mfi

    def _calc_roc(self, closes: list[float]) -> tuple[float, float, float]:
        """Rate of Change at 1d, 7d, 30d."""
        roc_1d = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
        roc_7d = (closes[-1] - closes[-8]) / closes[-8] * 100 if len(closes) >= 8 else 0
        roc_30d = (closes[-1] - closes[-31]) / closes[-31] * 100 if len(closes) >= 31 else 0
        return roc_1d, roc_7d, roc_30d

    def _calc_stoch_rsi(self, closes: list[float], period: int = 14) -> float:
        """Stochastic RSI (0-1 scale)."""
        # Need enough data to compute RSI for at least `period` windows
        if len(closes) < period * 2:
            return 0.5
        # Compute RSI for a rolling window to get a series
        rsi_series = []
        for end in range(period + 1, len(closes) + 1):
            window = closes[max(0, end - period * 2):end]
            rsi_val = self._calc_rsi(window, period)
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

    def _calc_squeeze(self, highs: list[float], lows: list[float],
                      closes: list[float], bb_period: int = 20,
                      bb_std: float = 2.0) -> tuple[bool, float]:
        """BB/Keltner Channel squeeze detection."""
        if len(closes) < bb_period:
            return False, 0.0
        # BB
        window = closes[-bb_period:]
        sma_20 = sum(window) / bb_period
        variance = sum((x - sma_20) ** 2 for x in window) / bb_period
        sd = math.sqrt(variance)
        bb_upper = sma_20 + bb_std * sd
        bb_lower = sma_20 - bb_std * sd
        # Keltner Channel using ATR(20)
        atr_20 = self._calc_atr(highs, lows, closes, bb_period)
        kc_upper = sma_20 + 1.5 * atr_20
        kc_lower = sma_20 - 1.5 * atr_20
        squeeze_on = (bb_lower > kc_lower) and (bb_upper < kc_upper)
        # Momentum: distance of close from SMA, normalized by ATR
        squeeze_momentum = (closes[-1] - sma_20) / atr_20 if atr_20 > 0 else 0.0
        return squeeze_on, squeeze_momentum

    def _calc_zscores(self, closes: list[float], highs: list[float],
                      lows: list[float], rsi_period: int = 14,
                      macd_fast: int = 12, macd_slow: int = 26,
                      macd_signal: int = 9, bb_period: int = 20,
                      bb_std: float = 2.0) -> dict[str, float]:
        """Z-scores for RSI, MACD histogram, BB position over 50-period rolling window."""
        min_needed = 50
        if len(closes) < min_needed:
            return {"rsi_zscore": 0.0, "macd_zscore": 0.0, "bb_zscore": 0.0}

        # Compute rolling RSI, MACD hist, BB position for last 50 periods
        rsi_vals = []
        macd_hist_vals = []
        bb_pos_vals = []

        # We need enough history before each window, so start from a safe offset
        start = max(macd_slow + macd_signal, rsi_period, bb_period)
        if len(closes) < start + min_needed:
            # Not enough data for full rolling computation — compute what we can
            # but ensure at least 50 data points
            start = max(0, len(closes) - min_needed)

        for end in range(len(closes) - min_needed, len(closes)):
            window = closes[:end + 1]
            if len(window) < max(rsi_period + 1, macd_slow + 1, bb_period):
                continue

            rsi_vals.append(self._calc_rsi(window, rsi_period))

            ema_f = self._calc_ema(window, macd_fast)
            ema_s = self._calc_ema(window, macd_slow)
            _, _, hist = self._calc_macd(ema_f, ema_s, macd_signal, macd_fast, macd_slow)
            macd_hist_vals.append(hist)

            bb = self._calc_bollinger(window, bb_period, bb_std)
            bb_pos_vals.append(bb["position"])

        def _zscore(values):
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

    def _calc_pivots(self, high, low, close):
        pivot = (high + low + close) / 3.0
        return {
            "pivot": pivot, "r1": 2 * pivot - low, "r2": pivot + (high - low),
            "s1": 2 * pivot - high, "s2": pivot - (high - low),
        }
