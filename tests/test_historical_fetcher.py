"""Tests for tools.historical_fetcher — all API calls are mocked."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """Return path to a temporary SQLite database file."""
    return str(tmp_path / "test_backtest.db")


def _sample_binance_klines(n: int = 5) -> list[list]:
    """Return raw Binance-format klines (list of lists)."""
    base_ts = int((datetime.utcnow() - timedelta(days=n)).timestamp() * 1000)
    klines = []
    for i in range(n):
        ts = base_ts + i * 86_400_000
        klines.append([
            ts,                     # 0  open time
            str(60000 + i * 100),   # 1  open
            str(61000 + i * 100),   # 2  high
            str(59000 + i * 100),   # 3  low
            str(60500 + i * 100),   # 4  close
            str(1000 + i * 10),     # 5  volume
            ts + 86_399_999,        # 6  close time
            "0", "0", "0", "0", "0" # 7-11  unused
        ])
    return klines


def _sample_fg_response(n: int = 5) -> dict:
    base = int(datetime.utcnow().timestamp())
    return {
        "data": [
            {"value": str(50 + i), "timestamp": str(base - i * 86400)}
            for i in range(n)
        ]
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFetchKlines:
    @patch("tools.historical_fetcher.time.sleep")
    @patch("tools.historical_fetcher.requests.get")
    def test_returns_correct_format(self, mock_get, mock_sleep):
        """Each candle dict has the required fields."""
        from tools.historical_fetcher import fetch_klines

        raw = _sample_binance_klines(5)
        # First call returns data, second call returns empty (terminates loop)
        resp_with_data = MagicMock()
        resp_with_data.json.return_value = raw
        resp_with_data.raise_for_status = MagicMock()
        resp_empty = MagicMock()
        resp_empty.json.return_value = []
        resp_empty.raise_for_status = MagicMock()
        mock_get.side_effect = [resp_with_data, resp_empty]

        candles = fetch_klines("BTCUSDT", "1d", 5)
        assert len(candles) == 5
        for c in candles:
            assert "open" in c and isinstance(c["open"], float)
            assert "high" in c and isinstance(c["high"], float)
            assert "low" in c and isinstance(c["low"], float)
            assert "close" in c and isinstance(c["close"], float)
            assert "volume" in c and isinstance(c["volume"], float)
            assert "date" in c
            assert "timestamp" in c and isinstance(c["timestamp"], int)

    @patch("tools.historical_fetcher.time.sleep")
    @patch("tools.historical_fetcher.requests.get")
    def test_no_duplicates_on_same_page(self, mock_get, mock_sleep):
        """Primary key constraint prevents duplicates when stored."""
        from tools.historical_fetcher import fetch_klines

        raw = _sample_binance_klines(3)
        resp_with_data = MagicMock()
        resp_with_data.json.return_value = raw
        resp_with_data.raise_for_status = MagicMock()
        resp_empty = MagicMock()
        resp_empty.json.return_value = []
        resp_empty.raise_for_status = MagicMock()
        mock_get.side_effect = [resp_with_data, resp_empty]

        candles = fetch_klines("BTCUSDT", "1d", 3)
        dates = [c["date"] for c in candles]
        assert len(dates) == len(set(dates)), "Duplicate dates in candles"


class TestFetchMacro:
    @patch("tools.historical_fetcher.yf")
    def test_returns_all_sources(self, mock_yf):
        """Result dict has sp500, dxy, nasdaq, vix keys."""
        from tools.historical_fetcher import fetch_macro
        import pandas as pd
        import numpy as np

        # Build a minimal DataFrame that yf.download would return
        dates = pd.date_range(end=datetime.utcnow(), periods=10, freq="D")
        data = pd.DataFrame({
            "Open": np.random.uniform(100, 200, 10),
            "Close": np.random.uniform(100, 200, 10),
        }, index=dates)
        mock_yf.download.return_value = data

        result = fetch_macro(days=10)
        assert "sp500" in result
        assert "dxy" in result
        assert "nasdaq" in result
        assert "vix" in result
        for key in ("sp500", "dxy", "nasdaq", "vix"):
            assert isinstance(result[key], list)


class TestFetchFearGreed:
    @patch("tools.historical_fetcher.requests.get")
    def test_format(self, mock_get):
        """Each entry has date (str) and value (int)."""
        from tools.historical_fetcher import fetch_fear_greed

        mock_resp = MagicMock()
        mock_resp.json.return_value = _sample_fg_response(5)
        mock_get.return_value = mock_resp

        entries = fetch_fear_greed(5)
        assert len(entries) == 5
        for e in entries:
            assert "date" in e
            assert "value" in e and isinstance(e["value"], int)


class TestStorage:
    def test_sqlite_write_read(self, tmp_db):
        """Write candles, read them back, verify counts."""
        from tools.historical_fetcher import store_klines_sqlite, init_sqlite

        init_sqlite(tmp_db)
        candles = [
            {"timestamp": 1000000 + i, "date": f"2025-01-{10+i:02d}",
             "open": 60000.0, "high": 61000.0, "low": 59000.0,
             "close": 60500.0, "volume": 1000.0}
            for i in range(5)
        ]
        store_klines_sqlite(tmp_db, "BTCUSDT", candles)

        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM klines WHERE symbol = 'BTCUSDT'")
        assert cur.fetchone()[0] == 5
        conn.close()

    def test_upsert(self, tmp_db):
        """Running twice doesn't create duplicates."""
        from tools.historical_fetcher import store_klines_sqlite, init_sqlite

        init_sqlite(tmp_db)
        candles = [
            {"timestamp": 1000000, "date": "2025-01-10",
             "open": 60000.0, "high": 61000.0, "low": 59000.0,
             "close": 60500.0, "volume": 1000.0}
        ]
        store_klines_sqlite(tmp_db, "BTCUSDT", candles)
        # Store again with updated close
        candles[0]["close"] = 61000.0
        store_klines_sqlite(tmp_db, "BTCUSDT", candles)

        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM klines WHERE symbol = 'BTCUSDT'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT close FROM klines WHERE symbol = 'BTCUSDT'")
        assert cur.fetchone()[0] == 61000.0
        conn.close()

    def test_macro_storage(self, tmp_db):
        """Macro data stored and retrieved correctly."""
        from tools.historical_fetcher import store_macro_sqlite, init_sqlite

        init_sqlite(tmp_db)
        entries = [
            {"date": "2025-01-10", "close": 500.0, "change_pct": 0.5},
            {"date": "2025-01-11", "close": 505.0, "change_pct": 1.0},
        ]
        store_macro_sqlite(tmp_db, "sp500", entries)

        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM macro WHERE source = 'sp500'")
        assert cur.fetchone()[0] == 2
        conn.close()

    def test_fear_greed_storage(self, tmp_db):
        """F&G data stored and retrieved correctly."""
        from tools.historical_fetcher import store_fear_greed_sqlite, init_sqlite

        init_sqlite(tmp_db)
        entries = [
            {"date": "2025-01-10", "value": 55},
            {"date": "2025-01-11", "value": 60},
        ]
        store_fear_greed_sqlite(tmp_db, entries)

        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM fear_greed")
        assert cur.fetchone()[0] == 2
        conn.close()
