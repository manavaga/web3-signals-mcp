# tests/test_walk_forward.py
"""Tests for walk-forward backtest engine."""
from __future__ import annotations

import pytest

from tools.walk_forward import (
    Fold,
    generate_folds,
    gradient_score,
    evaluate_neutral,
    compute_cwa,
    compute_ic,
    assert_no_leakage,
)


# --- Fold generator tests ---

class TestGenerateFolds:
    def test_no_overlap(self):
        """Folds must have embargo gap between train and test."""
        folds = generate_folds(total_days=180, embargo_days=7, test_window=21, min_train=90)
        for fold in folds:
            assert fold.train_end + 7 <= fold.test_start

    def test_expanding_window(self):
        """Each fold must have more training data than the previous."""
        folds = generate_folds(total_days=180, embargo_days=7, test_window=21, min_train=90)
        for i in range(1, len(folds)):
            assert folds[i].train_end > folds[i - 1].train_end

    def test_fold_count(self):
        """180 days with 90 min train, 7 embargo, 21 test -> 3-4 folds."""
        folds = generate_folds(total_days=180, embargo_days=7, test_window=21, min_train=90)
        assert 3 <= len(folds) <= 5

    def test_embargo_contiguous(self):
        """Embargo must sit between train and test with no gaps."""
        folds = generate_folds(total_days=180, embargo_days=7, test_window=21, min_train=90)
        for fold in folds:
            assert fold.embargo_start == fold.train_end + 1
            assert fold.embargo_end == fold.embargo_start + 6  # 7 days, 0-indexed
            assert fold.test_start == fold.embargo_end + 1

    def test_too_few_days(self):
        """If total_days < min_train + embargo + test, return empty."""
        folds = generate_folds(total_days=50, embargo_days=7, test_window=21, min_train=90)
        assert folds == []

    def test_test_window_coverage(self):
        """Each fold's test window should be exactly test_window days."""
        folds = generate_folds(total_days=180, embargo_days=7, test_window=21, min_train=90)
        for fold in folds:
            assert fold.test_end - fold.test_start + 1 == 21

    def test_last_fold_within_bounds(self):
        """Last fold's test_end must not exceed total_days - 1."""
        folds = generate_folds(total_days=180, embargo_days=7, test_window=21, min_train=90)
        for fold in folds:
            assert fold.test_end <= 179  # 0-based, so max index is 179


# --- Gradient score tests ---

class TestGradientScore:
    def test_bullish_strong_correct(self):
        score = gradient_score("bullish", 4.5, 1.0, 3.0)
        assert score == 1.0

    def test_bullish_correct(self):
        score = gradient_score("bullish", 1.5, 1.0, 3.0)
        assert score == 0.7

    def test_bullish_weak(self):
        score = gradient_score("bullish", 0.5, 1.0, 3.0)
        assert score == 0.4

    def test_bullish_wrong(self):
        score = gradient_score("bullish", -2.0, 1.0, 3.0)
        assert score == 0.0

    def test_bearish_strong(self):
        score = gradient_score("bearish", -5.0, 1.5, 4.0)
        assert score == 1.0

    def test_bearish_wrong(self):
        # +3.0 move exceeds noise_threshold (1.5) — clearly wrong direction
        score = gradient_score("bearish", 3.0, 1.5, 4.0)
        assert score == 0.0

    def test_bearish_wrong_within_noise(self):
        # +1.0 move is within noise band (1.5) — weak, not definitively wrong
        score = gradient_score("bearish", 1.0, 1.5, 4.0)
        assert score == 0.4

    def test_bearish_correct(self):
        score = gradient_score("bearish", -2.0, 1.5, 4.0)
        assert score == 0.7

    def test_bearish_weak(self):
        score = gradient_score("bearish", -0.5, 1.5, 4.0)
        assert score == 0.4

    def test_zero_change_bullish(self):
        score = gradient_score("bullish", 0.0, 1.0, 3.0)
        assert score == 0.4  # Within noise band, weak

    def test_zero_change_bearish(self):
        score = gradient_score("bearish", 0.0, 1.0, 3.0)
        assert score == 0.4  # Within noise band, weak


# --- Neutral evaluator tests ---

class TestEvaluateNeutral:
    def test_correct(self):
        score = evaluate_neutral(0.5, 1.5)
        assert score == 1.0

    def test_missed_move(self):
        score = evaluate_neutral(5.0, 1.5)
        assert score == 0.0

    def test_boundary(self):
        score = evaluate_neutral(1.5, 1.5)
        assert score == 1.0  # At boundary = correct

    def test_negative_move_within_band(self):
        score = evaluate_neutral(-1.0, 1.5)
        assert score == 1.0


# --- CWA tests ---

class TestComputeCWA:
    def test_full_coverage(self):
        cwa = compute_cwa(correct=70, total=100, directional=80)
        assert 0.5 < cwa < 1.0

    def test_low_coverage_penalized(self):
        cwa = compute_cwa(correct=9, total=100, directional=10)
        assert cwa < 0.05

    def test_perfect(self):
        cwa = compute_cwa(correct=100, total=100, directional=100)
        assert cwa == 1.0

    def test_zero_total(self):
        cwa = compute_cwa(correct=0, total=0, directional=0)
        assert cwa == 0.0


# --- IC tests ---

class TestComputeIC:
    def test_positive_correlation(self):
        ic = compute_ic(
            list(range(1, 21)),
            list(range(1, 21)),
        )
        assert ic > 0.9

    def test_insufficient_data(self):
        ic = compute_ic([1, 2, 3], [1, 2, 3])
        assert ic == 0.0

    def test_random_data(self):
        import random
        random.seed(42)
        ic = compute_ic(
            [random.random() for _ in range(50)],
            [random.random() for _ in range(50)],
        )
        assert abs(ic) < 0.3

    def test_with_nones(self):
        indicators = [1, None, 3, 4, None] + list(range(5, 23))
        returns = [1, 2, None, 4, None] + list(range(5, 23))
        ic = compute_ic(indicators, returns)
        assert ic > 0.5  # Should still work after filtering

    def test_all_nones(self):
        ic = compute_ic([None] * 30, [None] * 30)
        assert ic == 0.0


# --- Leakage guard tests ---

class TestAssertNoLeakage:
    def test_valid_fold(self):
        fold = Fold(
            fold_id=1,
            train_start=0, train_end=89,
            embargo_start=90, embargo_end=96,
            test_start=97, test_end=117,
        )
        # Should not raise
        assert_no_leakage(fold, list(range(180)))

    def test_leaky_fold_raises(self):
        fold = Fold(
            fold_id=1,
            train_start=0, train_end=95,
            embargo_start=90, embargo_end=96,
            test_start=97, test_end=117,
        )
        with pytest.raises(AssertionError, match="Leakage"):
            assert_no_leakage(fold, list(range(180)))
