# learning/optimizer.py
"""Bayesian weight optimizer — shadow mode until 90 days.

Uses Dirichlet prior with decay. Proposes weight adjustments
based on IC (Information Coefficient) of each dimension.
"""
from __future__ import annotations
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _rank_array(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j < len(indexed) - 1 and indexed[j + 1][1] == indexed[j][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 3:
        return 0.0
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    num = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    den_x = sum((x[i] - mean_x) ** 2 for i in range(n))
    den_y = sum((y[i] - mean_y) ** 2 for i in range(n))
    den = (den_x * den_y) ** 0.5
    return num / den if den > 0 else 0.0


def compute_ic(dimension_scores: list[dict[str, float]],
               outcomes: list[float]) -> dict[str, float]:
    if len(dimension_scores) < 5 or len(outcomes) < 5:
        return {}

    outcome_ranks = _rank_array(outcomes)
    dims = list(dimension_scores[0].keys())
    ics = {}

    for dim in dims:
        scores = [ds.get(dim, 50.0) for ds in dimension_scores]
        score_ranks = _rank_array(scores)
        ics[dim] = round(_pearson(score_ranks, outcome_ranks), 4)

    return ics


def propose_weight_update(current_weights: dict[str, float],
                          ics: dict[str, float],
                          step_size: float = 0.02) -> dict[str, float]:
    proposed = dict(current_weights)
    for dim, ic in ics.items():
        if dim in proposed:
            proposed[dim] += ic * step_size

    proposed = {k: max(0.0, v) for k, v in proposed.items()}

    total = sum(proposed.values())
    if total > 0:
        proposed = {k: round(v / total, 4) for k, v in proposed.items()}

    return proposed
