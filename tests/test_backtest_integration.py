# tests/test_backtest_integration.py
"""Integration tests for the full backtest runner."""
from __future__ import annotations

import math
import sqlite3
import tempfile
from pathlib import Path

import pytest

from tools.indicators import (
    calc_rsi,
    compute_technical_indicators,
    compute_market_indicators,
)
from tools.backtest import (
    compute_daily_scores,
    load_candles,
    load_macro_data,
    load_fear_greed_data,
    run_backtest,
)


# ---------------------------------------------------------------------------
# Helpers: synthetic candle data
# ---------------------------------------------------------------------------

def _make_candles(n: int = 100, start_price: float = 50000.0, trend: float = 0.001) -> list[dict]:
    """Generate synthetic candles with a slight upward trend and noise."""
    import random
    random.seed(42)
    candles = []
    price = start_price
    for i in range(n):
        noise = random.uniform(-0.02, 0.02)
        change = trend + noise
        open_p = price
        close_p = price * (1 + change)
        high_p = max(open_p, close_p) * (1 + random.uniform(0, 0.01))
        low_p = min(open_p, close_p) * (1 - random.uniform(0, 0.01))
        vol = random.uniform(1000, 5000)
        candles.append({
            "date": f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": vol,
            "timestamp": 1700000000000 + i * 86400000,
            "symbol": "BTCUSDT",
        })
        price = close_p
    return candles


def _create_test_db(candles: list[dict], db_path: str) -> None:
    """Create a test SQLite database with candles, macro, and F&G data."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS klines (
        symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, timestamp INTEGER, PRIMARY KEY (symbol, date)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS macro (
        source TEXT, date TEXT, close REAL, change_pct REAL,
        PRIMARY KEY (source, date)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS fear_greed (
        date TEXT PRIMARY KEY, value INTEGER
    )""")

    for c in candles:
        cur.execute(
            "INSERT OR REPLACE INTO klines VALUES (?,?,?,?,?,?,?,?)",
            (c["symbol"], c["date"], c["open"], c["high"], c["low"],
             c["close"], c["volume"], c["timestamp"]),
        )
    # Add some F&G data
    for c in candles:
        cur.execute(
            "INSERT OR REPLACE INTO fear_greed VALUES (?,?)",
            (c["date"], 50),
        )
    # Add some macro data
    for source in ["sp500", "dxy", "nasdaq", "vix"]:
        for c in candles:
            close_val = {"sp500": 4500.0, "dxy": 104.0, "nasdaq": 15000.0, "vix": 20.0}[source]
            cur.execute(
                "INSERT OR REPLACE INTO macro VALUES (?,?,?,?)",
                (source, c["date"], close_val, 0.1),
            )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests: indicator computation
# ---------------------------------------------------------------------------

def test_compute_indicators_from_candles_rsi():
    """RSI computed from historical candles matches expected range."""
    candles = _make_candles(50)
    indicators = compute_technical_indicators(candles)
    assert indicators, "Should produce indicators from 50 candles"
    rsi = indicators["rsi_14"]
    assert 0 <= rsi <= 100, f"RSI should be 0-100, got {rsi}"
    # With slight uptrend and seed, RSI should not be exactly 50
    assert rsi != 50.0, "RSI should not be default 50 with real data"


def test_compute_indicators_includes_all_fields():
    """All expected indicator fields are present."""
    candles = _make_candles(80)
    indicators = compute_technical_indicators(candles)
    expected_fields = [
        "price", "rsi_14", "macd_line", "macd_signal", "macd_histogram",
        "bb_upper", "bb_lower", "bb_middle", "bb_bandwidth",
        "atr_14", "atr_pct", "ma7", "ma30", "volume_ratio", "volume_status",
        "obv_slope", "roc_7d",
        "squeeze_on", "squeeze_momentum",
        "rsi_zscore", "macd_zscore", "bb_zscore",
    ]
    for field in expected_fields:
        assert field in indicators, f"Missing field: {field}"


def test_compute_indicators_no_future_leakage():
    """Indicators at day N must not use data from day N+1.

    Verifies by computing indicators at two different slice points
    and checking that the shorter slice doesn't magically know the future.
    """
    candles = _make_candles(100)

    # Compute at day 60 using candles[0:61]
    indicators_60 = compute_technical_indicators(candles[:61])

    # Compute at day 80 using candles[0:81]
    indicators_80 = compute_technical_indicators(candles[:81])

    # The RSI at day 60 should be based on first 61 candles only
    # If we change candle 70's price, day 60's indicators should NOT change
    modified_candles = [dict(c) for c in candles]
    modified_candles[70]["close"] = candles[70]["close"] * 2.0  # Huge change at day 70

    indicators_60_modified = compute_technical_indicators(modified_candles[:61])

    # Day 60 indicators should be identical regardless of day 70 data
    assert indicators_60["rsi_14"] == indicators_60_modified["rsi_14"]
    assert indicators_60["macd_histogram"] == indicators_60_modified["macd_histogram"]
    assert indicators_60["price"] == indicators_60_modified["price"]


def test_compute_indicators_empty_candles():
    """Empty or too-small candle lists return empty dict."""
    assert compute_technical_indicators([]) == {}
    assert compute_technical_indicators([{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100}]) == {}


# ---------------------------------------------------------------------------
# Tests: daily scores
# ---------------------------------------------------------------------------

def test_compute_daily_scores_produces_output():
    """compute_daily_scores returns dimension scores and forward returns."""
    candles = _make_candles(100)
    macro = {"sp500": [], "dxy": [], "nasdaq": [], "vix": []}
    fg = [{"date": c["date"], "value": 50} for c in candles]

    dim_scores, fwd_24h, fwd_48h = compute_daily_scores(
        candles, macro, fg, {}, start_idx=60,
    )
    # Should have scores for days after start_idx (minus last 1-2 for forward returns)
    assert len(dim_scores) > 0, "Should produce dimension scores"
    assert len(fwd_24h) > 0, "Should produce forward returns"
    # All day keys should be present in all three dicts
    for k in dim_scores:
        assert k in fwd_24h, f"Day {k} missing from forward_returns_24h"
        assert k in fwd_48h, f"Day {k} missing from forward_returns_48h"
    # Each dimension score should have technical and market
    for k, scores in dim_scores.items():
        assert "technical" in scores
        assert "market" in scores
        assert 0 <= scores["technical"] <= 100
        assert 0 <= scores["market"] <= 100


# ---------------------------------------------------------------------------
# Tests: SQLite data loading
# ---------------------------------------------------------------------------

def test_load_candles_from_db():
    """Load candles from a test database."""
    candles = _make_candles(50)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _create_test_db(candles, db_path)
        loaded = load_candles(db_path, "BTCUSDT")
        assert len(loaded) == 50
        assert loaded[0]["close"] == candles[0]["close"]
    finally:
        Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Tests: full backtest results format
# ---------------------------------------------------------------------------

def test_backtest_produces_per_asset_results():
    """Full backtest returns per-asset weights, CWA, confidence."""
    candles = _make_candles(120)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _create_test_db(candles, db_path)
        results = run_backtest(days=120, assets=["BTC"], db_path=db_path)
        assert "overall_cwa" in results
        assert "assets" in results
        if results["assets"]:
            btc = results["assets"].get("BTC", {})
            assert "weights" in btc
            assert "cwa_24h" in btc
            assert "confidence" in btc
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_backtest_results_format():
    """Results dict has required fields for deploy gate compatibility."""
    candles = _make_candles(120)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _create_test_db(candles, db_path)
        results = run_backtest(days=120, assets=["BTC"], db_path=db_path)
        # Top-level keys
        assert "overall_cwa" in results
        assert "assets" in results
        assert isinstance(results["overall_cwa"], float)
        assert isinstance(results["assets"], dict)
        # Per-asset keys
        for asset, data in results["assets"].items():
            for key in ["cwa_24h", "cwa_48h", "accuracy_24h", "accuracy_48h",
                        "coverage", "abstain_miss_rate", "n_signals",
                        "combined_score", "weights", "confidence"]:
                assert key in data, f"Missing key '{key}' in {asset} results"
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_backtest_no_database():
    """Backtest with non-existent DB exits with helpful error."""
    with pytest.raises(SystemExit):
        run_backtest(db_path="/tmp/nonexistent_test_db_12345.db")


# ---------------------------------------------------------------------------
# Tests: IC fitting uses only train data (no look-ahead bias)
# ---------------------------------------------------------------------------

def test_ic_fitting_uses_only_train_data():
    """IC params must be fitted on train fold data only, never on test fold data.

    This test verifies that modifying test-fold forward returns does NOT change
    the dimension scores for test days. If IC fitting used all data (including
    test), changing test-fold returns would change the fitted params and thus
    the scores -- that would be look-ahead bias.
    """
    from unittest.mock import patch
    from tools.walk_forward import generate_folds

    candles = _make_candles(200, trend=0.002)
    macro = {"sp500": [], "dxy": [], "nasdaq": [], "vix": []}
    fg = [{"date": c["date"], "value": 50} for c in candles]

    # Run 1: normal computation
    dim_scores_1, fwd_24h_1, fwd_48h_1 = compute_daily_scores(
        candles, macro, fg, {}, start_idx=60,
    )

    # Verify we got fold-based scoring (enough data for folds)
    all_day_keys = sorted(fwd_48h_1.keys())
    folds = generate_folds(len(all_day_keys))
    assert len(folds) > 0, "Should generate folds with 200 candles"

    # Get the last fold's test range
    last_fold = folds[-1]
    test_keys = all_day_keys[last_fold.test_start:last_fold.test_end + 1]
    assert len(test_keys) > 0, "Last fold should have test days"

    # Run 2: modify forward returns in the LAST fold's test range
    # If IC fitting is correct (train-only), this should NOT change scores
    # for earlier folds' test days
    modified_candles = [dict(c) for c in candles]
    # Flip the price trend dramatically for the last test window
    for i in range(len(candles) - 20, len(candles)):
        if i < len(modified_candles):
            modified_candles[i]["close"] = candles[i]["close"] * 0.5  # 50% crash

    dim_scores_2, _, _ = compute_daily_scores(
        modified_candles, macro, fg, {}, start_idx=60,
    )

    # Check scores for EARLIER folds' test days -- they should be identical
    # because their train data didn't include the modified region
    if len(folds) >= 2:
        first_fold = folds[0]
        first_test_keys = all_day_keys[first_fold.test_start:first_fold.test_end + 1]
        for dk in first_test_keys:
            if dk in dim_scores_1 and dk in dim_scores_2:
                for dim in dim_scores_1[dk]:
                    assert dim_scores_1[dk][dim] == dim_scores_2[dk][dim], (
                        f"Look-ahead bias detected! Score for day {dk}, "
                        f"dimension '{dim}' changed when future data was modified. "
                        f"Original: {dim_scores_1[dk][dim]}, "
                        f"Modified: {dim_scores_2[dk][dim]}"
                    )


def test_ic_fitting_fallback_when_insufficient_data():
    """When data is too short for folds, IC fitting falls back to all data."""
    # Use only 80 candles -- not enough for folds (need min_train=90 + embargo + test)
    candles = _make_candles(80)
    macro = {"sp500": [], "dxy": [], "nasdaq": [], "vix": []}
    fg = [{"date": c["date"], "value": 50} for c in candles]

    dim_scores, fwd_24h, fwd_48h = compute_daily_scores(
        candles, macro, fg, {}, start_idx=40,
    )
    # Should still produce scores (fallback to all-data fitting)
    assert len(dim_scores) > 0, "Fallback should still produce scores"
    for k, scores in dim_scores.items():
        assert "technical" in scores
        assert "market" in scores


def test_scored_days_are_test_days_only():
    """With fold-based scoring, only test-fold days should have scores.

    Train days should NOT be scored (they were used for fitting).
    """
    from tools.walk_forward import generate_folds

    candles = _make_candles(200, trend=0.002)
    macro = {"sp500": [], "dxy": [], "nasdaq": [], "vix": []}
    fg = [{"date": c["date"], "value": 50} for c in candles]

    dim_scores, fwd_24h, fwd_48h = compute_daily_scores(
        candles, macro, fg, {}, start_idx=60,
    )

    all_day_keys = sorted(fwd_48h.keys())
    folds = generate_folds(len(all_day_keys))
    assert len(folds) > 0

    # Collect all test day keys across folds
    test_day_set = set()
    for fold in folds:
        for dk in all_day_keys[fold.test_start:fold.test_end + 1]:
            test_day_set.add(dk)

    # All scored days must be test days
    for dk in dim_scores:
        assert dk in test_day_set, (
            f"Day {dk} was scored but is not a test day in any fold. "
            f"This means train/embargo data leaked into scored output."
        )
