"""Concept drift detection based on rolling CWA degradation."""
from __future__ import annotations


def detect_drift(
    recent_cwa: list[float],
    cwa_floor: float = 0.40,
    cwa_critical: float = 0.30,
    consecutive_windows: int = 3,
) -> dict:
    """Detect concept drift from declining CWA.

    Args:
        recent_cwa: Rolling CWA values, most recent last.
        cwa_floor: CWA threshold that triggers concern.
        cwa_critical: CWA threshold that triggers emergency action.
        consecutive_windows: Number of consecutive below-floor windows to trigger drift.

    Returns:
        {"is_drifting": bool, "windows_below_floor": int,
         "recommended_action": str, "severity": str}
    """
    if len(recent_cwa) < consecutive_windows:
        return {"is_drifting": False, "windows_below_floor": 0,
                "recommended_action": "none", "severity": "none"}

    # Count consecutive below-floor windows from the end
    consecutive = 0
    for cwa in reversed(recent_cwa):
        if cwa < cwa_floor:
            consecutive += 1
        else:
            break

    is_drifting = consecutive >= consecutive_windows

    # Determine severity
    latest = recent_cwa[-1] if recent_cwa else 0.5
    if latest < cwa_critical:
        severity = "critical"
        action = "pause_signals"
    elif is_drifting:
        severity = "warning"
        action = "widen_abstain"
    else:
        severity = "none"
        action = "none"

    return {
        "is_drifting": is_drifting,
        "windows_below_floor": consecutive,
        "recommended_action": action,
        "severity": severity,
        "latest_cwa": latest,
    }
