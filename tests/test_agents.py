# tests/test_agents.py
import time
from agents.base import BaseAgent, CircuitBreaker


class MockAgent(BaseAgent):
    def __init__(self, should_fail=False):
        super().__init__("mock_agent")
        self.should_fail = should_fail

    def empty_data(self) -> dict:
        return {"assets": {}}

    def collect(self) -> tuple[dict, list[str]]:
        if self.should_fail:
            raise ValueError("API down")
        return {"assets": {"BTC": {"score": 65}}}, []


def test_agent_success():
    agent = MockAgent()
    result = agent.execute()
    assert result["status"] == "success"
    assert result["data"]["assets"]["BTC"]["score"] == 65
    assert result["agent"] == "mock_agent"
    assert "timestamp" in result
    assert result["meta"]["duration_ms"] >= 0


def test_agent_error():
    agent = MockAgent(should_fail=True)
    result = agent.execute()
    assert result["status"] == "error"
    assert len(result["meta"]["errors"]) > 0


def test_circuit_breaker_opens_after_failures():
    cb = CircuitBreaker(failure_threshold=3, recovery_seconds=1)
    assert cb.allow_request()
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert not cb.allow_request()


def test_circuit_breaker_recovers():
    cb = CircuitBreaker(failure_threshold=2, recovery_seconds=0.1)
    cb.record_failure()
    cb.record_failure()
    assert not cb.allow_request()
    time.sleep(0.15)
    assert cb.allow_request()


def test_circuit_breaker_resets_on_success():
    cb = CircuitBreaker(failure_threshold=3, recovery_seconds=1)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb.allow_request()
    assert cb._failure_count == 0
