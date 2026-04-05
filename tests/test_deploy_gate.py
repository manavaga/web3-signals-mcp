# tests/test_deploy_gate.py
"""Tests for the deploy gate — blocks changes that regress CWA."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tools.deploy_gate import check_deploy_gate, load_baseline, save_baseline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_results(overall_cwa: float, assets: dict | None = None) -> dict:
    """Helper to build a results dict matching run_optimization output."""
    if assets is None:
        assets = {
            "BTC": {"cwa_24h": 0.30, "cwa_48h": 0.28, "accuracy_24h": 0.65,
                    "abstain_miss_rate": 0.15, "n_signals": 90,
                    "weights": {"technical": 0.40, "derivatives": 0.25, "market": 0.35}},
            "ETH": {"cwa_24h": 0.25, "cwa_48h": 0.22, "accuracy_24h": 0.60,
                    "abstain_miss_rate": 0.18, "n_signals": 80,
                    "weights": {"technical": 0.35, "derivatives": 0.30, "market": 0.35}},
        }
    return {"overall_cwa": overall_cwa, "assets": assets}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_gate_passes_when_cwa_improves():
    baseline = _make_results(0.25)
    proposed = _make_results(0.30, {
        "BTC": {"cwa_24h": 0.35, "cwa_48h": 0.33, "accuracy_24h": 0.70,
                "abstain_miss_rate": 0.12, "n_signals": 95,
                "weights": {"technical": 0.40, "derivatives": 0.25, "market": 0.35}},
        "ETH": {"cwa_24h": 0.28, "cwa_48h": 0.25, "accuracy_24h": 0.63,
                "abstain_miss_rate": 0.16, "n_signals": 85,
                "weights": {"technical": 0.35, "derivatives": 0.30, "market": 0.35}},
    })
    result = check_deploy_gate(baseline, proposed)
    assert result["passed"] is True
    assert result["failures"] == []
    assert "BTC" in result["summary"]["improved_assets"]
    assert "ETH" in result["summary"]["improved_assets"]


def test_gate_fails_when_overall_cwa_regresses():
    baseline = _make_results(0.30)
    proposed = _make_results(0.25, {
        "BTC": {"cwa_24h": 0.28, "cwa_48h": 0.26, "accuracy_24h": 0.62,
                "abstain_miss_rate": 0.15, "n_signals": 88,
                "weights": {"technical": 0.40, "derivatives": 0.25, "market": 0.35}},
        "ETH": {"cwa_24h": 0.22, "cwa_48h": 0.20, "accuracy_24h": 0.58,
                "abstain_miss_rate": 0.18, "n_signals": 78,
                "weights": {"technical": 0.35, "derivatives": 0.30, "market": 0.35}},
    })
    result = check_deploy_gate(baseline, proposed)
    assert result["passed"] is False
    assert any("Overall CWA regressed" in f for f in result["failures"])


def test_gate_fails_when_asset_drops_15pct():
    baseline = _make_results(0.30, {
        "BTC": {"cwa_24h": 0.40, "cwa_48h": 0.38, "accuracy_24h": 0.70,
                "abstain_miss_rate": 0.10, "n_signals": 90,
                "weights": {"technical": 0.40, "derivatives": 0.25, "market": 0.35}},
        "ETH": {"cwa_24h": 0.25, "cwa_48h": 0.22, "accuracy_24h": 0.60,
                "abstain_miss_rate": 0.18, "n_signals": 80,
                "weights": {"technical": 0.35, "derivatives": 0.30, "market": 0.35}},
    })
    # BTC drops from 0.40 to 0.30 = 25% drop (exceeds 15% threshold)
    proposed = _make_results(0.30, {
        "BTC": {"cwa_24h": 0.30, "cwa_48h": 0.28, "accuracy_24h": 0.65,
                "abstain_miss_rate": 0.12, "n_signals": 90,
                "weights": {"technical": 0.40, "derivatives": 0.25, "market": 0.35}},
        "ETH": {"cwa_24h": 0.28, "cwa_48h": 0.25, "accuracy_24h": 0.63,
                "abstain_miss_rate": 0.16, "n_signals": 85,
                "weights": {"technical": 0.35, "derivatives": 0.30, "market": 0.35}},
    })
    result = check_deploy_gate(baseline, proposed)
    assert result["passed"] is False
    assert any("BTC" in f and "dropped" in f for f in result["failures"])


def test_gate_fails_high_abstain_miss():
    baseline = _make_results(0.25)
    proposed = _make_results(0.30, {
        "BTC": {"cwa_24h": 0.35, "cwa_48h": 0.33, "accuracy_24h": 0.70,
                "abstain_miss_rate": 0.35,  # 35% > 30% threshold
                "n_signals": 95,
                "weights": {"technical": 0.40, "derivatives": 0.25, "market": 0.35}},
        "ETH": {"cwa_24h": 0.28, "cwa_48h": 0.25, "accuracy_24h": 0.63,
                "abstain_miss_rate": 0.16, "n_signals": 85,
                "weights": {"technical": 0.35, "derivatives": 0.30, "market": 0.35}},
    })
    result = check_deploy_gate(baseline, proposed)
    assert result["passed"] is False
    assert any("abstain_miss_rate" in f and "BTC" in f for f in result["failures"])


def test_gate_passes_no_baseline():
    """First run, no baseline exists — always pass."""
    proposed = _make_results(0.20)
    result = check_deploy_gate(None, proposed)
    assert result["passed"] is True
    assert result["failures"] == []
    assert result["summary"]["overall_cwa_baseline"] is None


def test_gate_multiple_failures():
    """All failure reasons should be reported, not just the first."""
    baseline = _make_results(0.35, {
        "BTC": {"cwa_24h": 0.40, "cwa_48h": 0.38, "accuracy_24h": 0.70,
                "abstain_miss_rate": 0.10, "n_signals": 90,
                "weights": {"technical": 0.40, "derivatives": 0.25, "market": 0.35}},
        "ETH": {"cwa_24h": 0.30, "cwa_48h": 0.28, "accuracy_24h": 0.65,
                "abstain_miss_rate": 0.15, "n_signals": 80,
                "weights": {"technical": 0.35, "derivatives": 0.30, "market": 0.35}},
    })
    # Overall regresses, BTC drops >15%, ETH has high abstain miss
    proposed = _make_results(0.20, {
        "BTC": {"cwa_24h": 0.25, "cwa_48h": 0.22, "accuracy_24h": 0.55,
                "abstain_miss_rate": 0.20, "n_signals": 85,
                "weights": {"technical": 0.40, "derivatives": 0.25, "market": 0.35}},
        "ETH": {"cwa_24h": 0.20, "cwa_48h": 0.18, "accuracy_24h": 0.50,
                "abstain_miss_rate": 0.40,  # >30%
                "n_signals": 75,
                "weights": {"technical": 0.35, "derivatives": 0.30, "market": 0.35}},
    })
    result = check_deploy_gate(baseline, proposed)
    assert result["passed"] is False
    assert len(result["failures"]) >= 3  # overall + BTC drop + ETH abstain


def test_save_and_load_baseline():
    """Save baseline to temp file, load it back, verify round-trip."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp_path = Path(f.name)

    try:
        results = _make_results(0.30)
        save_baseline(results, path=tmp_path)

        loaded = load_baseline(path=tmp_path)
        assert loaded is not None
        assert loaded["overall_cwa"] == 0.30
        assert "saved_at" in loaded
        assert "BTC" in loaded["assets"]
    finally:
        tmp_path.unlink(missing_ok=True)


def test_load_baseline_missing_file():
    """Loading from a non-existent path returns None."""
    result = load_baseline(path=Path("/tmp/does_not_exist_12345.json"))
    assert result is None
