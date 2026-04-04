# agents/exchange_flow.py
"""Exchange flow agent — order book depth, taker volume.

Data source: Binance spot API.
"""
from __future__ import annotations
import logging
from typing import Any
from agents.base import BaseAgent

logger = logging.getLogger(__name__)


class ExchangeFlowAgent(BaseAgent):
    def __init__(self, config: dict, symbols: dict[str, str]):
        super().__init__("exchange_flow_agent")
        self.config = config
        self.symbols = symbols

    def empty_data(self) -> dict[str, Any]:
        return {asset: {} for asset in self.symbols}

    def collect(self) -> tuple[dict[str, Any], list[str]]:
        results = {}
        errors = []

        for asset in self.symbols:
            # Exchange flow agent is weight=0.0 in current config
            # Placeholder — returns neutral
            results[asset] = {
                "flow_score": 50.0,
                "detail": "exchange flow agent placeholder — weight=0.0 in config",
            }

        return results, errors
