# learning/evaluation.py
"""Signal evaluation + CWA computation + drift detection.

Evaluates signals 48h after generation using gradient scoring.
"""
from __future__ import annotations
import logging
from typing import Any

logger = logging.getLogger(__name__)


def gradient_score(direction: str, pct_change: float,
                   noise_pct: float = 2.0, strong_pct: float = 5.0,
                   thresholds: dict | None = None) -> float:
    t = thresholds or {}
    effective = pct_change if direction == "bullish" else -pct_change

    if effective >= strong_pct:
        return t.get("strong_correct", 1.0)
    elif effective >= noise_pct:
        return t.get("correct", 0.7)
    elif effective >= 0:
        return t.get("weak_correct", 0.4)
    elif effective >= -noise_pct:
        return t.get("weak_wrong", 0.2)
    else:
        return t.get("wrong", 0.0)


def compute_cwa(evaluations: list[dict], target_coverage: float = 0.30) -> dict:
    total = len(evaluations)
    if total == 0:
        return {"cwa": 0, "accuracy": 0, "coverage": 0, "directional": 0, "total": 0}

    directional = [e for e in evaluations if not e.get("abstained", False) and e["direction"] != "neutral"]
    coverage = len(directional) / total
    coverage_factor = min(coverage / target_coverage, 1.0) if target_coverage > 0 else 1.0

    if directional:
        correct_sum = sum(e.get("gradient_score", 0) for e in directional)
        accuracy = correct_sum / len(directional)
    else:
        accuracy = 0

    cwa = accuracy * coverage_factor

    return {
        "cwa": round(cwa, 4),
        "accuracy": round(accuracy, 4),
        "coverage": round(coverage, 4),
        "coverage_factor": round(coverage_factor, 4),
        "directional": len(directional),
        "total": total,
    }


def detect_drift(cwa_history: list[float], floor: float = 0.40,
                 critical: float = 0.30, lookback: int = 3) -> list[str]:
    alerts = []
    recent = cwa_history[:lookback]

    if not recent:
        return alerts

    if all(c < critical for c in recent):
        alerts.append("IC_REVERSAL")
    elif all(c < floor for c in recent):
        alerts.append("IC_DROP")

    if len(cwa_history) >= lookback * 2:
        prev_window = cwa_history[lookback:lookback * 2]
        prev_avg = sum(prev_window) / len(prev_window)
        curr_avg = sum(recent) / len(recent)
        if prev_avg > 0 and (prev_avg - curr_avg) / prev_avg > 0.30:
            alerts.append("REGIME_SHIFT")

    return alerts
