from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import time
from typing import Any, Dict, List, Tuple


class BaseAgent(ABC):
    """Base contract for all data-collection agents in the x402 signal stack."""

    def __init__(self, agent_name: str, profile_name: str) -> None:
        self.agent_name = agent_name
        self.profile_name = profile_name

    def execute(self) -> Dict[str, Any]:
        start = time.perf_counter()
        status = "success"
        data = self.empty_data()
        errors: List[str] = []

        try:
            data, errors = self.collect()
            if errors:
                status = "partial" if data else "error"
        except Exception as exc:
            status = "error"
            errors.append(str(exc))

        duration_ms = int((time.perf_counter() - start) * 1000)
        return {
            "agent": self.agent_name,
            "profile": self.profile_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "data": data,
            "meta": {
                "duration_ms": duration_ms,
                "errors": errors,
            },
        }

    @abstractmethod
    def empty_data(self) -> Dict[str, Any]:
        """Return deterministic empty payload for this agent."""

    @abstractmethod
    def collect(self) -> Tuple[Dict[str, Any], List[str]]:
        """Collect normalized data and return (data, errors)."""
