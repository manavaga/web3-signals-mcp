from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.request import Request, urlopen

from shared.base_agent import BaseAgent
from shared.profile_loader import load_profile, get_assets, get_threshold, is_source_enabled

logger = logging.getLogger(__name__)


class ExchangeFlowAgent(BaseAgent):
    """
    Exchange flow intelligence engine using free APIs.

    Layer 1: Binance Order Book Depth — bid/ask imbalance scoring
    Layer 2: Binance 24h Ticker — volume & trade count metrics
    Layer 3: CoinGecko Market Chart — 7-day volume momentum

    All free, no API keys required.
    """

    def __init__(self, profile_path: str | None = None, db_path: str = "signals.db") -> None:
        if profile_path is None:
            profile_path = str(Path(__file__).resolve().parent / "profiles" / "default.yaml")
        self.profile = load_profile(Path(profile_path))
        self.assets = get_assets(self.profile)
        self.timeout = int(self.profile.get("http_timeout_sec", 20))

        # Source configs
        self._binance_depth_cfg = self.profile.get("sources", {}).get("binance_depth", {})
        self._binance_ticker_cfg = self.profile.get("sources", {}).get("binance_ticker", {})
        self._coingecko_cfg = self.profile.get("sources", {}).get("coingecko_chart", {})

        # Mappings
        self._binance_map: Dict[str, str] = self.profile.get("binance_symbol_map", {})
        self._coingecko_map: Dict[str, str] = self.profile.get("coingecko_id_map", {})

        # Scoring weights
        scoring = self.profile.get("scoring", {})
        self._ob_weight = float(scoring.get("order_book_weight", 0.40))
        self._vm_weight = float(scoring.get("volume_momentum_weight", 0.35))
        self._ti_weight = float(scoring.get("trade_intensity_weight", 0.25))

        # Thresholds
        thresholds = self.profile.get("thresholds", {})
        self._ba_strong_bull = float(thresholds.get("bid_ask_strong_bullish", 2.0))
        self._ba_bull = float(thresholds.get("bid_ask_bullish", 1.5))
        self._ba_mild_bull = float(thresholds.get("bid_ask_mild_bullish", 1.2))
        self._ba_mild_bear = float(thresholds.get("bid_ask_mild_bearish", 0.8))
        self._ba_bear = float(thresholds.get("bid_ask_bearish", 0.5))
        self._vol_spike = float(thresholds.get("volume_spike_pct", 100))
        self._vol_high = float(thresholds.get("volume_high_pct", 50))
        self._vol_declining = float(thresholds.get("volume_declining_pct", -30))
        self._vol_drying = float(thresholds.get("volume_drying_pct", -50))
        self._flow_bull = float(thresholds.get("flow_bullish_threshold", 60))
        self._flow_bear = float(thresholds.get("flow_bearish_threshold", 40))

        super().__init__(
            agent_name="exchange_flow_agent",
            profile_name=self.profile.get("name", "exchange_flow_default"),
        )

    # ------------------------------------------------------------------
    # BaseAgent interface
    # ------------------------------------------------------------------

    def empty_data(self) -> Dict[str, Any]:
        return {
            "by_asset": {sym: {} for sym in self.assets},
            "summary": {
                "bullish_flow_assets": [],
                "bearish_flow_assets": [],
                "neutral_flow_assets": [],
                "volume_spike_assets": [],
            },
            "sources_used": [],
        }

    def collect(self) -> Tuple[Dict[str, Any], List[str]]:
        data = self.empty_data()
        errors: List[str] = []

        # ----- Collect per-asset data from Binance (depth + ticker) -----
        sources = self.profile.get("sources", {})
        depth_enabled = sources.get("binance_depth", {}).get("enabled", False)
        ticker_enabled = sources.get("binance_ticker", {}).get("enabled", False)
        coingecko_enabled = sources.get("coingecko_chart", {}).get("enabled", False)

        # Store ticker data for trade intensity baseline
        all_trade_counts: List[int] = []
        ticker_data: Dict[str, Dict[str, Any]] = {}

        # --- Phase 1: Binance depth + ticker (fast, 0.1s rate limit) ---
        for asset in self.assets:
            symbol = self._binance_map.get(asset, f"{asset}USDT")

            # Order book depth
            if depth_enabled:
                try:
                    ob = self._fetch_order_book(symbol)
                    data["by_asset"][asset].update(ob)
                    if "binance_depth" not in data["sources_used"]:
                        data["sources_used"].append("binance_depth")
                except Exception as exc:
                    errors.append(f"binance_depth/{asset}: {exc}")
                    logger.warning("binance_depth/%s failed: %s", asset, exc)
                time.sleep(float(self._binance_depth_cfg.get("rate_limit_sec", 0.1)))

            # 24h ticker
            if ticker_enabled:
                try:
                    tk = self._fetch_ticker(symbol)
                    ticker_data[asset] = tk
                    data["by_asset"][asset].update({
                        "volume_24h": tk["volume_24h"],
                        "trade_count_24h": tk["trade_count"],
                    })
                    all_trade_counts.append(tk["trade_count"])
                    if "binance_ticker" not in data["sources_used"]:
                        data["sources_used"].append("binance_ticker")
                except Exception as exc:
                    errors.append(f"binance_ticker/{asset}: {exc}")
                    logger.warning("binance_ticker/%s failed: %s", asset, exc)
                time.sleep(float(self._binance_ticker_cfg.get("rate_limit_sec", 0.1)))

        # --- Phase 2: CoinGecko volume history (slow, 1.5s rate limit) ---
        if coingecko_enabled:
            for asset in self.assets:
                cg_id = self._coingecko_map.get(asset)
                if not cg_id:
                    errors.append(f"coingecko_chart/{asset}: no CoinGecko ID mapped")
                    continue
                try:
                    vol = self._fetch_volume_history(cg_id)
                    data["by_asset"][asset].update(vol)
                    if "coingecko_chart" not in data["sources_used"]:
                        data["sources_used"].append("coingecko_chart")
                except Exception as exc:
                    errors.append(f"coingecko_chart/{asset}: {exc}")
                    logger.warning("coingecko_chart/%s failed: %s", asset, exc)
                time.sleep(float(self._coingecko_cfg.get("rate_limit_sec", 1.5)))

        # --- Phase 3: Compute scores ---
        avg_trade_count = (
            sum(all_trade_counts) / len(all_trade_counts)
            if all_trade_counts else 1
        )

        for asset in self.assets:
            ad = data["by_asset"][asset]
            if not ad:
                continue

            # Order book score
            bar = ad.get("bid_ask_ratio")
            ob_score = self._score_order_book(bar) if bar is not None else 50.0
            ad["order_book_score"] = round(ob_score, 1)

            # Volume momentum score
            vcp = ad.get("volume_change_pct")
            vm_score = self._score_volume_momentum(vcp) if vcp is not None else 50.0
            ad["volume_momentum_score"] = round(vm_score, 1)

            # Trade intensity score
            tc = ad.get("trade_count_24h")
            ti_score = self._score_trade_intensity(tc, avg_trade_count) if tc is not None else 50.0
            ad["trade_intensity_score"] = round(ti_score, 1)

            # Composite
            composite = (
                self._ob_weight * ob_score
                + self._vm_weight * vm_score
                + self._ti_weight * ti_score
            )
            ad["exchange_flow_score"] = round(composite, 1)

            # Status label
            ad["exchange_flow_status"] = self._flow_status(composite)

            # Classify into summary buckets
            if composite > self._flow_bull:
                data["summary"]["bullish_flow_assets"].append(asset)
            elif composite < self._flow_bear:
                data["summary"]["bearish_flow_assets"].append(asset)
            else:
                data["summary"]["neutral_flow_assets"].append(asset)

            if vcp is not None and vcp > self._vol_spike:
                data["summary"]["volume_spike_assets"].append(asset)

        return data, errors

    # ------------------------------------------------------------------
    # Data fetching helpers
    # ------------------------------------------------------------------

    def _http_get(self, url: str) -> Any:
        """Fetch JSON from URL using urllib (no requests library)."""
        req = Request(url, headers={"User-Agent": "Web3Signals/1.0"})
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _fetch_order_book(self, symbol: str) -> Dict[str, Any]:
        """Fetch Binance order book depth and compute bid/ask metrics."""
        base = self._binance_depth_cfg.get("base_url", "https://api.binance.com/api/v3")
        limit = int(self._binance_depth_cfg.get("order_book_limit", 20))
        url = f"{base}/depth?symbol={symbol}&limit={limit}"
        raw = self._http_get(url)

        bid_volume = sum(float(level[1]) for level in raw.get("bids", []))
        ask_volume = sum(float(level[1]) for level in raw.get("asks", []))
        bid_ask_ratio = bid_volume / ask_volume if ask_volume > 0 else 1.0

        return {
            "bid_volume": round(bid_volume, 4),
            "ask_volume": round(ask_volume, 4),
            "bid_ask_ratio": round(bid_ask_ratio, 4),
        }

    def _fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        """Fetch Binance 24h ticker stats."""
        base = self._binance_ticker_cfg.get("base_url", "https://api.binance.com/api/v3")
        url = f"{base}/ticker/24hr?symbol={symbol}"
        raw = self._http_get(url)

        return {
            "volume_24h": float(raw.get("quoteVolume", 0)),
            "volume_raw": float(raw.get("volume", 0)),
            "trade_count": int(raw.get("count", 0)),
            "taker_buy_ratio": (
                float(raw.get("volume", 1)) and
                float(raw.get("quoteVolume", 0)) / float(raw.get("volume", 1))
                if float(raw.get("volume", 0)) > 0 else 0.0
            ),
        }

    def _fetch_volume_history(self, cg_id: str) -> Dict[str, Any]:
        """Fetch 7-day volume from CoinGecko and compute momentum."""
        base = self._coingecko_cfg.get("base_url", "https://api.coingecko.com/api/v3")
        days = int(self._coingecko_cfg.get("volume_days", 7))
        url = f"{base}/coins/{cg_id}/market_chart?vs_currency=usd&days={days}"
        raw = self._http_get(url)

        volumes = raw.get("total_volumes", [])
        if not volumes or len(volumes) < 2:
            return {"volume_7d_avg": 0.0, "volume_change_pct": 0.0}

        # Each entry is [timestamp_ms, volume_usd]
        vol_values = [v[1] for v in volumes]

        # Latest day volume (last entry) vs average of all
        latest_vol = vol_values[-1]
        avg_vol = sum(vol_values) / len(vol_values) if vol_values else 1.0

        change_pct = ((latest_vol / avg_vol) - 1.0) * 100 if avg_vol > 0 else 0.0

        return {
            "volume_7d_avg": round(avg_vol, 2),
            "volume_change_pct": round(change_pct, 2),
        }

    # ------------------------------------------------------------------
    # Scoring functions
    # ------------------------------------------------------------------

    def _score_order_book(self, bid_ask_ratio: float) -> float:
        """
        Score order book imbalance (0-100).
        Uses linear interpolation between threshold points.
        """
        # Define interpolation breakpoints: (ratio, score)
        breakpoints = [
            (0.0, 10.0),
            (self._ba_bear, 25.0),       # 0.5 -> 25
            (self._ba_mild_bear, 40.0),  # 0.8 -> 40
            (1.0, 50.0),                 # 1.0 -> 50 (neutral)
            (self._ba_mild_bull, 60.0),  # 1.2 -> 60
            (self._ba_bull, 70.0),       # 1.5 -> 70
            (self._ba_strong_bull, 85.0),# 2.0 -> 85
            (3.0, 95.0),                 # 3.0 -> 95 (cap)
        ]
        return self._interpolate(bid_ask_ratio, breakpoints)

    def _score_volume_momentum(self, change_pct: float) -> float:
        """
        Score volume momentum (0-100).
        Uses linear interpolation between threshold points.
        """
        breakpoints = [
            (-100.0, 10.0),
            (self._vol_drying, 20.0),     # -50% -> 20
            (self._vol_declining, 35.0),  # -30% -> 35
            (0.0, 50.0),                  # 0% -> 50 (neutral)
            (self._vol_high, 65.0),       # +50% -> 65
            (self._vol_spike, 80.0),      # +100% -> 80
            (200.0, 90.0),                # +200% -> 90 (cap)
        ]
        return self._interpolate(change_pct, breakpoints)

    def _score_trade_intensity(self, trade_count: int, avg_count: float) -> float:
        """
        Score trade intensity relative to the average across all assets.
        Higher trade count relative to average → more bullish activity.
        """
        if avg_count <= 0:
            return 50.0
        ratio = trade_count / avg_count
        breakpoints = [
            (0.0, 20.0),
            (0.5, 35.0),
            (0.8, 45.0),
            (1.0, 50.0),   # average = neutral
            (1.3, 60.0),
            (1.8, 70.0),
            (2.5, 80.0),
            (4.0, 90.0),
        ]
        return self._interpolate(ratio, breakpoints)

    @staticmethod
    def _interpolate(value: float, breakpoints: List[tuple]) -> float:
        """Linear interpolation between sorted breakpoints [(x, y), ...]."""
        if value <= breakpoints[0][0]:
            return breakpoints[0][1]
        if value >= breakpoints[-1][0]:
            return breakpoints[-1][1]
        for i in range(len(breakpoints) - 1):
            x0, y0 = breakpoints[i]
            x1, y1 = breakpoints[i + 1]
            if x0 <= value <= x1:
                t = (value - x0) / (x1 - x0) if x1 != x0 else 0.0
                return y0 + t * (y1 - y0)
        return 50.0  # fallback

    def _flow_status(self, score: float) -> str:
        """Map composite score to a human-readable flow status."""
        if score >= 75:
            return "strong_inflow"
        elif score >= self._flow_bull:
            return "moderate_inflow"
        elif score <= 25:
            return "strong_outflow"
        elif score <= self._flow_bear:
            return "moderate_outflow"
        else:
            return "neutral"
