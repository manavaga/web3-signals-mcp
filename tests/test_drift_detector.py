from tools.drift_detector import detect_drift


def test_detect_drift_from_accuracy_drop():
    """Drift detected when rolling CWA drops below floor for N windows."""
    recent_cwa = [0.55, 0.52, 0.48, 0.38, 0.35, 0.32, 0.30]

    drift = detect_drift(
        recent_cwa=recent_cwa,
        cwa_floor=0.40,
        consecutive_windows=3,
    )

    assert drift["is_drifting"] is True
    assert drift["windows_below_floor"] >= 3
    assert drift["recommended_action"] == "widen_abstain"


def test_no_drift_when_stable():
    """No drift when CWA is stable above floor."""
    recent_cwa = [0.55, 0.52, 0.58, 0.53, 0.56, 0.54, 0.57]

    drift = detect_drift(
        recent_cwa=recent_cwa,
        cwa_floor=0.40,
        consecutive_windows=3,
    )

    assert drift["is_drifting"] is False
    assert drift["recommended_action"] == "none"


def test_critical_severity():
    """Very low CWA should trigger critical severity."""
    recent_cwa = [0.40, 0.35, 0.25, 0.20, 0.15]

    drift = detect_drift(
        recent_cwa=recent_cwa,
        cwa_floor=0.40,
        cwa_critical=0.30,
        consecutive_windows=3,
    )

    assert drift["is_drifting"] is True
    assert drift["severity"] == "critical"
    assert drift["recommended_action"] == "pause_signals"


def test_insufficient_data():
    """Not enough windows should not trigger drift."""
    recent_cwa = [0.30, 0.25]  # Only 2 windows

    drift = detect_drift(
        recent_cwa=recent_cwa,
        cwa_floor=0.40,
        consecutive_windows=3,
    )

    assert drift["is_drifting"] is False


def test_recovery_breaks_consecutive():
    """A recovery window should reset the consecutive counter."""
    recent_cwa = [0.55, 0.35, 0.30, 0.55, 0.35, 0.30]  # Recovery at index 3

    drift = detect_drift(
        recent_cwa=recent_cwa,
        cwa_floor=0.40,
        consecutive_windows=3,
    )

    # Only 2 consecutive at the end (after recovery), not 3
    assert drift["is_drifting"] is False
    assert drift["windows_below_floor"] == 2
