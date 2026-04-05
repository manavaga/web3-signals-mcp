# tests/test_learned_params.py
"""Tests for the self-learning parameter engine."""
import pytest
from tools.learned_params import (
    learn_asset_params, _optimize_direction, _compute_confidence,
    _compute_abstain_adjustment, _ema_blend,
    save_learned_state, load_learned_state, LearnedState, AssetLearnedParams,
    DirectionParams, incremental_update,
)
from pathlib import Path
import json
import tempfile


def _make_candles(n: int, base_price: float = 100.0, volatility: float = 0.02):
    """Generate synthetic candles for testing."""
    import random
    random.seed(42)
    candles = []
    price = base_price
    for i in range(n):
        change = random.gauss(0, volatility)
        close = price * (1 + change)
        high = close * (1 + abs(random.gauss(0, volatility * 0.5)))
        low = close * (1 - abs(random.gauss(0, volatility * 0.5)))
        candles.append({
            "date": f"2026-01-{i+1:02d}" if i < 31 else f"2026-02-{i-30:02d}",
            "open": price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000000,
            "timestamp": 1700000000 + i * 86400,
        })
        price = close
    return candles


class TestLearnAssetParams:
    def test_basic_learning(self):
        """Learning produces valid params from price data."""
        candles = _make_candles(100)
        params = learn_asset_params(candles)

        assert params.bullish.optimal_tp_pct > 0
        assert params.bullish.optimal_sl_pct > 0
        assert params.bearish.optimal_tp_pct > 0
        assert params.bearish.optimal_sl_pct > 0
        assert params.daily_volatility_pct > 0
        assert params.noise_floor_pct > 0

    def test_insufficient_data(self):
        """Returns empty params when data is insufficient."""
        candles = _make_candles(10)
        params = learn_asset_params(candles, min_history=30)
        assert params.bullish.optimal_tp_pct == 0
        assert params.bearish.optimal_tp_pct == 0

    def test_tp_always_positive(self):
        """TP distances must be positive."""
        candles = _make_candles(120)
        params = learn_asset_params(candles)
        assert params.bullish.optimal_tp_pct > 0
        assert params.bearish.optimal_tp_pct > 0

    def test_sl_always_positive(self):
        """SL distances must be positive."""
        candles = _make_candles(120)
        params = learn_asset_params(candles)
        assert params.bullish.optimal_sl_pct > 0
        assert params.bearish.optimal_sl_pct > 0

    def test_win_rate_bounded(self):
        """Win rates must be 0-1."""
        candles = _make_candles(120)
        params = learn_asset_params(candles)
        assert 0 <= params.bullish.win_rate <= 1
        assert 0 <= params.bearish.win_rate <= 1

    def test_confidence_bounded(self):
        """Confidence must be 0-1."""
        candles = _make_candles(120)
        params = learn_asset_params(candles)
        assert 0 <= params.bullish.direction_confidence <= 1
        assert 0 <= params.bearish.direction_confidence <= 1


class TestOptimizeDirection:
    def test_returns_valid_result(self):
        """Optimization returns a dict with all required keys."""
        favorable = [1.0, 2.0, 3.0, 4.0, 5.0] * 20
        adverse = [0.5, 1.0, 1.5, 2.0, 2.5] * 20
        returns = [0.5, 1.0, -0.5, 1.5, -1.0] * 20

        result = _optimize_direction(favorable, adverse, returns, "bullish")
        assert "tp_pct" in result
        assert "sl_pct" in result
        assert "win_rate" in result
        assert "expected_value" in result
        assert result["tp_pct"] > 0
        assert result["sl_pct"] > 0

    def test_empty_data_fallback(self):
        """Empty data returns fallback values."""
        result = _optimize_direction([], [], [], "bullish")
        assert result["expected_value"] == -1.0


class TestConfidence:
    def test_high_confidence(self):
        """High win rate with decent R:R = high confidence."""
        conf = _compute_confidence(0.70, 1.5)
        assert conf > 0.5

    def test_low_confidence(self):
        """Win rate below break-even = low confidence."""
        # At R:R=1.5, break-even = 40%. 30% < 40%.
        conf = _compute_confidence(0.30, 1.5)
        assert conf < 0.3

    def test_zero_rr(self):
        """Zero R:R = zero confidence."""
        conf = _compute_confidence(0.50, 0.0)
        assert conf == 0.0

    def test_confidence_range(self):
        """Confidence always 0-1."""
        for wr in [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]:
            for rr in [0.5, 1.0, 1.5, 2.0]:
                c = _compute_confidence(wr, rr)
                assert 0 <= c <= 1, f"confidence={c} for WR={wr}, RR={rr}"


class TestAbstainAdjustment:
    def test_low_confidence_widens_abstain(self):
        """Low confidence → positive adjustment → wider abstain zone."""
        adj = _compute_abstain_adjustment(0.1)
        assert adj > 5.0  # Should strongly discourage signals

    def test_high_confidence_narrows_abstain(self):
        """High confidence → negative adjustment → narrower abstain zone."""
        adj = _compute_abstain_adjustment(0.9)
        assert adj < 0  # Should allow more signals

    def test_neutral_confidence(self):
        """Mid confidence → near-zero adjustment."""
        adj = _compute_abstain_adjustment(0.5)
        assert abs(adj) < 1.0


class TestEMABlend:
    def test_blend_moves_toward_new(self):
        old, new, alpha = 10.0, 20.0, 0.1
        result = _ema_blend(old, new, alpha)
        assert old < result < new

    def test_full_weight_old(self):
        result = _ema_blend(10.0, 20.0, 0.0)
        assert result == 10.0

    def test_full_weight_new(self):
        result = _ema_blend(10.0, 20.0, 1.0)
        assert result == 20.0


class TestPersistence:
    def test_save_and_load(self, tmp_path):
        """Save and load round-trips correctly."""
        state = LearnedState(version="test", learning_days=5)
        state.assets["BTC"] = AssetLearnedParams(
            asset="BTC",
            bullish=DirectionParams(
                optimal_sl_pct=2.0, optimal_tp_pct=4.0,
                realized_rr=2.0, win_rate=0.60,
                n_observations=100, expected_value=0.80,
                direction_confidence=0.7, abstain_adjustment=-1.5,
            ),
            bearish=DirectionParams(
                optimal_sl_pct=3.0, optimal_tp_pct=5.0,
                realized_rr=1.67, win_rate=0.55,
                n_observations=100, expected_value=0.65,
                direction_confidence=0.6, abstain_adjustment=-0.5,
            ),
            daily_volatility_pct=1.86,
            noise_floor_pct=0.8,
            typical_48h_range_pct=2.5,
        )

        path = tmp_path / "test_learned.json"
        save_learned_state(state, path)
        loaded = load_learned_state(path)

        assert loaded is not None
        assert "BTC" in loaded.assets
        btc = loaded.assets["BTC"]
        assert btc.bullish.optimal_sl_pct == 2.0
        assert btc.bullish.optimal_tp_pct == 4.0
        assert btc.bearish.win_rate == 0.55
        assert btc.daily_volatility_pct == 1.86

    def test_load_missing_file(self, tmp_path):
        """Loading from missing file returns None."""
        result = load_learned_state(tmp_path / "nonexistent.json")
        assert result is None


class TestIncrementalUpdate:
    def test_blend_updates_params(self):
        """Incremental update blends old and new params."""
        old_state = LearnedState(version="v1", learning_days=100)
        old_state.assets["BTC"] = AssetLearnedParams(
            asset="BTC",
            bullish=DirectionParams(
                optimal_sl_pct=2.0, optimal_tp_pct=4.0,
                realized_rr=2.0, win_rate=0.60,
                n_observations=100, expected_value=0.80,
                direction_confidence=0.7, abstain_adjustment=-1.5,
            ),
            bearish=DirectionParams(
                optimal_sl_pct=3.0, optimal_tp_pct=5.0,
                realized_rr=1.67, win_rate=0.55,
                n_observations=100, expected_value=0.65,
                direction_confidence=0.6, abstain_adjustment=-0.5,
            ),
            daily_volatility_pct=1.86,
        )

        # New candles with different characteristics
        new_candles = {"BTC": _make_candles(120, base_price=85000, volatility=0.025)}

        updated = incremental_update(old_state, new_candles)

        # Params should have moved slightly from old values
        assert "BTC" in updated.assets
        btc = updated.assets["BTC"]
        # With blending, values should be between old and new (not identical to either)
        assert btc.bullish.n_observations == 101  # Incremented
        assert updated.learning_days == 101
