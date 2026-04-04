# agents/market.py
"""Market agent — Fear & Greed, price, volume, macro, order book.

Data sources: CoinGecko, Alternative.me F&G, Binance, yfinance.
"""
from __future__ import annotations
import json
import logging
from urllib.request import urlopen, Request
from urllib.error import URLError
from typing import Any
from agents.base import BaseAgent

logger = logging.getLogger(__name__)


class MarketAgent(BaseAgent):
    def __init__(self, config: dict, symbols: dict[str, str], coingecko_ids: dict[str, str]):
        super().__init__("market_agent")
        self.config = config
        self.symbols = symbols
        self.coingecko_ids = coingecko_ids

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

        # Fetch macro (VIX, S&P, DXY) — optional, may fail
        macro_status = "unknown"
        try:
            macro_status = self._fetch_macro_status()
        except Exception as e:
            errors.append(f"Macro: {e}")

        for asset, symbol in self.symbols.items():
            try:
                result = {"fear_greed": fg_value, "macro_status": macro_status}

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

                # Breadth (simplified — use CoinGecko trending)
                result["breadth_status"] = "neutral"

                results[asset] = result
            except Exception as e:
                logger.error(f"Market agent error for {asset}: {e}")
                errors.append(f"{asset}: {e}")
                results[asset] = {}

        return results, errors

    def _fetch_macro_status(self) -> str:
        """Fetch VIX, S&P 500, DXY from yfinance. Returns risk status."""
        try:
            import yfinance as yf
            vix = yf.Ticker("^VIX").fast_info.get("lastPrice", 20)
            cfg = self.config
            if vix > cfg.get("macro_vix_risk_off", 25):
                return "risk_off"
            elif vix < cfg.get("macro_vix_risk_on", 18):
                return "risk_on"
            return "neutral"
        except Exception:
            return "unknown"

    def _fetch_json(self, url: str) -> Any:
        req = Request(url, headers={"User-Agent": "web3-signals/1.0"})
        resp = urlopen(req, timeout=15)
        return json.loads(resp.read())
