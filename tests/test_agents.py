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


# --- Derivatives OI tests ---

import os
import tempfile
from unittest.mock import patch, MagicMock
from agents.derivatives import DerivativesAgent
from storage.db import Storage


def _make_storage():
    """Create a Storage instance with a temp file (not :memory: — Storage reconnects per call)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Storage(db_path=path), path


def _make_derivatives_agent(storage=None):
    """Helper to create a DerivativesAgent with test config."""
    config = {}
    symbols = {"BTC": "BTCUSDT"}
    return DerivativesAgent(config, symbols, storage=storage)


def _mock_fetch_json(responses):
    """Return a side_effect function that returns responses based on URL patterns."""
    def side_effect(url):
        if "globalLongShortAccountRatio" in url:
            return responses.get("ls", [{"longShortRatio": "0.5"}])
        elif "premiumIndex" in url:
            return responses.get("funding", {"lastFundingRate": "0.0001"})
        elif "openInterest" in url:
            return responses.get("oi", {"openInterest": "50000"})
        elif "takerlongshortRatio" in url:
            return responses.get("taker", [{"buySellRatio": "1.0"}])
        elif "forceOrders" in url:
            return responses.get("liq", [])
        return {}
    return side_effect


def test_derivatives_oi_change_computed():
    """When previous OI exists in storage, oi_change_pct should be computed."""
    storage, db_path = _make_storage()
    # Store previous OI of 40000
    storage.save_kv("derivatives", "prev_oi_BTCUSDT", 40000.0)

    agent = _make_derivatives_agent(storage=storage)
    responses = {"oi": {"openInterest": "50000"}}  # current OI = 50000

    with patch.object(agent, "_fetch_json", side_effect=_mock_fetch_json(responses)):
        results, errors = agent.collect()

    # (50000 - 40000) / 40000 * 100 = 25.0%
    assert abs(results["BTC"]["oi_change_pct"] - 25.0) < 0.01


def test_derivatives_oi_change_first_run():
    """On first run with no previous OI, oi_change_pct should be 0.0."""
    storage, db_path = _make_storage()
    agent = _make_derivatives_agent(storage=storage)
    responses = {"oi": {"openInterest": "50000"}}

    with patch.object(agent, "_fetch_json", side_effect=_mock_fetch_json(responses)):
        results, errors = agent.collect()

    assert results["BTC"]["oi_change_pct"] == 0.0
    # After first run, the OI should be saved for next time
    saved = storage.load_kv("derivatives", "prev_oi_BTCUSDT")
    assert saved == 50000.0


def test_derivatives_oi_weighted_funding():
    """oi_weighted_funding should equal funding_rate * open_interest."""
    storage, db_path = _make_storage()
    agent = _make_derivatives_agent(storage=storage)
    responses = {
        "funding": {"lastFundingRate": "0.0005"},
        "oi": {"openInterest": "100000"},
    }

    with patch.object(agent, "_fetch_json", side_effect=_mock_fetch_json(responses)):
        results, errors = agent.collect()

    expected = 0.0005 * 100000  # = 50.0
    assert abs(results["BTC"]["oi_weighted_funding"] - expected) < 0.01
