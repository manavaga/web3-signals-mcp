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
    Calculates TA indicators (RSI, MACD, MA, Bollinger Bands, ATR, Volume,
    Pivot Points) for top crypto assets across daily and 4h timeframes.
    Everything is driven by profiles/default.yaml — no hardcoded values.

    Source: Binance spot klines (free, no API key).
    Signal: 30D trend bullish + 7D trend bullish → technical_condition = True.
    Also produces a numeric technical_score (0-100).
    """

    def __init__(self, profile_path: str | None = None) -> None:
        default = Path(__file__).resolve().parent / "profiles" / "default.yaml"
        self.profile = load_profile(Path(profile_path) if profile_path else default)
        self.assets = get_assets(self.profile)
        self.timeout = int(self.profile.get("http_timeout_sec", 20))
        self.symbol_map: Dict[str, str] = self.profile.get("binance_symbol_map", {})
        self.binance_cfg = self.profile.get("binance", {})
        self.base_url = self.binance_cfg.get("base_url", "https://api.binance.com/api/v3")

        # New config sections
        self.bb_cfg = self.profile.get("bollinger", {})
        self.vol_cfg = self.profile.get("volume", {})
        self.atr_cfg = self.profile.get("atr", {})
        self.candles_4h_cfg = self.profile.get("candles_4h", {})
        self.scoring_cfg = self.profile.get("scoring", {})

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
            # Bollinger Bands
            "bb_upper": None,
            "bb_lower": None,
            "bb_middle": None,
            "bb_bandwidth": None,
            "bb_position": None,
            "bb_squeeze": None,
            # Volume
            "volume_ma_ratio": None,
            "volume_status": "unknown",
            # Pivot Points
            "pivot": None,
            "resistance_1": None,
            "resistance_2": None,
            "support_1": None,
            "support_2": None,
            # 4h timeframe
            "rsi_14_4h": None,
            "macd_line_4h": None,
            "macd_signal_4h": None,
            "macd_histogram_4h": None,
            # ATR
            "atr_14": None,
            "atr_pct": None,
            # Composite score
            "technical_score": None,
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

        # Bollinger config
        bb_period = int(self.bb_cfg.get("period", 20))
        bb_std_dev = float(self.bb_cfg.get("std_dev", 2))
        bb_squeeze_threshold = float(self.bb_cfg.get("squeeze_threshold", 0.04))

        # Volume config
        vol_ma_period = int(self.vol_cfg.get("ma_period", 20))
        vol_spike = float(self.vol_cfg.get("spike_threshold", 2.0))
        vol_elevated = float(self.vol_cfg.get("elevated_threshold", 1.5))

        # ATR config
        atr_period = int(self.atr_cfg.get("period", 14))

        # 4h candles config
        candles_4h_enabled = bool(self.candles_4h_cfg.get("enabled", True))
        candles_4h_interval = self.candles_4h_cfg.get("interval", "4h")
        candles_4h_limit = int(self.candles_4h_cfg.get("limit", 50))

        # Scoring weights
        scoring_weights = self.scoring_cfg.get("weights", {})
        w_rsi = float(scoring_weights.get("rsi", 0.25))
        w_macd = float(scoring_weights.get("macd", 0.25))
        w_bb = float(scoring_weights.get("bollinger", 0.20))
        w_trend = float(scoring_weights.get("trend", 0.30))
        volume_spike_bonus = float(self.scoring_cfg.get("volume_spike_bonus", 10))

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

            # --- Fetch daily klines (OHLCV) ---
            try:
                candles = self._fetch_klines_full(binance_sym, interval, candle_limit)
                closes = [c["close"] for c in candles]
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

            # --- Bollinger Bands ---
            if len(closes) >= bb_period:
                bb_mid, bb_up, bb_low, bb_bw, bb_pos, bb_sq = self._calc_bollinger(
                    closes, bb_period, bb_std_dev, bb_squeeze_threshold
                )
                asset["bb_middle"] = round(bb_mid, 6)
                asset["bb_upper"] = round(bb_up, 6)
                asset["bb_lower"] = round(bb_low, 6)
                asset["bb_bandwidth"] = round(bb_bw, 6)
                asset["bb_position"] = round(bb_pos, 4)
                asset["bb_squeeze"] = bb_sq

            # --- Volume Profile ---
            volumes = [c["volume"] for c in candles]
            if len(volumes) >= vol_ma_period:
                vol_ratio, vol_status = self._calc_volume_profile(
                    volumes, vol_ma_period, vol_spike, vol_elevated
                )
                asset["volume_ma_ratio"] = round(vol_ratio, 4)
                asset["volume_status"] = vol_status

            # --- Pivot Points (from latest daily candle) ---
            latest_candle = candles[-1]
            pivot, r1, r2, s1, s2 = self._calc_pivot_points(
                latest_candle["high"], latest_candle["low"], latest_candle["close"]
            )
            asset["pivot"] = round(pivot, 6)
            asset["resistance_1"] = round(r1, 6)
            asset["resistance_2"] = round(r2, 6)
            asset["support_1"] = round(s1, 6)
            asset["support_2"] = round(s2, 6)

            # --- ATR (14-period) ---
            if len(candles) > atr_period:
                atr_val = self._calc_atr(candles, atr_period)
                if atr_val is not None:
                    asset["atr_14"] = round(atr_val, 6)
                    asset["atr_pct"] = round(atr_val / price * 100, 4) if price > 0 else None

            # --- 4h Candles (RSI + MACD) ---
            if candles_4h_enabled:
                try:
                    candles_4h = self._fetch_klines_full(
                        binance_sym, candles_4h_interval, candles_4h_limit
                    )
                    closes_4h = [c["close"] for c in candles_4h]

                    rsi_4h = self._calc_rsi(closes_4h, rsi_period)
                    if rsi_4h is not None:
                        asset["rsi_14_4h"] = round(rsi_4h, 2)

                    macd_4h, signal_4h, hist_4h = self._calc_macd(
                        closes_4h, macd_fast, macd_slow, macd_signal_period
                    )
                    if macd_4h is not None:
                        asset["macd_line_4h"] = round(macd_4h, 6)
                        asset["macd_signal_4h"] = round(signal_4h, 6)
                        asset["macd_histogram_4h"] = round(hist_4h, 6)
                except Exception as exc:
                    errors.append(f"{sym} 4h klines: {exc}")

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

            # --- Composite technical_score (0-100) ---
            asset["technical_score"] = self._calc_technical_score(
                rsi=rsi,
                macd_histogram=histogram if macd_line is not None else None,
                bb_position=asset["bb_position"],
                trend_7d=trend_7d,
                trend_30d=trend_30d,
                volume_status=asset["volume_status"],
                price=asset.get("price"),
                w_rsi=w_rsi,
                w_macd=w_macd,
                w_bb=w_bb,
                w_trend=w_trend,
                volume_spike_bonus=volume_spike_bonus,
                rsi_oversold=rsi_oversold,
                rsi_overbought=rsi_overbought,
            )

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
        """Fetch daily close prices from Binance spot klines (legacy compat)."""
        candles = self._fetch_klines_full(symbol, interval, limit)
        return [c["close"] for c in candles]

    def _fetch_klines_full(
        self, symbol: str, interval: str, limit: int
    ) -> List[Dict[str, float]]:
        """Fetch full OHLCV candle data from Binance spot klines."""
        ep = self.binance_cfg.get("klines_endpoint", "/klines")
        url = f"{self.base_url}{ep}?symbol={symbol}&interval={interval}&limit={limit}"
        raw = self._get_json(url)

        # Binance kline format: [open_time, open, high, low, close, volume, close_time, ...]
        candles: List[Dict[str, float]] = []
        for c in raw:
            candles.append({
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            })
        return candles

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

    @staticmethod
    def _calc_bollinger(
        closes: List[float],
        period: int,
        std_dev_mult: float,
        squeeze_threshold: float,
    ) -> Tuple[float, float, float, float, float, bool]:
        """
        Bollinger Bands (period-SMA +/- std_dev_mult * stdev).
        Returns: (middle, upper, lower, bandwidth, position, squeeze).
        """
        window = closes[-period:]
        middle = sum(window) / period
        variance = sum((x - middle) ** 2 for x in window) / period
        std_dev = math.sqrt(variance)

        upper = middle + std_dev_mult * std_dev
        lower = middle - std_dev_mult * std_dev

        # Bandwidth: (upper - lower) / middle
        bandwidth = (upper - lower) / middle if middle != 0 else 0.0

        # Position: where price sits within the bands (0 = lower, 1 = upper)
        band_range = upper - lower
        if band_range > 0:
            position = (closes[-1] - lower) / band_range
        else:
            position = 0.5

        # Squeeze detection
        squeeze = bandwidth < squeeze_threshold

        return middle, upper, lower, bandwidth, position, squeeze

    @staticmethod
    def _calc_volume_profile(
        volumes: List[float],
        ma_period: int,
        spike_threshold: float,
        elevated_threshold: float,
    ) -> Tuple[float, str]:
        """
        Volume MA ratio and status classification.
        Returns: (volume_ma_ratio, volume_status).
        """
        vol_ma = sum(volumes[-ma_period:]) / ma_period
        current_vol = volumes[-1]
        ratio = current_vol / vol_ma if vol_ma > 0 else 1.0

        if ratio >= spike_threshold:
            status = "spike"
        elif ratio >= elevated_threshold:
            status = "elevated"
        elif ratio < 0.5:
            status = "low"
        else:
            status = "normal"

        return ratio, status

    @staticmethod
    def _calc_pivot_points(
        high: float, low: float, close: float
    ) -> Tuple[float, float, float, float, float]:
        """
        Classic pivot point formula.
        Returns: (pivot, R1, R2, S1, S2).
        """
        pivot = (high + low + close) / 3.0
        r1 = 2.0 * pivot - low
        s1 = 2.0 * pivot - high
        r2 = pivot + (high - low)
        s2 = pivot - (high - low)
        return pivot, r1, r2, s1, s2

    @staticmethod
    def _calc_atr(candles: List[Dict[str, float]], period: int) -> Optional[float]:
        """
        Average True Range using Wilder's smoothing.
        candles must have 'high', 'low', 'close' keys.
        """
        if len(candles) < period + 1:
            return None

        # Calculate True Range series (skip first candle, need prev close)
        tr_values: List[float] = []
        for i in range(1, len(candles)):
            h = candles[i]["high"]
            l = candles[i]["low"]
            prev_c = candles[i - 1]["close"]
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            tr_values.append(tr)

        if len(tr_values) < period:
            return None

        # Initial ATR = simple average of first `period` TR values
        atr = sum(tr_values[:period]) / period

        # Wilder's smoothing for remaining
        for tr in tr_values[period:]:
            atr = (atr * (period - 1) + tr) / period

        return atr

    @staticmethod
    def _calc_technical_score(
        rsi: Optional[float],
        macd_histogram: Optional[float],
        bb_position: Optional[float],
        trend_7d: str,
        trend_30d: str,
        volume_status: str,
        price: Optional[float] = None,
        w_rsi: float = 0.25,
        w_macd: float = 0.25,
        w_bb: float = 0.20,
        w_trend: float = 0.30,
        volume_spike_bonus: float = 5.0,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
    ) -> Optional[float]:
        """
        Composite technical score (0-100).
        Higher = more bullish opportunity.
        """
        components: List[Tuple[float, float]] = []  # (score, weight)

        # --- RSI score (wider range: 10-90) ---
        if rsi is not None:
            if rsi <= rsi_oversold:
                # Deeply oversold = strong bullish signal
                # RSI 0→95, RSI 30→75
                rsi_score = 95.0 - (rsi / rsi_oversold) * 20.0
            elif rsi >= rsi_overbought:
                # Deeply overbought = strong bearish signal
                # RSI 70→25, RSI 100→5
                rsi_score = 25.0 - ((rsi - rsi_overbought) / (100.0 - rsi_overbought)) * 20.0
            else:
                # Middle range: RSI 30→65, RSI 50→50, RSI 70→35
                rsi_score = 65.0 - (rsi - rsi_oversold) / (rsi_overbought - rsi_oversold) * 30.0
            rsi_score = max(5.0, min(95.0, rsi_score))
            components.append((rsi_score, w_rsi))

        # --- MACD score (normalized by price for cross-asset comparability) ---
        if macd_histogram is not None:
            # MACD histogram is in absolute price units (e.g., -524 for BTC at $67K)
            # Normalize to percentage of price for cross-asset comparability
            if price is not None and price > 0:
                macd_pct = (macd_histogram / price) * 100  # as percentage
            else:
                macd_pct = macd_histogram  # fallback if no price

            # macd_pct typical range: -2% to +2% for crypto
            # Map: -2%→10, -1%→30, 0%→50, +1%→70, +2%→90
            if macd_pct > 0:
                intensity = min(macd_pct / 2.0, 1.0)  # 2% = full bullish
                macd_score = 50.0 + intensity * 40.0  # 50→90
            else:
                intensity = min(abs(macd_pct) / 2.0, 1.0)  # -2% = full bearish
                macd_score = 50.0 - intensity * 40.0  # 50→10
            macd_score = max(10.0, min(90.0, macd_score))
            components.append((macd_score, w_macd))

        # --- Bollinger Bands score (wider range: 10-90) ---
        if bb_position is not None:
            # Near lower band (position ~0) = buy opportunity = high score
            # Near upper band (position ~1) = sell territory = low score
            # Below lower band (<0) = extreme oversold = very high score
            # Above upper band (>1) = extreme overbought = very low score
            if bb_position < 0:
                bb_score = 90.0  # Below lower band
            elif bb_position > 1:
                bb_score = 10.0  # Above upper band
            else:
                bb_score = 85.0 - bb_position * 70.0  # 0→85, 0.5→50, 1.0→15
            bb_score = max(10.0, min(90.0, bb_score))
            components.append((bb_score, w_bb))

        # --- Trend score (wider range: 15-85) ---
        trend_score = 50.0
        above_7d = trend_7d == "bullish"
        above_30d = trend_30d == "bullish"
        below_7d = trend_7d == "bearish"
        below_30d = trend_30d == "bearish"

        if above_7d and above_30d:
            trend_score = 85.0  # Strong uptrend
        elif above_7d and not above_30d:
            trend_score = 60.0  # Early recovery
        elif below_7d and below_30d:
            trend_score = 15.0  # Strong downtrend
        elif below_30d and not below_7d:
            trend_score = 35.0  # Early decline
        components.append((trend_score, w_trend))

        if not components:
            return None

        # Weighted average
        total_weight = sum(w for _, w in components)
        if total_weight == 0:
            return None
        score = sum(s * w for s, w in components) / total_weight

        # Volume spike bonus
        if volume_status == "spike":
            score += volume_spike_bonus

        # Clamp to 0-100
        score = max(0.0, min(100.0, score))
        return round(score, 2)

    # ------------------------------------------------------------------ #
    # HTTP helper
    # ------------------------------------------------------------------ #

    def _get_json(self, url: str) -> Any:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
