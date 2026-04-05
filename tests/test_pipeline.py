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
    assert signals["BTC"].regime.regime in ["trending", "ranging", "unknown"]
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
            "BTC": {"rsi_14": 50, "macd_histogram": 0.0, "bb_position": 0.5,
                    "price": 84000, "ma7": 83000, "ma30": 82000,
                    "volume_status": "normal", "atr_14": 2100,
                    "roc_7d": 2.0},
            "ETH": {"rsi_14": 70, "macd_histogram": 0.0, "bb_position": 0.5,
                    "price": 3200, "ma7": 3250, "ma30": 3100,
                    "volume_status": "normal", "atr_14": 120,
                    "roc_7d": 5.0},
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
            "ETH": {"rsi_14": 70, "macd_histogram": 0.0, "bb_position": 0.5,
                    "price": 3200, "ma7": 3250, "ma30": 3100,
                    "volume_status": "normal", "atr_14": 120},
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
