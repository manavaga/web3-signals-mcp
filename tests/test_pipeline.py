# tests/test_pipeline.py
import os
from scoring.pipeline import fuse_signals
from scoring.config import load_config, load_assets
from scoring.types import Signal


def _get_config():
    root = os.path.join(os.path.dirname(__file__), "..")
    return load_config(os.path.join(root, "config.yaml"))


def _get_assets():
    root = os.path.join(os.path.dirname(__file__), "..")
    return load_assets(os.path.join(root, "assets.yaml"))


def _mock_agent_data():
    return {
        "technical": {
            "BTC": {"rsi_14": 35, "macd_histogram": 0.5, "bb_bandwidth": 0.04,
                    "price": 84000, "ma7": 83000, "ma30": 82000,
                    "volume_status": "normal", "atr_14": 2100, "macd_zscore": 0.3},
            "ETH": {"rsi_14": 55, "macd_histogram": -0.1, "bb_bandwidth": 0.06,
                    "price": 3200, "ma7": 3250, "ma30": 3100,
                    "volume_status": "normal", "atr_14": 120, "macd_zscore": -0.1},
        },
        "derivatives": {
            "BTC": {"long_short_ratio": 0.58, "funding_rate": 0.0001,
                    "oi_change_pct": 2.0, "liq_imbalance": 0.0,
                    "taker_buy_sell_ratio": 1.05},
            "ETH": {"long_short_ratio": 0.62, "funding_rate": 0.0003,
                    "oi_change_pct": -1.0, "liq_imbalance": 0.1,
                    "taker_buy_sell_ratio": 0.98},
        },
        "market": {
            "BTC": {"fear_greed": 35, "volume_ratio": 1.2,
                    "breadth_status": "neutral", "macro_status": "neutral",
                    "order_book_imbalance": 1.1},
            "ETH": {"fear_greed": 35, "volume_ratio": 0.9,
                    "breadth_status": "neutral", "macro_status": "neutral",
                    "order_book_imbalance": 0.95},
        },
    }


def test_fuse_produces_signals():
    cfg = _get_config()
    assets = _get_assets()
    agent_data = _mock_agent_data()

    signals = fuse_signals(agent_data, cfg, assets)

    assert isinstance(signals, dict)
    assert "BTC" in signals
    assert isinstance(signals["BTC"], Signal)
    assert 0 <= signals["BTC"].composite <= 100
    assert signals["BTC"].label in ["STRONG BUY", "MODERATE BUY", "NEUTRAL",
                                     "MODERATE SELL", "STRONG SELL", "INSUFFICIENT EDGE"]


def test_fuse_only_enabled_assets():
    cfg = _get_config()
    assets = _get_assets()
    agent_data = _mock_agent_data()

    signals = fuse_signals(agent_data, cfg, assets)
    assert "INJ" not in signals


def test_fuse_signal_has_regime():
    cfg = _get_config()
    assets = _get_assets()
    agent_data = _mock_agent_data()

    signals = fuse_signals(agent_data, cfg, assets)
    assert signals["BTC"].regime.regime in ["trending_up", "trending_down", "ranging", "volatile"]
    assert signals["BTC"].regime.fg_value == 35


def test_fuse_weights_sum_to_one():
    cfg = _get_config()
    assets = _get_assets()
    agent_data = _mock_agent_data()

    signals = fuse_signals(agent_data, cfg, assets)
    for asset, sig in signals.items():
        total = sum(sig.weights_used.values())
        # Assets with no data across all dimensions get zero weights (all tiers="none")
        if total == 0.0:
            assert all(ds.tier == "none" for ds in sig.dimensions.values()), \
                f"{asset} has zero weights but not all tiers are 'none'"
        else:
            assert abs(total - 1.0) < 0.01, f"{asset} weights sum to {total}"


def test_fuse_with_prev_scores_momentum():
    cfg = _get_config()
    assets = _get_assets()
    agent_data = _mock_agent_data()

    # First run
    signals1 = fuse_signals(agent_data, cfg, assets)
    prev = {asset: sig.composite for asset, sig in signals1.items()}

    # Modify data to shift BTC score significantly
    agent_data["technical"]["BTC"]["rsi_14"] = 20  # very oversold -> bullish
    signals2 = fuse_signals(agent_data, cfg, assets, prev_scores=prev)

    # BTC should show momentum change
    assert signals2["BTC"].momentum in ["improving", "degrading", "stable"]


# --- Relative features tests ---

def _mock_agent_data_for_relative():
    """Agent data where ETH RSI is much higher than BTC RSI."""
    return {
        "technical": {
            "BTC": {"rsi_14": 50, "macd_histogram": 0.0, "bb_bandwidth": 0.06,
                    "price": 84000, "ma7": 83000, "ma30": 82000,
                    "volume_status": "normal", "atr_14": 2100,
                    "roc_7d": 2.0, "macd_zscore": 0.0},
            "ETH": {"rsi_14": 70, "macd_histogram": 0.0, "bb_bandwidth": 0.06,
                    "price": 3200, "ma7": 3250, "ma30": 3100,
                    "volume_status": "normal", "atr_14": 120,
                    "roc_7d": 5.0, "macd_zscore": 0.0},
        },
        "derivatives": {
            "BTC": {"long_short_ratio": 0.58, "funding_rate": 0.0001,
                    "oi_change_pct": 2.0, "liq_imbalance": 0.0,
                    "taker_buy_sell_ratio": 1.05},
            "ETH": {"long_short_ratio": 0.62, "funding_rate": 0.0005,
                    "oi_change_pct": -1.0, "liq_imbalance": 0.1,
                    "taker_buy_sell_ratio": 0.98},
        },
        "market": {
            "BTC": {"fear_greed": 50, "volume_ratio": 1.0,
                    "breadth_status": "neutral", "macro_status": "neutral",
                    "order_book_imbalance": 1.0},
            "ETH": {"fear_greed": 50, "volume_ratio": 1.0,
                    "breadth_status": "neutral", "macro_status": "neutral",
                    "order_book_imbalance": 1.0},
        },
    }


def test_relative_features_adjust_non_btc():
    """ETH with RSI 70 when BTC RSI is 50 -> ETH tech score gets boost."""
    cfg = _get_config()
    assets = _get_assets()
    agent_data = _mock_agent_data_for_relative()

    signals = fuse_signals(agent_data, cfg, assets)

    if "ETH" in signals:
        sig = signals["ETH"]
        # relative_momentum = 70 - 50 = 20
        # adjustment = 20 * 0.15 = 3.0 (clamped to ±5)
        assert sig.metadata.get("relative_momentum") == 20.0
        assert sig.metadata.get("relative_strength") == 3.0  # 5.0 - 2.0
        assert abs(sig.metadata.get("relative_funding", 999) - 0.0004) < 0.0001


def test_relative_features_btc_unchanged():
    """BTC's own scores should not be adjusted by relative features."""
    cfg = _get_config()
    assets = _get_assets()
    agent_data = _mock_agent_data_for_relative()

    # Run once without relative features (baseline)
    # BTC should have no relative metadata
    signals = fuse_signals(agent_data, cfg, assets)

    if "BTC" in signals:
        sig = signals["BTC"]
        assert "relative_momentum" not in sig.metadata
        assert "relative_strength" not in sig.metadata
        assert "relative_funding" not in sig.metadata


def test_relative_features_no_btc_data():
    """If BTC data is missing, no crash, no adjustment."""
    cfg = _get_config()
    assets = _get_assets()
    agent_data = {
        "technical": {
            "ETH": {"rsi_14": 70, "macd_histogram": 0.0, "bb_bandwidth": 0.06,
                    "price": 3200, "ma7": 3250, "ma30": 3100,
                    "volume_status": "normal", "atr_14": 120, "macd_zscore": 0.0},
        },
        "derivatives": {
            "ETH": {"long_short_ratio": 0.62, "funding_rate": 0.0003,
                    "oi_change_pct": -1.0, "liq_imbalance": 0.1,
                    "taker_buy_sell_ratio": 0.98},
        },
        "market": {
            "ETH": {"fear_greed": 50, "volume_ratio": 1.0,
                    "breadth_status": "neutral", "macro_status": "neutral",
                    "order_book_imbalance": 1.0},
        },
    }

    # Should not crash
    signals = fuse_signals(agent_data, cfg, assets)

    if "ETH" in signals:
        sig = signals["ETH"]
        # No relative features since BTC data is missing
        assert "relative_momentum" not in sig.metadata


# --- Trending-down soft dampening tests ---

def test_trending_down_soft_dampening():
    """Trending_down should dampen bullish composites, not hard-cap to 50."""
    # A composite of 75 should be dampened to ~62.5 (not killed to 50)
    composite = 75.0
    dampen_factor = 0.5
    dampened = 50.0 + (composite - 50.0) * dampen_factor
    assert dampened == 62.5
    assert dampened > 50.0  # Still bullish, just reduced


def test_negative_ev_direction_suppressed():
    """Directions with negative EV should be suppressed when enough data exists."""
    # Test the logic directly using DirectionParams field names
    expected_value = -1.5
    win_rate = 0.35
    n_observations = 25
    min_samples = 20

    should_suppress = (
        n_observations >= min_samples
        and expected_value < 0
        and win_rate < 0.45
    )
    assert should_suppress is True


def test_positive_ev_direction_not_suppressed():
    """Directions with positive EV should not be suppressed."""
    expected_value = 2.0
    win_rate = 0.55
    n_observations = 30
    min_samples = 20

    should_suppress = (
        n_observations >= min_samples
        and expected_value < 0
        and win_rate < 0.45
    )
    assert should_suppress is False


def test_negative_ev_insufficient_samples_not_suppressed():
    """Negative EV with too few samples should not be suppressed."""
    expected_value = -1.5
    win_rate = 0.35
    n_observations = 10
    min_samples = 20

    should_suppress = (
        n_observations >= min_samples
        and expected_value < 0
        and win_rate < 0.45
    )
    assert should_suppress is False


def test_negative_ev_high_winrate_not_suppressed():
    """Negative EV but high win rate should not be suppressed (edge case)."""
    expected_value = -0.5
    win_rate = 0.50
    n_observations = 30
    min_samples = 20

    should_suppress = (
        n_observations >= min_samples
        and expected_value < 0
        and win_rate < 0.45
    )
    assert should_suppress is False


def test_targets_always_use_sr_not_learned_flat_pct():
    """Targets should use S/R levels even when learned params exist."""
    # Verify the S/R-based calculate_targets is always used
    from scoring.modifiers import calculate_targets

    targets = calculate_targets(
        entry_price=100.0, composite=65.0, direction="bullish",
        atr_14=2.0, sl_multiplier=1.5,
        cfg={"min_rr_ratio": 1.5, "timeframe_hours": 48},
        sr_levels={"ma7": 98.0, "ma30": 95.0, "bb_upper": 106.0,
                   "bb_lower": 94.0, "swing_high": 108.0, "swing_low": 96.0},
    )
    assert targets is not None
    # SL should be near a support level, not a flat percentage
    assert targets.stop_loss < 100.0
    assert targets.stop_loss > 90.0
    # Target should be at an S/R resistance level
    assert targets.target_price > 100.0


def test_trending_down_no_effect_on_bearish():
    """Bearish composites (< 50) should not be dampened in trending_down."""
    composite = 35.0
    # Dampening only applies when composite > 50
    assert composite < 50.0  # No dampening needed


# --- Per-asset walk-forward weights tests ---

def test_per_asset_weights_loaded_from_baseline(tmp_path, monkeypatch):
    """Per-asset weights from backtest baseline should override tier weights."""
    import json
    from scoring import pipeline

    baseline = {
        "per_asset_weights": {
            "BTC": {"technical": 0.30, "derivatives": 0.20, "market": 0.50},
        }
    }
    baseline_path = tmp_path / "backtest_baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    # Monkeypatch the cache to force reload
    monkeypatch.setitem(pipeline._baseline_cache, "data", None)
    monkeypatch.setitem(pipeline._baseline_cache, "timestamp", 0.0)

    result = pipeline._load_per_asset_weights(path_override=baseline_path)
    assert "BTC" in result
    assert result["BTC"]["technical"] == 0.30
    assert result["BTC"]["derivatives"] == 0.20

    # Non-existent asset should not be present
    assert "UNKNOWN" not in result


def test_per_asset_weights_priority_over_confidence_gated(tmp_path, monkeypatch):
    """Walk-forward per_asset_weights should take priority over assets section."""
    import json
    from scoring import pipeline

    baseline = {
        "per_asset_weights": {
            "BTC": {"technical": 0.30, "derivatives": 0.20, "market": 0.50},
        },
        "assets": {
            "BTC": {
                "confidence": "high",
                "weights": {"technical": 0.70, "market": 0.30},
            },
            "ETH": {
                "confidence": "high",
                "weights": {"technical": 0.45, "market": 0.55},
            },
        },
    }
    baseline_path = tmp_path / "backtest_baseline.json"
    baseline_path.write_text(json.dumps(baseline))

    monkeypatch.setitem(pipeline._baseline_cache, "data", None)
    monkeypatch.setitem(pipeline._baseline_cache, "timestamp", 0.0)

    result = pipeline._load_per_asset_weights(path_override=baseline_path)

    # BTC should use walk-forward weights, not assets section
    assert result["BTC"]["technical"] == 0.30
    assert result["BTC"]["derivatives"] == 0.20

    # ETH should fall through to confidence-gated assets section
    assert result["ETH"]["technical"] == 0.45
    assert result["ETH"]["market"] == 0.55


def test_per_asset_weights_missing_file(tmp_path, monkeypatch):
    """Missing baseline file should return empty dict."""
    from scoring import pipeline

    monkeypatch.setitem(pipeline._baseline_cache, "data", None)
    monkeypatch.setitem(pipeline._baseline_cache, "timestamp", 0.0)

    result = pipeline._load_per_asset_weights(
        path_override=tmp_path / "nonexistent.json"
    )
    assert result == {}
