# tests/test_learning.py
from learning.evaluation import gradient_score, compute_cwa, detect_drift
from learning.optimizer import compute_ic, propose_weight_update, _rank_array


def test_gradient_score_strong_correct():
    assert gradient_score("bullish", 6.0) == 1.0


def test_gradient_score_correct():
    assert gradient_score("bullish", 3.0) == 0.7


def test_gradient_score_weak_correct():
    assert gradient_score("bullish", 1.0) == 0.4


def test_gradient_score_weak_wrong():
    assert gradient_score("bullish", -1.0) == 0.2


def test_gradient_score_wrong():
    assert gradient_score("bullish", -5.0) == 0.0


def test_gradient_score_bearish():
    assert gradient_score("bearish", -6.0) == 1.0
    assert gradient_score("bearish", 5.0) == 0.0


def test_compute_cwa():
    evals = [
        {"direction": "bullish", "gradient_score": 0.7, "abstained": False},
        {"direction": "bearish", "gradient_score": 1.0, "abstained": False},
        {"direction": "neutral", "gradient_score": 0, "abstained": True},
    ]
    result = compute_cwa(evals, target_coverage=0.30)
    assert result["total"] == 3
    assert result["directional"] == 2
    assert result["accuracy"] > 0
    assert result["cwa"] > 0


def test_compute_cwa_all_abstain():
    evals = [{"direction": "neutral", "gradient_score": 0, "abstained": True}] * 5
    result = compute_cwa(evals)
    assert result["accuracy"] == 0
    assert result["directional"] == 0


def test_detect_drift_ic_reversal():
    alerts = detect_drift([0.2, 0.25, 0.1], floor=0.40, critical=0.30)
    assert "IC_REVERSAL" in alerts


def test_detect_drift_no_alert():
    alerts = detect_drift([0.5, 0.6, 0.55], floor=0.40, critical=0.30)
    assert len(alerts) == 0


def test_rank_array():
    ranks = _rank_array([10, 30, 20])
    assert ranks == [1.0, 3.0, 2.0]


def test_rank_array_ties():
    ranks = _rank_array([10, 10, 20])
    assert ranks == [1.5, 1.5, 3.0]


def test_compute_ic():
    dim_scores = [
        {"tech": 70, "market": 60},
        {"tech": 30, "market": 40},
        {"tech": 50, "market": 50},
        {"tech": 80, "market": 70},
        {"tech": 20, "market": 30},
    ]
    outcomes = [5.0, -3.0, 1.0, 7.0, -5.0]
    ics = compute_ic(dim_scores, outcomes)
    assert "tech" in ics
    assert "market" in ics
    assert ics["tech"] > 0  # positive correlation


def test_propose_weight_update():
    current = {"tech": 0.5, "market": 0.3, "deriv": 0.2}
    ics = {"tech": 0.3, "market": -0.1, "deriv": 0.0}
    proposed = propose_weight_update(current, ics, step_size=0.02)
    assert abs(sum(proposed.values()) - 1.0) < 0.001
    assert proposed["tech"] > current["tech"]  # positive IC → weight up
