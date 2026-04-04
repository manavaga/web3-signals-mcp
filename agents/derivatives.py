# agents/derivatives.py
"""Derivatives agent — funding, OI, L/S, liquidations, taker ratio.

Data source: Binance Futures API (free, no API key for public endpoints).
"""
from __future__ import annotations
import json
import logging
from urllib.request import urlopen, Request
from urllib.error import URLError
from typing import Any
from agents.base import BaseAgent

logger = logging.getLogger(__name__)

BINANCE_FUTURES = "https://fapi.binance.com"


class DerivativesAgent(BaseAgent):
    def __init__(self, config: dict, symbols: dict[str, str]):
        super().__init__("derivatives_agent")
        self.config = config
        self.symbols = symbols

    def empty_data(self) -> dict[str, Any]:
        return {asset: {} for asset in self.symbols}

    def collect(self) -> tuple[dict[str, Any], list[str]]:
        results = {}
        errors = []

        for asset, symbol in self.symbols.items():
            try:
                result = {}
                # L/S ratio
                try:
                    data = self._fetch_json(f"{BINANCE_FUTURES}/futures/data/globalLongShortAccountRatio?symbol={symbol}&period=1h&limit=1")
                    if data:
                        result["long_short_ratio"] = float(data[0].get("longShortRatio", 0.5))
                except Exception as e:
                    errors.append(f"{asset} L/S: {e}")
                    result["long_short_ratio"] = 0.5

                # Funding rate
                try:
                    data = self._fetch_json(f"{BINANCE_FUTURES}/fapi/v1/premiumIndex?symbol={symbol}")
                    if data:
                        result["funding_rate"] = float(data.get("lastFundingRate", 0))
                except Exception as e:
                    errors.append(f"{asset} funding: {e}")
                    result["funding_rate"] = 0.0

                # Open Interest
                try:
                    data = self._fetch_json(f"{BINANCE_FUTURES}/fapi/v1/openInterest?symbol={symbol}")
                    if data:
                        result["open_interest"] = float(data.get("openInterest", 0))
                    result["oi_change_pct"] = 0.0  # Need previous to compute change
                except Exception as e:
                    errors.append(f"{asset} OI: {e}")
                    result["oi_change_pct"] = 0.0

                # Taker buy/sell ratio
                try:
                    data = self._fetch_json(f"{BINANCE_FUTURES}/futures/data/takerlongshortRatio?symbol={symbol}&period=1h&limit=1")
                    if data:
                        result["taker_buy_sell_ratio"] = float(data[0].get("buySellRatio", 1.0))
                except Exception as e:
                    errors.append(f"{asset} taker: {e}")
                    result["taker_buy_sell_ratio"] = 1.0

                # Liquidations (simplified — count-based imbalance)
                try:
                    data = self._fetch_json(f"{BINANCE_FUTURES}/fapi/v1/forceOrders?symbol={symbol}&limit=50")
                    if data:
                        longs = sum(1 for o in data if o.get("side") == "SELL")  # forced sell = long liq
                        shorts = sum(1 for o in data if o.get("side") == "BUY")   # forced buy = short liq
                        total = longs + shorts
                        result["liq_imbalance"] = (longs - shorts) / total if total > 0 else 0.0
                    else:
                        result["liq_imbalance"] = 0.0
                except Exception as e:
                    errors.append(f"{asset} liq: {e}")
                    result["liq_imbalance"] = 0.0

                results[asset] = result
            except Exception as e:
                logger.error(f"Derivatives agent error for {asset}: {e}")
                errors.append(f"{asset}: {e}")
                results[asset] = {}

        return results, errors

    def _fetch_json(self, url: str) -> Any:
        req = Request(url, headers={"User-Agent": "web3-signals/1.0"})
        resp = urlopen(req, timeout=15)
        return json.loads(resp.read())
