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
        limit = self.config.get("binance_kline_limit", 50)

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
                    "ma7": ma7,
                    "ma30": ma30,
                    "volume_ratio": vol_profile["ratio"],
                    "volume_status": vol_profile["status"],
                    "pivot": pivots["pivot"],
                    "r1": pivots["r1"],
                    "r2": pivots["r2"],
                    "s1": pivots["s1"],
                    "s2": pivots["s2"],
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

    def _calc_pivots(self, high, low, close):
        pivot = (high + low + close) / 3.0
        return {
            "pivot": pivot, "r1": 2 * pivot - low, "r2": pivot + (high - low),
            "s1": 2 * pivot - high, "s2": pivot - (high - low),
        }
