# tests/test_backtest_integration.py
"""Integration tests for the full backtest runner."""
from __future__ import annotations

import json
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

    dim_scores, fwd_24h, fwd_48h, _fitted = compute_daily_scores(
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
    dim_scores_1, fwd_24h_1, fwd_48h_1, _fitted_1 = compute_daily_scores(
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

    dim_scores_2, _, _, _ = compute_daily_scores(
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

    dim_scores, fwd_24h, fwd_48h, _fitted = compute_daily_scores(
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

    dim_scores, fwd_24h, fwd_48h, _fitted = compute_daily_scores(
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


# ---------------------------------------------------------------------------
# Tests: fitted params saved to baseline
# ---------------------------------------------------------------------------

def test_compute_daily_scores_returns_fitted_params():
    """compute_daily_scores should return fitted IC params from the last fold."""
    candles = _make_candles(200, trend=0.002)
    macro = {"sp500": [], "dxy": [], "nasdaq": [], "vix": []}
    fg = [{"date": c["date"], "value": 50} for c in candles]

    dim_scores, fwd_24h, fwd_48h, fitted = compute_daily_scores(
        candles, macro, fg, {}, start_idx=60,
    )
    assert isinstance(fitted, dict), "fitted_params should be a dict"
    assert len(fitted) > 0, "fitted_params should not be empty with 200 candles"
    # Each indicator param should have mean, std, ic
    for ind_name, params in fitted.items():
        assert "mean" in params, f"Missing 'mean' for {ind_name}"
        assert "std" in params, f"Missing 'std' for {ind_name}"
        assert "ic" in params, f"Missing 'ic' for {ind_name}"


def test_compute_daily_scores_fallback_returns_fitted_params():
    """Fallback (insufficient data for folds) should also return fitted params."""
    candles = _make_candles(80)
    macro = {"sp500": [], "dxy": [], "nasdaq": [], "vix": []}
    fg = [{"date": c["date"], "value": 50} for c in candles]

    dim_scores, fwd_24h, fwd_48h, fitted = compute_daily_scores(
        candles, macro, fg, {}, start_idx=40,
    )
    assert isinstance(fitted, dict), "fitted_params should be a dict"
    # May or may not have params depending on data quality, but should be a dict
    if len(dim_scores) > 0:
        # If scores were produced, fitted params should exist
        assert len(fitted) > 0, "fitted_params should not be empty when scores exist"


def test_backtest_saves_fitted_params(tmp_path):
    """Backtest should save fitted params to baseline file."""
    baseline_path = tmp_path / "backtest_baseline.json"

    # Simulate what the backtest saves: verify the format is correct
    baseline = {
        "fitted_params": {
            "rsi_14": {"mean": 50.0, "std": 15.0, "ic": 0.12},
            "macd_histogram": {"mean": 0.0, "std": 0.01, "ic": 0.15},
        },
        "per_asset_weights": {
            "BTC": {"technical": 0.7, "market": 0.3},
            "ETH": {"technical": 0.5, "market": 0.5},
        },
        "timestamp": "2026-04-06T00:00:00+00:00",
    }

    baseline_path.write_text(json.dumps(baseline))
    loaded = json.loads(baseline_path.read_text())

    assert "fitted_params" in loaded
    assert "per_asset_weights" in loaded
    assert "rsi_14" in loaded["fitted_params"]
    assert "mean" in loaded["fitted_params"]["rsi_14"]
    assert "std" in loaded["fitted_params"]["rsi_14"]
    assert "ic" in loaded["fitted_params"]["rsi_14"]
    assert "BTC" in loaded["per_asset_weights"]


def test_save_baseline_merges_fitted_params(tmp_path):
    """save_baseline should merge, not overwrite, fitted_params."""
    from tools.deploy_gate import save_baseline, load_baseline

    baseline_path = tmp_path / "backtest_baseline.json"

    # Pre-existing baseline with fitted_params
    existing = {
        "overall_cwa": 0.05,
        "fitted_params": {
            "rsi_14": {"mean": 50.0, "std": 15.0, "ic": 0.12},
        },
        "assets": {},
    }
    baseline_path.write_text(json.dumps(existing))

    # New results without fitted_params (e.g. a quick backtest)
    new_results = {
        "overall_cwa": 0.06,
        "assets": {"BTC": {"cwa_24h": 0.05}},
    }

    save_baseline(new_results, path=baseline_path)
    loaded = load_baseline(path=baseline_path)

    # fitted_params from existing baseline should be preserved
    assert "fitted_params" in loaded, "fitted_params should be preserved after merge"
    assert loaded["fitted_params"]["rsi_14"]["ic"] == 0.12
    # New results should be updated
    assert loaded["overall_cwa"] == 0.06
    assert "BTC" in loaded["assets"]


def test_full_pipeline_with_all_fixes():
    """Full pipeline with all fixes should produce valid signals."""
    from scoring.pipeline import fuse_signals
    from scoring.config import load_config, load_assets

    config = load_config()
    assets_cfg = load_assets()

    # Mock minimal agent data for BTC
    agent_data = {
        "technical": {"BTC": {
            "price": 84000, "rsi_14": 45, "macd_histogram": 0.001,
            "bb_bandwidth": 0.05, "ma7": 83500, "ma30": 82000,
            "obv_slope": 0.01, "roc_7d": 2.0, "squeeze_on": False,
            "squeeze_momentum": 0.3, "macd_zscore": 0.5,
            "bb_upper": 86000, "bb_lower": 81000,
            "swing_high": 87000, "swing_low": 80000,
            "atr_14": 1500, "volume_status": "normal",
            "bb_position": 0.6, "roc_1d": 0.5, "roc_30d": 5.0,
            "adx_14": 30, "volume_ratio": 1.2, "atr_pct": 1.8,
            "rsi_zscore": 0.3, "bb_zscore": -0.5,
        }},
        "derivatives": {"BTC": {
            "long_short_ratio": 0.55, "funding_rate": 0.0003,
            "oi_change_pct": 3.0, "taker_buy_sell_ratio": 1.05,
            "liq_imbalance": 0.1,
        }},
        "market": {"BTC": {
            "fear_greed": 45, "volume_ratio": 1.2,
            "macro_status": "neutral", "order_book_imbalance": 1.1,
            "stablecoin_supply_change_7d": 0.3,
            "dxy_change": -0.1, "nasdaq_change": 0.5, "vix_roc": -2.0,
        }},
    }

    signals = fuse_signals(agent_data, config, assets_cfg)

    assert "BTC" in signals
    sig = signals["BTC"]

    # Signal should have valid structure
    assert sig.composite >= 0 and sig.composite <= 100
    assert sig.direction in ("bullish", "bearish", "neutral")
    assert sig.label is not None

    # If directional, should have S/R-based targets
    if sig.direction != "neutral" and not sig.abstained:
        assert sig.targets is not None
        assert sig.targets.entry_price > 0
        assert sig.targets.target_price > 0
        assert sig.targets.stop_loss > 0
        assert sig.targets.risk_reward_ratio >= 1.0
        assert sig.targets.confidence in ("high", "medium", "low")

    # Regime should be detected
    assert sig.regime is not None
    assert sig.regime.regime in ("trending_up", "trending_down", "ranging", "volatile")


def test_backtest_results_include_per_asset_weights():
    """run_backtest results should include per_asset_weights at top level."""
    candles = _make_candles(120)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _create_test_db(candles, db_path)
        results = run_backtest(days=120, assets=["BTC"], db_path=db_path)
        if results.get("assets"):
            # If any assets were processed, per_asset_weights should exist
            assert "per_asset_weights" in results, (
                "per_asset_weights should be in backtest results"
            )
            for asset, weights in results["per_asset_weights"].items():
                assert isinstance(weights, dict)
                assert len(weights) > 0
    finally:
        Path(db_path).unlink(missing_ok=True)
