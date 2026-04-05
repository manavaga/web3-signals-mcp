# agents/market.py
"""Market agent — Fear & Greed, price, volume, macro, order book.

Data sources: CoinGecko, Alternative.me F&G, Binance, yfinance.
"""
from __future__ import annotations
import json
import logging
import time
from urllib.request import urlopen, Request
from urllib.error import URLError
from typing import Any

import requests
import yfinance as yf

from agents.base import BaseAgent

logger = logging.getLogger(__name__)

# Simple in-memory cache for macro data (30-minute TTL)
_macro_cache: dict = {"data": None, "timestamp": 0}
_stablecoin_cache: dict = {"data": None, "timestamp": 0}
CACHE_TTL = 1800  # 30 minutes


class MarketAgent(BaseAgent):
    def __init__(self, config: dict, symbols: dict[str, str], coingecko_ids: dict[str, str], storage=None):
        super().__init__("market_agent")
        self.config = config
        self.symbols = symbols
        self.coingecko_ids = coingecko_ids
        self.storage = storage

    def empty_data(self) -> dict[str, Any]:
        return {asset: {} for asset in self.symbols}

    def collect(self) -> tuple[dict[str, Any], list[str]]:
        results = {}
        errors = []

        # Fetch Fear & Greed (global, once)
        fg_value = 50
        try:
            data = self._fetch_json("https://api.alternative.me/fng/?limit=1&format=json")
            if data and "data" in data:
                fg_value = int(data["data"][0]["value"])
        except Exception as e:
            errors.append(f"F&G: {e}")

        # Fetch macro data (S&P, DXY, NASDAQ, VIX) with caching
        macro = {}
        try:
            macro = self._fetch_macro_cached()
        except Exception as e:
            errors.append(f"Macro: {e}")

        # Compute macro_status from thresholds
        macro_status = self._compute_macro_status(macro)

        # Fetch BTC dominance for breadth
        btc_dominance = self._fetch_btc_dominance()
        breadth_status = self._compute_breadth_status(btc_dominance)

        # Fetch stablecoin supply data
        stablecoin = {}
        try:
            stablecoin = self._fetch_stablecoin_supply()
        except Exception as e:
            errors.append(f"Stablecoin: {e}")

        for asset, symbol in self.symbols.items():
            try:
                result = {
                    "fear_greed": fg_value,
                    "macro_status": macro_status,
                    "sp500_change": macro.get("sp500_change", 0.0),
                    "dxy_change": macro.get("dxy_change", 0.0),
                    "nasdaq_change": macro.get("nasdaq_change", 0.0),
                    "vix_roc": macro.get("vix_roc", 0.0),
                    "btc_dominance": btc_dominance,
                    "breadth_status": breadth_status,
                    "stablecoin_supply_total": stablecoin.get("stablecoin_supply_total", 0),
                    "stablecoin_supply_change_7d": stablecoin.get("stablecoin_supply_change_7d", 0.0),
                }

                # Volume from Binance klines
                try:
                    klines = self._fetch_json(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit=8")
                    if klines and len(klines) >= 2:
                        volumes = [float(k[5]) for k in klines]
                        avg_7d = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else volumes[0]
                        result["volume_ratio"] = volumes[-1] / avg_7d if avg_7d > 0 else 1.0
                    else:
                        result["volume_ratio"] = 1.0
                except Exception as e:
                    errors.append(f"{asset} volume: {e}")
                    result["volume_ratio"] = 1.0

                # Order book depth
                try:
                    depth = self._fetch_json(f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=20")
                    if depth:
                        bid_vol = sum(float(b[1]) for b in depth.get("bids", []))
                        ask_vol = sum(float(a[1]) for a in depth.get("asks", []))
                        result["order_book_imbalance"] = bid_vol / ask_vol if ask_vol > 0 else 1.0
                    else:
                        result["order_book_imbalance"] = 1.0
                except Exception as e:
                    errors.append(f"{asset} depth: {e}")
                    result["order_book_imbalance"] = 1.0

                results[asset] = result
            except Exception as e:
                logger.error(f"Market agent error for {asset}: {e}")
                errors.append(f"{asset}: {e}")
                results[asset] = {}

        return results, errors

    def _fetch_macro_cached(self) -> dict:
        """Fetch macro data (S&P, DXY, NASDAQ, VIX) with 30-min caching."""
        now = time.time()
        if _macro_cache["data"] and (now - _macro_cache["timestamp"]) < CACHE_TTL:
            return _macro_cache["data"]

        macro = {}

        # Fetch equity/currency indices
        for ticker, key in [("SPY", "sp500_change"), ("DX-Y.NYB", "dxy_change"), ("QQQ", "nasdaq_change")]:
            try:
                data = yf.download(ticker, period="5d", interval="1d", progress=False)
                if len(data) >= 2:
                    close = data["Close"]
                    macro[key] = float((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100)
                else:
                    macro[key] = 0.0
            except Exception as e:
                logger.warning(f"Failed to fetch {ticker}: {e}")
                macro[key] = 0.0

        # Fetch VIX and compute rate of change
        try:
            vix_data = yf.download("^VIX", period="5d", interval="1d", progress=False)
            if len(vix_data) >= 2:
                vix_close = vix_data["Close"]
                macro["vix"] = float(vix_close.iloc[-1])
                macro["vix_roc"] = float((vix_close.iloc[-1] - vix_close.iloc[-2]) / vix_close.iloc[-2] * 100)
            else:
                vix_price = yf.Ticker("^VIX").fast_info.get("lastPrice", 20)
                macro["vix"] = float(vix_price)
                macro["vix_roc"] = 0.0
        except Exception as e:
            logger.warning(f"Failed to fetch VIX: {e}")
            macro["vix"] = 20.0
            macro["vix_roc"] = 0.0

        _macro_cache["data"] = macro
        _macro_cache["timestamp"] = now
        return macro

    def _compute_macro_status(self, macro: dict) -> str:
        """Compute macro risk status from VIX and S&P thresholds."""
        vix = macro.get("vix", 20.0)
        sp500_change = macro.get("sp500_change", 0.0)
        cfg = self.config

        vix_risk_off = cfg.get("macro_vix_risk_off", 25)
        vix_risk_on = cfg.get("macro_vix_risk_on", 18)
        sp_risk_off = cfg.get("macro_sp_risk_off_pct", -1.5)
        sp_risk_on = cfg.get("macro_sp_risk_on_pct", 0.5)

        if vix > vix_risk_off or sp500_change < sp_risk_off:
            return "strong_risk_off"
        elif vix < vix_risk_on and sp500_change > sp_risk_on:
            return "strong_risk_on"
        elif sp500_change > 0:
            return "risk_on"
        elif sp500_change < 0:
            return "risk_off"
        else:
            return "neutral"

    def _fetch_btc_dominance(self) -> float:
        """Fetch BTC market cap dominance from CoinGecko."""
        try:
            resp = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
            data = resp.json()["data"]
            return float(data["market_cap_percentage"]["btc"])
        except Exception as e:
            logger.warning(f"Failed to fetch BTC dominance: {e}")
            return 50.0

    def _compute_breadth_status(self, btc_dominance: float) -> str:
        """Compute breadth from BTC dominance.

        High BTC dominance (>60%) means altcoins are losing ground = 'loser'.
        Low BTC dominance (<40%) means altcoins gaining = 'gainer'.
        """
        if btc_dominance > 60:
            return "loser"
        elif btc_dominance < 40:
            return "gainer"
        else:
            return "neutral"

    def _fetch_stablecoin_supply(self) -> dict:
        """Fetch total stablecoin supply and 7-day change from DefiLlama (cached 30 min)."""
        now = time.time()
        if _stablecoin_cache["data"] and (now - _stablecoin_cache["timestamp"]) < CACHE_TTL:
            return _stablecoin_cache["data"]

        try:
            resp = requests.get(
                "https://stablecoins.llama.fi/stablecoins?includePrices=false",
                timeout=10,
            )
            data = resp.json()
            # Sum top stablecoins by circulating supply
            top_names = {"USDT", "USDC", "DAI", "BUSD", "TUSD"}
            total = 0.0
            for asset in data.get("peggedAssets", []):
                name = asset.get("symbol", "")
                if name in top_names:
                    total += asset.get("circulating", {}).get("peggedUSD", 0)

            # Compute 7d change from stored previous value
            change_pct = 0.0
            if self.storage:
                prev_supply = self.storage.load_kv("market", "stablecoin_supply")
                if prev_supply and total > 0:
                    prev = float(prev_supply)
                    change_pct = ((total - prev) / prev) * 100
                if total > 0:
                    self.storage.save_kv("market", "stablecoin_supply", str(total))

            result = {"stablecoin_supply_total": total, "stablecoin_supply_change_7d": change_pct}
            _stablecoin_cache["data"] = result
            _stablecoin_cache["timestamp"] = now
            return result
        except Exception:
            return {"stablecoin_supply_total": 0, "stablecoin_supply_change_7d": 0.0}

    def _fetch_json(self, url: str) -> Any:
        req = Request(url, headers={"User-Agent": "web3-signals/1.0"})
        resp = urlopen(req, timeout=15)
        return json.loads(resp.read())
