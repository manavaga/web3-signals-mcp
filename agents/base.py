# agents/base.py
"""Base agent contract and circuit breaker.

Every data agent inherits BaseAgent and implements collect() + empty_data().
CircuitBreaker prevents hammering failed APIs.
"""
from __future__ import annotations
import time
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """3 failures → stop calling for recovery_seconds."""

    def __init__(self, failure_threshold: int = 3, recovery_seconds: float = 1800):
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._failure_count = 0
        self._last_failure_time: float = 0
        self._open = False

    def allow_request(self) -> bool:
        if not self._open:
            return True
        if time.time() - self._last_failure_time >= self._recovery_seconds:
            self._open = False
            self._failure_count = 0
            return True
        return False

    def record_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self._failure_threshold:
            self._open = True
            logger.warning(f"Circuit breaker OPEN after {self._failure_count} failures")

    def record_success(self):
        self._failure_count = 0
        self._open = False


class BaseAgent(ABC):
    """Contract for data collection agents."""

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.circuit_breaker = CircuitBreaker()

    def execute(self) -> dict[str, Any]:
        start = time.time()
        try:
            data, errors = self.collect()
            duration_ms = int((time.time() - start) * 1000)
            status = "success" if not errors else "partial"
            self.circuit_breaker.record_success()
            return {
                "agent": self.agent_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "data": data,
                "meta": {"duration_ms": duration_ms, "errors": errors},
            }
        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            self.circuit_breaker.record_failure()
            logger.error(f"Agent {self.agent_name} failed: {e}")
            return {
                "agent": self.agent_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "data": self.empty_data(),
                "meta": {"duration_ms": duration_ms, "errors": [str(e)]},
            }

    @abstractmethod
    def empty_data(self) -> dict[str, Any]:
        """Return deterministic empty payload."""

    @abstractmethod
    def collect(self) -> tuple[dict[str, Any], list[str]]:
        """Collect data. Return (data, errors)."""
