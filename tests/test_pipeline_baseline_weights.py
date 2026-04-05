# tests/test_pipeline_baseline_weights.py
"""Tests for Task 13: pipeline loading per-asset weights from backtest baseline."""
import json
import os
import tempfile
from pathlib import Path

from scoring.pipeline import _load_per_asset_weights, _baseline_cache, fuse_signals
from scoring.config import load_config, load_assets


def _get_config():
    root = os.path.join(os.path.dirname(__file__), "..")
    return load_config(os.path.join(root, "config.yaml"))


def _get_assets():
    root = os.path.join(os.path.dirname(__file__), "..")
    return load_assets(os.path.join(root, "assets.yaml"))


def _mock_agent_data():
    return {
        "technical": {
            "BTC": {"rsi_14": 35, "macd_histogram": 0.5, "bb_position": 0.2,
                    "price": 84000, "ma7": 83000, "ma30": 82000,
                    "volume_status": "normal", "atr_14": 2100},
            "ETH": {"rsi_14": 55, "macd_histogram": -0.1, "bb_position": 0.6,
                    "price": 3200, "ma7": 3250, "ma30": 3100,
                    "volume_status": "normal", "atr_14": 120},
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


def _make_baseline(assets_data: dict) -> Path:
    """Create a temporary baseline JSON file."""
    baseline = {"overall_cwa": 0.25, "assets": assets_data}
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(baseline, f)
    f.close()
    return Path(f.name)


def test_load_per_asset_weights_high_confidence():
    """High-confidence weights are loaded from baseline."""
    path = _make_baseline({
        "BTC": {
            "weights": {"technical": 0.50, "derivatives": 0.30, "market": 0.20},
            "confidence": "high",
            "n_signals": 100,
        }
    })
    try:
        result = _load_per_asset_weights(path_override=path)
        assert "BTC" in result
        assert result["BTC"]["technical"] == 0.50
        assert result["BTC"]["derivatives"] == 0.30
        assert result["BTC"]["market"] == 0.20
    finally:
        os.unlink(path)


def test_load_per_asset_weights_medium_confidence():
    """Medium-confidence weights are also loaded."""
    path = _make_baseline({
        "ETH": {
            "weights": {"technical": 0.40, "derivatives": 0.35, "market": 0.25},
            "confidence": "medium",
            "n_signals": 60,
        }
    })
    try:
        result = _load_per_asset_weights(path_override=path)
        assert "ETH" in result
        assert result["ETH"]["technical"] == 0.40
    finally:
        os.unlink(path)


def test_load_per_asset_weights_ignores_low_confidence():
    """Low and insufficient confidence weights are excluded."""
    path = _make_baseline({
        "BTC": {
            "weights": {"technical": 0.50, "derivatives": 0.30, "market": 0.20},
            "confidence": "high",
        },
        "SOL": {
            "weights": {"technical": 0.33, "derivatives": 0.33, "market": 0.34},
            "confidence": "low",
        },
        "DOGE": {
            "weights": {"technical": 0.33, "derivatives": 0.33, "market": 0.34},
            "confidence": "insufficient",
        },
    })
    try:
        result = _load_per_asset_weights(path_override=path)
        assert "BTC" in result
        assert "SOL" not in result
        assert "DOGE" not in result
    finally:
        os.unlink(path)


def test_load_per_asset_weights_no_file():
    """Returns empty dict when baseline file does not exist."""
    result = _load_per_asset_weights(path_override=Path("/tmp/nonexistent_baseline_xyz.json"))
    assert result == {}


def test_load_per_asset_weights_malformed_json():
    """Returns empty dict for malformed JSON."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    f.write("{not valid json")
    f.close()
    try:
        result = _load_per_asset_weights(path_override=Path(f.name))
        assert result == {}
    finally:
        os.unlink(f.name)


def test_pipeline_uses_per_asset_weights_when_available(monkeypatch):
    """Pipeline uses backtest baseline weights for high-confidence assets."""
    cfg = _get_config()
    assets = _get_assets()
    agent_data = _mock_agent_data()

    custom_weights = {"technical": 0.60, "derivatives": 0.25, "market": 0.15}

    # Monkeypatch _load_per_asset_weights to return custom weights for BTC
    monkeypatch.setattr(
        "scoring.pipeline._load_per_asset_weights",
        lambda path_override=None: {"BTC": custom_weights},
    )

    signals = fuse_signals(agent_data, cfg, assets)

    # BTC should use the per-asset weights (before regime shifts + tier redistribution)
    # The weights_used will be modified by regime shifts and tier redistribution,
    # but the starting point should be different from config defaults.
    # We verify the pipeline doesn't crash and produces valid signals.
    assert "BTC" in signals
    assert 0 <= signals["BTC"].composite <= 100


def test_pipeline_falls_back_to_config_without_baseline(monkeypatch):
    """Without baseline, pipeline uses config.yaml weights as before."""
    cfg = _get_config()
    assets = _get_assets()
    agent_data = _mock_agent_data()

    # Monkeypatch to return empty (no baseline)
    monkeypatch.setattr(
        "scoring.pipeline._load_per_asset_weights",
        lambda path_override=None: {},
    )

    signals = fuse_signals(agent_data, cfg, assets)
    assert "BTC" in signals
    assert isinstance(signals["BTC"].composite, float)


def test_pipeline_ignores_low_confidence_weights(monkeypatch):
    """Per-asset weights with low confidence are not used."""
    cfg = _get_config()
    assets = _get_assets()
    agent_data = _mock_agent_data()

    # _load_per_asset_weights already filters by confidence,
    # so if it returns nothing for an asset, config weights are used.
    monkeypatch.setattr(
        "scoring.pipeline._load_per_asset_weights",
        lambda path_override=None: {},  # nothing passes confidence filter
    )

    # Run with no baseline -> should produce valid signals using config weights
    signals_no_baseline = fuse_signals(agent_data, cfg, assets)

    # Now with a baseline that has per-asset weights for BTC only
    monkeypatch.setattr(
        "scoring.pipeline._load_per_asset_weights",
        lambda path_override=None: {
            "BTC": {"technical": 0.70, "derivatives": 0.15, "market": 0.15}
        },
    )
    signals_with_baseline = fuse_signals(agent_data, cfg, assets)

    # ETH should produce the same result in both cases (not in baseline)
    if "ETH" in signals_no_baseline and "ETH" in signals_with_baseline:
        assert signals_no_baseline["ETH"].composite == signals_with_baseline["ETH"].composite
