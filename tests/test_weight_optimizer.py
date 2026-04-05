# tests/test_weight_optimizer.py
"""Tests for per-asset weight optimizer."""
from __future__ import annotations

import math
import random

import pytest

from tools.weight_optimizer import (
    generate_weight_grid,
    compute_ic_sub_weights,
    optimize_asset,
    get_confidence_tier,
    run_optimization,
)


# ---------------------------------------------------------------------------
# Weight Grid Tests
# ---------------------------------------------------------------------------

class TestGenerateWeightGrid:
    def test_count(self):
        """Grid should produce a reasonable number of valid combos for 3 dims."""
        grid = generate_weight_grid(n_dims=3, step=0.05)
        # With min=0.05, max=0.70, step=0.05 -> 141 valid combos
        assert 100 <= len(grid) <= 200

    def test_all_sum_to_one(self):
        grid = generate_weight_grid()
        for combo in grid:
            assert abs(sum(combo) - 1.0) < 0.001

    def test_respects_bounds(self):
        grid = generate_weight_grid(min_weight=0.05, max_weight=0.70)
        for combo in grid:
            for w in combo:
                assert 0.05 <= w <= 0.70

    def test_no_duplicates(self):
        grid = generate_weight_grid()
        assert len(grid) == len(set(grid))

    def test_two_dims(self):
        grid = generate_weight_grid(n_dims=2, step=0.10, min_weight=0.10, max_weight=0.90)
        for combo in grid:
            assert len(combo) == 2
            assert abs(sum(combo) - 1.0) < 0.001


# ---------------------------------------------------------------------------
# IC Sub-Weight Tests
# ---------------------------------------------------------------------------

class TestICSubWeights:
    def test_positive_only(self):
        sub = compute_ic_sub_weights({"a": 0.10, "b": -0.05, "c": 0.20})
        assert sub["b"] == 0.0
        assert sub["c"] > sub["a"]  # Higher IC -> higher weight
        assert abs(sum(sub.values()) - 1.0) < 0.001

    def test_all_negative(self):
        """All negative IC -> equal weights (can't determine anything)."""
        sub = compute_ic_sub_weights({"a": -0.10, "b": -0.05})
        assert sub["a"] == 0.5
        assert sub["b"] == 0.5

    def test_all_positive(self):
        sub = compute_ic_sub_weights({"x": 0.20, "y": 0.20})
        assert abs(sub["x"] - 0.5) < 0.01
        assert abs(sub["y"] - 0.5) < 0.01

    def test_single_positive(self):
        sub = compute_ic_sub_weights({"only": 0.15, "bad": -0.10})
        assert abs(sub["only"] - 1.0) < 0.001
        assert sub["bad"] == 0.0


# ---------------------------------------------------------------------------
# Confidence Tier Tests
# ---------------------------------------------------------------------------

class TestConfidenceTiers:
    def test_high(self):
        assert get_confidence_tier(100) == "high"
        assert get_confidence_tier(80) == "high"

    def test_medium(self):
        assert get_confidence_tier(60) == "medium"
        assert get_confidence_tier(50) == "medium"

    def test_low(self):
        assert get_confidence_tier(40) == "low"
        assert get_confidence_tier(30) == "low"

    def test_insufficient(self):
        assert get_confidence_tier(10) == "insufficient"
        assert get_confidence_tier(0) == "insufficient"


# ---------------------------------------------------------------------------
# Optimize Asset Tests
# ---------------------------------------------------------------------------

def _make_synthetic_data(n_days: int = 120, seed: int = 42):
    """Create synthetic dimension scores and returns for testing.

    Technical scores correlate with returns, others are random noise.
    """
    rng = random.Random(seed)
    dimension_scores = {}
    returns_24h = {}
    returns_48h = {}

    for d in range(n_days):
        # Technical score correlates with return
        tech = 50 + rng.gauss(0, 15)
        tech = max(0, min(100, tech))
        # Return correlates with tech
        ret = (tech - 50) * 0.05 + rng.gauss(0, 1.0)
        returns_24h[d] = ret
        returns_48h[d] = ret * 0.8 + rng.gauss(0, 0.5)

        dimension_scores[d] = {
            "technical": tech,
            "derivatives": 50 + rng.gauss(0, 15),  # noise
            "market": 50 + rng.gauss(0, 15),  # noise
        }
    return dimension_scores, returns_24h, returns_48h


def _make_dominant_dimension_data(dominant: str, n_days: int = 120, seed: int = 99):
    """Create data where one dimension is clearly predictive."""
    rng = random.Random(seed)
    dims = ["technical", "derivatives", "market"]
    dimension_scores = {}
    returns_24h = {}
    returns_48h = {}

    for d in range(n_days):
        scores = {}
        dom_score = 50 + rng.gauss(0, 20)
        dom_score = max(0, min(100, dom_score))
        for dim in dims:
            if dim == dominant:
                scores[dim] = dom_score
            else:
                scores[dim] = 50 + rng.gauss(0, 15)
        dimension_scores[d] = scores
        ret = (dom_score - 50) * 0.08 + rng.gauss(0, 0.5)
        returns_24h[d] = ret
        returns_48h[d] = ret * 0.7 + rng.gauss(0, 0.3)
    return dimension_scores, returns_24h, returns_48h


class TestOptimizeAsset:
    def test_returns_valid_weights(self):
        """Optimizer must return weights that sum to 1.0."""
        scores, r24, r48 = _make_synthetic_data()
        result = optimize_asset("BTC", scores, r24, r48, 1.0, 3.0)
        assert abs(sum(result["weights"].values()) - 1.0) < 0.01

    def test_returns_required_keys(self):
        scores, r24, r48 = _make_synthetic_data()
        result = optimize_asset("BTC", scores, r24, r48, 1.0, 3.0)
        required = {
            "weights", "cwa_24h", "cwa_48h", "accuracy_24h",
            "accuracy_48h", "coverage", "abstain_miss_rate",
            "n_signals", "combined_score",
        }
        assert required.issubset(result.keys())

    def test_favors_predictive_dimension(self):
        """If technical is clearly predictive, it should get higher weight."""
        scores, r24, r48 = _make_dominant_dimension_data("technical")
        result = optimize_asset("TEST", scores, r24, r48, 1.0, 3.0)
        assert result["weights"]["technical"] >= 0.30

    def test_combined_score_formula(self):
        """combined_score = 0.4*CWA_24h + 0.4*CWA_48h + 0.2*(1-abstain_miss_rate)."""
        scores, r24, r48 = _make_synthetic_data()
        result = optimize_asset("BTC", scores, r24, r48, 1.0, 3.0)
        expected = (
            0.4 * result["cwa_24h"]
            + 0.4 * result["cwa_48h"]
            + 0.2 * (1.0 - result["abstain_miss_rate"])
        )
        assert abs(result["combined_score"] - expected) < 0.01

    def test_handles_small_dataset(self):
        """Should not crash with small data — returns valid result."""
        scores, r24, r48 = _make_synthetic_data(n_days=10)
        result = optimize_asset("BTC", scores, r24, r48, 1.0, 3.0)
        assert abs(sum(result["weights"].values()) - 1.0) < 0.01


# ---------------------------------------------------------------------------
# Run Optimization Tests
# ---------------------------------------------------------------------------

class TestRunOptimization:
    def test_returns_overall_metrics(self):
        scores, r24, r48 = _make_synthetic_data()
        all_data = {
            "BTC": {
                "dimension_scores": scores,
                "forward_returns_24h": r24,
                "forward_returns_48h": r48,
            }
        }
        configs = {
            "BTC": {"noise_threshold": 1.0, "strong_threshold": 3.0},
        }
        result = run_optimization(all_data, configs)
        assert "version" in result
        assert "overall_cwa" in result
        assert "assets" in result
        assert "BTC" in result["assets"]
        assert "confidence" in result["assets"]["BTC"]
