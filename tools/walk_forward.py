# tools/walk_forward.py
"""Walk-forward backtest engine with gradient scoring, CWA, and IC.

All functions are pure — no side effects, no API calls. Data is passed in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from scipy.stats import spearmanr


# ---------------------------------------------------------------------------
# Component 1: Fold Generator
# ---------------------------------------------------------------------------

@dataclass
class Fold:
    fold_id: int
    train_start: int   # Day index (0-based)
    train_end: int      # Inclusive
    embargo_start: int
    embargo_end: int    # Inclusive
    test_start: int
    test_end: int       # Inclusive


def generate_folds(
    total_days: int,
    embargo_days: int = 14,
    test_window: int = 21,
    min_train: int = 90,
) -> list[Fold]:
    """Generate expanding-window walk-forward folds.

    Example with 200 days:
      Fold 1: Train [0-89],  Embargo [90-103],  Test [104-124]
      Fold 2: Train [0-124], Embargo [125-138], Test [139-159]
      Fold 3: Train [0-159], Embargo [160-173], Test [174-194]

    Critical rule: max(train_days) + embargo_days <= min(test_days)
    """
    min_needed = min_train + embargo_days + test_window
    if total_days < min_needed:
        return []

    folds: list[Fold] = []
    fold_id = 1
    train_end = min_train - 1  # 0-based inclusive

    while True:
        embargo_start = train_end + 1
        embargo_end = embargo_start + embargo_days - 1
        test_start = embargo_end + 1
        test_end = test_start + test_window - 1

        if test_end >= total_days:
            break

        folds.append(Fold(
            fold_id=fold_id,
            train_start=0,
            train_end=train_end,
            embargo_start=embargo_start,
            embargo_end=embargo_end,
            test_start=test_start,
            test_end=test_end,
        ))
        fold_id += 1
        # Expand training window: next fold's train absorbs previous test window
        train_end = test_end

    return folds


# ---------------------------------------------------------------------------
# Component 2: Gradient Score
# ---------------------------------------------------------------------------

def gradient_score(
    direction: str,
    actual_pct_change: float,
    noise_threshold: float,
    strong_threshold: float,
) -> float:
    """Score a directional prediction against actual price change.

    Args:
        direction: "bullish" or "bearish"
        actual_pct_change: actual % price change (positive = up)
        noise_threshold: from assets.yaml (e.g., 1.0% for BTC)
        strong_threshold: from assets.yaml (e.g., 3.0% for BTC)

    Returns:
        0.0 (wrong direction) to 1.0 (strong correct)

    Scoring tiers:
        1.0 — strong correct: move >= strong_threshold in predicted direction
        0.7 — correct: move >= noise_threshold in predicted direction
        0.4 — weak: move in predicted direction but within noise band
              (or zero change — too small to tell)
        0.0 — wrong: move in opposite direction beyond noise
    """
    # Normalize: for bearish predictions, flip the sign so we always
    # evaluate "did it move in the predicted direction?"
    if direction == "bearish":
        effective_change = -actual_pct_change
    else:
        effective_change = actual_pct_change

    abs_change = abs(effective_change)

    if effective_change < 0 and abs_change > noise_threshold:
        # Wrong direction beyond noise
        return 0.0

    if effective_change < 0:
        # Wrong direction but within noise — treat as weak/noise
        return 0.4

    # Correct direction (effective_change >= 0)
    if abs_change >= strong_threshold:
        return 1.0
    elif abs_change >= noise_threshold:
        return 0.7
    else:
        return 0.4


# ---------------------------------------------------------------------------
# Component 3: Neutral/Abstain Evaluator
# ---------------------------------------------------------------------------

def evaluate_neutral(actual_pct_change: float, atr_band_pct: float) -> float:
    """Evaluate a neutral/abstain signal.

    Neutral is CORRECT if price stayed within 1x ATR band.
    Neutral is WRONG (abstain miss) if price moved > 1x ATR.

    Returns: 1.0 (correct neutral) or 0.0 (missed a move)
    """
    return 1.0 if abs(actual_pct_change) <= atr_band_pct else 0.0


# ---------------------------------------------------------------------------
# Component 4: CWA (Coverage-Weighted Accuracy)
# ---------------------------------------------------------------------------

def compute_cwa(
    correct: int,
    total: int,
    directional: int,
    target_coverage: float = 0.30,
) -> float:
    """Coverage-Weighted Accuracy.

    CWA = accuracy * coverage_factor
    coverage = directional / total
    coverage_factor = min(coverage / target_coverage, 1.0)

    This penalizes systems that achieve high accuracy by abstaining
    from most signals. A system calling 1/100 correctly gets low CWA.
    """
    if total == 0 or directional == 0:
        return 0.0

    accuracy = correct / total
    coverage = directional / total
    coverage_factor = min(coverage / target_coverage, 1.0)
    return accuracy * coverage_factor


# ---------------------------------------------------------------------------
# Component 5: Indicator-Level IC Computation
# ---------------------------------------------------------------------------

def compute_ic(
    indicator_values: list,
    forward_returns: list,
    min_observations: int = 20,
    p_threshold: float = 0.05,
) -> float:
    """Compute Information Coefficient (Spearman rank correlation).

    IC > +0.10: Predictive indicator, increase weight
    IC -0.05 to +0.05: Noise, consider removing
    IC < -0.10: Anti-predictive, remove or invert

    Returns 0.0 if insufficient data or not statistically significant.
    """
    if len(indicator_values) < min_observations:
        return 0.0

    # Remove NaN/None pairs
    pairs = [
        (i, r)
        for i, r in zip(indicator_values, forward_returns)
        if i is not None and r is not None
    ]
    if len(pairs) < min_observations:
        return 0.0

    indicators, returns = zip(*pairs)
    corr, p_value = spearmanr(indicators, returns)

    # Handle NaN correlation (e.g., constant inputs)
    if corr != corr:  # NaN check
        return 0.0

    return float(corr) if p_value < p_threshold else 0.0


# ---------------------------------------------------------------------------
# Component 7: Data Leakage Guard
# ---------------------------------------------------------------------------

def assert_no_leakage(fold: Fold, candle_dates: list) -> None:
    """Verify no data leakage in this fold.

    Checks:
    1. Train end + embargo gap <= test start
    2. Embargo starts immediately after train
    3. Test starts immediately after embargo
    """
    embargo_days = fold.embargo_end - fold.embargo_start + 1

    assert fold.embargo_start == fold.train_end + 1, (
        f"Leakage! Embargo must start at train_end+1. "
        f"train_end={fold.train_end}, embargo_start={fold.embargo_start}"
    )
    assert fold.test_start == fold.embargo_end + 1, (
        f"Leakage! Test must start at embargo_end+1. "
        f"embargo_end={fold.embargo_end}, test_start={fold.test_start}"
    )
    assert fold.train_end + embargo_days < fold.test_start, (
        f"Leakage! Train ends at {fold.train_end} + {embargo_days} embargo "
        f"but test starts at {fold.test_start}"
    )

    # Verify no candle index in training range overlaps embargo or test
    if candle_dates:
        max_train_idx = fold.train_end
        assert max_train_idx < fold.embargo_start, (
            f"Leakage! Train index {max_train_idx} >= embargo_start {fold.embargo_start}"
        )
