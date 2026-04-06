import random
from tools.multicollinearity import find_collinear_pairs, drop_collinear


def test_detect_collinear_pairs():
    """Should identify pairs of indicators with correlation > threshold."""
    random.seed(42)
    n = 100
    base = [random.gauss(0, 1) for _ in range(n)]

    indicators = {
        "independent_1": [random.gauss(0, 1) for _ in range(n)],
        "independent_2": [random.gauss(0, 1) for _ in range(n)],
        "correlated_a": base,
        "correlated_b": [x + random.gauss(0, 0.1) for x in base],
    }

    pairs = find_collinear_pairs(indicators, threshold=0.80)
    assert len(pairs) >= 1

    pair_names = {(a, b) for a, b, _ in pairs}
    assert ("correlated_a", "correlated_b") in pair_names or \
           ("correlated_b", "correlated_a") in pair_names


def test_no_collinear_among_independent():
    """Independent indicators should not be flagged."""
    random.seed(42)
    n = 100
    indicators = {
        f"ind_{i}": [random.gauss(0, 1) for _ in range(n)]
        for i in range(5)
    }
    pairs = find_collinear_pairs(indicators, threshold=0.80)
    assert len(pairs) == 0


def test_drop_collinear_keeps_higher_ic():
    """When dropping collinear indicators, keep the one with higher abs(IC)."""
    fitted_params = {
        "rsi_14": {"mean": 50, "std": 15, "ic": 0.12},
        "rsi_zscore": {"mean": 0, "std": 1, "ic": 0.08},
        "macd_histogram": {"mean": 0, "std": 0.01, "ic": 0.15},
    }
    collinear_pairs = [("rsi_14", "rsi_zscore", 0.92)]

    filtered = drop_collinear(fitted_params, collinear_pairs)

    assert "rsi_14" in filtered
    assert "rsi_zscore" not in filtered
    assert "macd_histogram" in filtered


def test_drop_collinear_empty_pairs():
    """No collinear pairs means nothing dropped."""
    fitted_params = {"a": {"ic": 0.1}, "b": {"ic": 0.2}}
    filtered = drop_collinear(fitted_params, [])
    assert filtered == fitted_params
