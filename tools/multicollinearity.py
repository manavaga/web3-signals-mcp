"""Multicollinearity detection for indicator sets.

Identifies redundant indicators that would cause double-counting
in IC-weighted scoring.
"""
from __future__ import annotations
from scipy.stats import pearsonr


def find_collinear_pairs(
    indicator_series: dict[str, list[float]],
    threshold: float = 0.80,
    min_obs: int = 20,
) -> list[tuple[str, str, float]]:
    """Find pairs of indicators with absolute correlation > threshold.

    Returns: [(name_a, name_b, correlation), ...]
    """
    names = sorted(indicator_series.keys())
    pairs = []

    for i, name_a in enumerate(names):
        for name_b in names[i+1:]:
            vals_a = indicator_series[name_a]
            vals_b = indicator_series[name_b]

            # Align and filter None/NaN
            aligned = [
                (a, b) for a, b in zip(vals_a, vals_b)
                if a is not None and b is not None
                and a == a and b == b
            ]
            if len(aligned) < min_obs:
                continue

            a_vals, b_vals = zip(*aligned)
            corr, _ = pearsonr(a_vals, b_vals)

            if abs(corr) > threshold:
                pairs.append((name_a, name_b, round(corr, 4)))

    return pairs


def drop_collinear(
    fitted_params: dict[str, dict],
    collinear_pairs: list[tuple[str, str, float]],
) -> dict[str, dict]:
    """Drop the lower-IC indicator from each collinear pair.

    Returns filtered fitted_params with collinear indicators removed.
    """
    to_drop = set()

    for name_a, name_b, _ in collinear_pairs:
        if name_a not in fitted_params or name_b not in fitted_params:
            continue
        if name_a in to_drop or name_b in to_drop:
            continue  # Already dropping one of the pair

        ic_a = abs(fitted_params[name_a].get("ic", 0))
        ic_b = abs(fitted_params[name_b].get("ic", 0))

        if ic_a >= ic_b:
            to_drop.add(name_b)
        else:
            to_drop.add(name_a)

    return {k: v for k, v in fitted_params.items() if k not in to_drop}
