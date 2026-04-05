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


# --- Market agent macro tests ---

import pandas as pd
import numpy as np
from agents.market import MarketAgent, _macro_cache


def _make_market_agent():
    """Helper to create a MarketAgent with test config."""
    config = {
        "macro_vix_risk_off": 25,
        "macro_vix_risk_on": 18,
        "macro_sp_risk_off_pct": -1.5,
        "macro_sp_risk_on_pct": 0.5,
        "macro_dxy_risk_off_pct": 0.5,
        "macro_dxy_risk_on_pct": -0.3,
    }
    symbols = {"BTC": "BTCUSDT"}
    coingecko_ids = {"BTC": "bitcoin"}
    return MarketAgent(config, symbols, coingecko_ids)


def _mock_yf_download(close_values):
    """Create a mock DataFrame that mimics yfinance download output."""
    dates = pd.date_range("2026-01-01", periods=len(close_values), freq="D")
    df = pd.DataFrame({"Close": close_values}, index=dates)
    return df


def test_market_macro_fetches_real_data():
    """sp500_change, dxy_change, nasdaq_change should be floats, not None."""
    agent = _make_market_agent()
    # Reset cache
    _macro_cache["data"] = None
    _macro_cache["timestamp"] = 0

    mock_df = _mock_yf_download([100.0, 101.0, 102.0, 103.0, 104.0])

    with patch("agents.market.yf") as mock_yf, \
         patch.object(agent, "_fetch_json") as mock_fetch:
        mock_yf.download.return_value = mock_df
        mock_yf.Ticker.return_value.fast_info = {"lastPrice": 20}
        mock_fetch.return_value = {"data": [{"value": "50"}]}

        # Mock BTC dominance
        with patch("agents.market.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"data": {"market_cap_percentage": {"btc": 55.2}}}
            mock_req.get.return_value = mock_resp

            results, errors = agent.collect()

    btc = results["BTC"]
    assert isinstance(btc.get("sp500_change"), float), f"sp500_change should be float, got {type(btc.get('sp500_change'))}"
    assert isinstance(btc.get("dxy_change"), float), f"dxy_change should be float, got {type(btc.get('dxy_change'))}"
    assert isinstance(btc.get("nasdaq_change"), float), f"nasdaq_change should be float, got {type(btc.get('nasdaq_change'))}"


def test_market_macro_cache_works():
    """Second call within 30min should return cached data without calling yfinance again."""
    agent = _make_market_agent()
    # Reset cache
    _macro_cache["data"] = None
    _macro_cache["timestamp"] = 0

    mock_df = _mock_yf_download([100.0, 101.0, 102.0, 103.0, 104.0])

    with patch("agents.market.yf") as mock_yf:
        mock_yf.download.return_value = mock_df
        mock_yf.Ticker.return_value.fast_info = {"lastPrice": 20}

        # First call — populates cache
        agent._fetch_macro_cached()
        first_call_count = mock_yf.download.call_count

        # Second call — should use cache
        agent._fetch_macro_cached()
        second_call_count = mock_yf.download.call_count

    assert second_call_count == first_call_count, "Cache miss: yfinance was called again within TTL"


def test_market_btc_dominance_fetched():
    """btc_dominance should be a float from CoinGecko global endpoint."""
    agent = _make_market_agent()
    _macro_cache["data"] = None
    _macro_cache["timestamp"] = 0

    mock_df = _mock_yf_download([100.0, 101.0, 102.0, 103.0, 104.0])

    with patch("agents.market.yf") as mock_yf, \
         patch.object(agent, "_fetch_json") as mock_fetch, \
         patch("agents.market.requests") as mock_req:
        mock_yf.download.return_value = mock_df
        mock_yf.Ticker.return_value.fast_info = {"lastPrice": 20}

        # F&G mock
        def fetch_side_effect(url):
            if "alternative.me" in url:
                return {"data": [{"value": "50"}]}
            return {}
        mock_fetch.side_effect = fetch_side_effect

        # BTC dominance mock
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"market_cap_percentage": {"btc": 62.5}}}
        mock_req.get.return_value = mock_resp

        results, errors = agent.collect()

    assert isinstance(results["BTC"].get("btc_dominance"), float)
    assert results["BTC"]["btc_dominance"] == 62.5


def test_market_breadth_not_hardcoded():
    """breadth_status should vary based on BTC dominance, not always 'neutral'."""
    agent = _make_market_agent()
    _macro_cache["data"] = None
    _macro_cache["timestamp"] = 0

    mock_df = _mock_yf_download([100.0, 101.0, 102.0, 103.0, 104.0])

    with patch("agents.market.yf") as mock_yf, \
         patch.object(agent, "_fetch_json") as mock_fetch, \
         patch("agents.market.requests") as mock_req:
        mock_yf.download.return_value = mock_df
        mock_yf.Ticker.return_value.fast_info = {"lastPrice": 20}

        def fetch_side_effect(url):
            if "alternative.me" in url:
                return {"data": [{"value": "50"}]}
            return {}
        mock_fetch.side_effect = fetch_side_effect

        # High BTC dominance (>60%) => "loser" breadth (altcoins losing)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"market_cap_percentage": {"btc": 65.0}}}
        mock_req.get.return_value = mock_resp

        results, errors = agent.collect()

    assert results["BTC"]["breadth_status"] != "neutral", \
        "breadth_status should not always be 'neutral'"


def test_market_macro_status_risk_off():
    """High VIX + negative S&P should produce strong_risk_off."""
    agent = _make_market_agent()
    _macro_cache["data"] = None
    _macro_cache["timestamp"] = 0

    # S&P drops 2%: 100 -> 98
    mock_df_spy = _mock_yf_download([102.0, 101.0, 100.5, 100.0, 98.0])
    mock_df_other = _mock_yf_download([100.0, 100.5, 101.0, 101.5, 102.0])

    def download_side_effect(ticker, **kwargs):
        if ticker == "SPY":
            return mock_df_spy
        return mock_df_other

    with patch("agents.market.yf") as mock_yf, \
         patch.object(agent, "_fetch_json") as mock_fetch, \
         patch("agents.market.requests") as mock_req:
        mock_yf.download.side_effect = download_side_effect
        mock_yf.Ticker.return_value.fast_info = {"lastPrice": 30}  # VIX > 25

        def fetch_side_effect(url):
            if "alternative.me" in url:
                return {"data": [{"value": "50"}]}
            return {}
        mock_fetch.side_effect = fetch_side_effect

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"market_cap_percentage": {"btc": 50.0}}}
        mock_req.get.return_value = mock_resp

        results, errors = agent.collect()

    assert results["BTC"]["macro_status"] == "strong_risk_off"


def test_market_vix_rate_of_change():
    """VIX RoC should be computed as % change from previous close."""
    agent = _make_market_agent()
    _macro_cache["data"] = None
    _macro_cache["timestamp"] = 0

    mock_df = _mock_yf_download([100.0, 101.0, 102.0, 103.0, 104.0])

    with patch("agents.market.yf") as mock_yf:
        # VIX data: 18 -> 22 = +22.2% RoC
        vix_df = _mock_yf_download([15.0, 16.0, 17.0, 18.0, 22.0])
        def download_side_effect(ticker, **kwargs):
            if ticker == "^VIX":
                return vix_df
            return mock_df
        mock_yf.download.side_effect = download_side_effect
        mock_yf.Ticker.return_value.fast_info = {"lastPrice": 22}

        macro = agent._fetch_macro_cached()

    expected_roc = ((22.0 - 18.0) / 18.0) * 100
    assert abs(macro["vix_roc"] - expected_roc) < 0.1, f"Expected VIX RoC ~{expected_roc}, got {macro.get('vix_roc')}"
