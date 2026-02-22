from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from shared.base_agent import BaseAgent
from shared.profile_loader import load_profile, get_assets, get_threshold


class TechnicalAgent(BaseAgent):
    """
    Calculates TA indicators (RSI, MACD, MA) for top crypto assets.
    Everything is driven by profiles/default.yaml — no hardcoded values.

    Source: Binance spot klines (free, no API key).
    Signal: 30D trend bullish + 7D trend bullish → technical_condition = True.
    """

    def __init__(self, profile_path: str | None = None) -> None:
        default = Path(__file__).resolve().parent / "profiles" / "default.yaml"
        self.profile = load_profile(Path(profile_path) if profile_path else default)
        self.assets = get_assets(self.profile)
        self.timeout = int(self.profile.get("http_timeout_sec", 20))
        self.symbol_map: Dict[str, str] = self.profile.get("binance_symbol_map", {})
        self.binance_cfg = self.profile.get("binance", {})
        self.base_url = self.binance_cfg.get("base_url", "https://api.binance.com/api/v3")

        super().__init__(
            agent_name="technical_agent",
            profile_name=self.profile.get("name", "technical_default"),
        )

    def empty_data(self) -> Dict[str, Any]:
        return {
            "by_asset": {sym: self._empty_asset() for sym in self.assets},
            "summary": {
                "bullish_assets": [],
                "bearish_assets": [],
                "neutral_assets": [],
                "overbought_assets": [],
                "oversold_assets": [],
            },
        }

    @staticmethod
    def _empty_asset() -> Dict[str, Any]:
        return {
            "price": None,
            "rsi_14": None,
            "macd_line": None,
            "macd_signal": None,
            "macd_histogram": None,
            "ma_7d": None,
            "ma_30d": None,
            "price_vs_7d_ma": None,
            "price_vs_30d_ma": None,
            "trend_7d": "unknown",
            "trend_30d": "unknown",
            "rsi_status": "unknown",
            "macd_status": "unknown",
            "technical_condition": False,
        }

    def collect(self) -> Tuple[Dict[str, Any], List[str]]:
        data = self.empty_data()
        errors: List[str] = []

        # Thresholds from YAML
        rsi_period = int(get_threshold(self.profile, "thresholds", "rsi_period", default=14))
        rsi_bullish = float(get_threshold(self.profile, "thresholds", "rsi_bullish", default=50))
        rsi_overbought = float(get_threshold(self.profile, "thresholds", "rsi_overbought", default=70))
        rsi_oversold = float(get_threshold(self.profile, "thresholds", "rsi_oversold", default=30))
        ma_7d_period = int(get_threshold(self.profile, "thresholds", "ma_7d_period", default=7))
        ma_30d_period = int(get_threshold(self.profile, "thresholds", "ma_30d_period", default=30))
        macd_fast = int(get_threshold(self.profile, "thresholds", "macd_fast", default=12))
        macd_slow = int(get_threshold(self.profile, "thresholds", "macd_slow", default=26))
        macd_signal_period = int(get_threshold(self.profile, "thresholds", "macd_signal", default=9))

        # Trend rules from YAML
        trend_rules = self.profile.get("trend_rules", {})
        require_30d = bool(trend_rules.get("require_30d_bullish", True))
        require_7d = bool(trend_rules.get("require_7d_bullish", True))

        # Binance klines config
        interval = self.binance_cfg.get("interval", "1d")
        candle_limit = int(self.binance_cfg.get("candle_limit", 50))

        bullish, bearish, neutral = [], [], []
        overbought, oversold = [], []

        for sym in self.assets:
            binance_sym = self.symbol_map.get(sym)
            if not binance_sym:
                errors.append(f"{sym}: no Binance symbol mapping in profile")
                continue

            asset = data["by_asset"][sym]

            # --- Fetch klines ---
            try:
                closes = self._fetch_klines(binance_sym, interval, candle_limit)
                if len(closes) < macd_slow + macd_signal_period:
                    errors.append(f"{sym}: not enough candles ({len(closes)})")
                    continue
            except Exception as exc:
                errors.append(f"{sym} klines: {exc}")
                continue

            price = closes[-1]
            asset["price"] = round(price, 6)

            # --- RSI ---
            rsi = self._calc_rsi(closes, rsi_period)
            if rsi is not None:
                asset["rsi_14"] = round(rsi, 2)
                if rsi >= rsi_overbought:
                    asset["rsi_status"] = "overbought"
                    overbought.append(sym)
                elif rsi <= rsi_oversold:
                    asset["rsi_status"] = "oversold"
                    oversold.append(sym)
                elif rsi >= rsi_bullish:
                    asset["rsi_status"] = "bullish"
                else:
                    asset["rsi_status"] = "bearish"

            # --- Moving Averages ---
            if len(closes) >= ma_7d_period:
                ma7 = sum(closes[-ma_7d_period:]) / ma_7d_period
                asset["ma_7d"] = round(ma7, 6)
                asset["price_vs_7d_ma"] = round((price - ma7) / ma7 * 100, 2)

            if len(closes) >= ma_30d_period:
                ma30 = sum(closes[-ma_30d_period:]) / ma_30d_period
                asset["ma_30d"] = round(ma30, 6)
                asset["price_vs_30d_ma"] = round((price - ma30) / ma30 * 100, 2)

            # --- MACD ---
            macd_line, signal_line, histogram = self._calc_macd(
                closes, macd_fast, macd_slow, macd_signal_period
            )
            if macd_line is not None:
                asset["macd_line"] = round(macd_line, 6)
                asset["macd_signal"] = round(signal_line, 6)
                asset["macd_histogram"] = round(histogram, 6)
                asset["macd_status"] = "bullish" if macd_line > signal_line else "bearish"

            # --- Trend evaluation (from YAML rules) ---
            # 30D trend: price above 30D MA AND RSI > bullish threshold
            trend_30d = "unknown"
            if asset["ma_30d"] is not None and rsi is not None:
                if price > asset["ma_30d"] and rsi > rsi_bullish:
                    trend_30d = "bullish"
                elif price < asset["ma_30d"] and rsi < rsi_bullish:
                    trend_30d = "bearish"
                else:
                    trend_30d = "neutral"
            asset["trend_30d"] = trend_30d

            # 7D trend: price above 7D MA AND MACD line > signal line
            trend_7d = "unknown"
            if asset["ma_7d"] is not None and macd_line is not None:
                if price > asset["ma_7d"] and macd_line > signal_line:
                    trend_7d = "bullish"
                elif price < asset["ma_7d"] and macd_line < signal_line:
                    trend_7d = "bearish"
                else:
                    trend_7d = "neutral"
            asset["trend_7d"] = trend_7d

            # --- Technical condition ---
            cond_30d = (trend_30d == "bullish") if require_30d else True
            cond_7d = (trend_7d == "bullish") if require_7d else True
            asset["technical_condition"] = cond_30d and cond_7d

            # Classify for summary
            if asset["technical_condition"]:
                bullish.append(sym)
            elif trend_30d == "bearish" or trend_7d == "bearish":
                bearish.append(sym)
            else:
                neutral.append(sym)

        data["summary"] = {
            "bullish_assets": bullish,
            "bearish_assets": bearish,
            "neutral_assets": neutral,
            "overbought_assets": overbought,
            "oversold_assets": oversold,
        }

        return data, errors

    # ------------------------------------------------------------------ #
    # Binance klines fetch
    # ------------------------------------------------------------------ #

    def _fetch_klines(self, symbol: str, interval: str, limit: int) -> List[float]:
        """Fetch daily close prices from Binance spot klines."""
        ep = self.binance_cfg.get("klines_endpoint", "/klines")
        url = f"{self.base_url}{ep}?symbol={symbol}&interval={interval}&limit={limit}"
        raw = self._get_json(url)

        # Binance kline format: [open_time, open, high, low, close, volume, ...]
        closes: List[float] = []
        for candle in raw:
            closes.append(float(candle[4]))  # index 4 = close price
        return closes

    # ------------------------------------------------------------------ #
    # Technical indicators — pure Python, no pandas-ta needed
    # ------------------------------------------------------------------ #

    @staticmethod
    def _calc_rsi(closes: List[float], period: int) -> Optional[float]:
        """Relative Strength Index using Wilder's smoothing."""
        if len(closes) < period + 1:
            return None

        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

        # Initial average gain/loss over first `period` deltas
        gains = [d if d > 0 else 0 for d in deltas[:period]]
        losses = [-d if d < 0 else 0 for d in deltas[:period]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        # Wilder's smoothing for remaining deltas
        for d in deltas[period:]:
            gain = d if d > 0 else 0
            loss = -d if d < 0 else 0
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _calc_ema(values: List[float], period: int) -> List[float]:
        """Exponential Moving Average."""
        if len(values) < period:
            return []
        multiplier = 2.0 / (period + 1)
        ema = [sum(values[:period]) / period]  # SMA seed
        for val in values[period:]:
            ema.append((val - ema[-1]) * multiplier + ema[-1])
        return ema

    @classmethod
    def _calc_macd(
        cls, closes: List[float], fast: int, slow: int, signal_period: int
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """MACD line, signal line, histogram — latest values only."""
        if len(closes) < slow + signal_period:
            return None, None, None

        ema_fast = cls._calc_ema(closes, fast)
        ema_slow = cls._calc_ema(closes, slow)

        # Align fast EMA to same start as slow EMA
        offset = slow - fast
        if offset > len(ema_fast):
            return None, None, None
        aligned_fast = ema_fast[offset:]

        # MACD line = fast EMA - slow EMA
        min_len = min(len(aligned_fast), len(ema_slow))
        macd_line_series = [aligned_fast[i] - ema_slow[i] for i in range(min_len)]

        if len(macd_line_series) < signal_period:
            return None, None, None

        # Signal line = EMA of MACD line
        signal_series = cls._calc_ema(macd_line_series, signal_period)
        if not signal_series:
            return None, None, None

        macd_val = macd_line_series[-1]
        signal_val = signal_series[-1]
        histogram = macd_val - signal_val

        return macd_val, signal_val, histogram

    # ------------------------------------------------------------------ #
    # HTTP helper
    # ------------------------------------------------------------------ #

    def _get_json(self, url: str) -> Any:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
