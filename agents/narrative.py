# agents/narrative.py
"""Narrative agent — Reddit sentiment, CryptoPanic, LLM sentiment.

Data sources: Reddit (PRAW), CryptoPanic RSS.
"""
from __future__ import annotations
import logging
from typing import Any
from agents.base import BaseAgent

logger = logging.getLogger(__name__)


class NarrativeAgent(BaseAgent):
    def __init__(self, config: dict, symbols: dict[str, str]):
        super().__init__("narrative_agent")
        self.config = config
        self.symbols = symbols

    def empty_data(self) -> dict[str, Any]:
        return {asset: {} for asset in self.symbols}

    def collect(self) -> tuple[dict[str, Any], list[str]]:
        results = {}
        errors = []

        for asset in self.symbols:
            # Narrative agent is weight=0.0 in current config
            # Placeholder — returns neutral sentiment
            results[asset] = {
                "sentiment_score": 50.0,
                "detail": "narrative agent placeholder — weight=0.0 in config",
            }

        return results, errors
