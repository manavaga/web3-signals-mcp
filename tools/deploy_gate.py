# tools/deploy_gate.py
"""Deploy gate — blocks scoring/config changes that regress CWA.

The gate compares proposed backtest results against a committed baseline.
If overall CWA regresses, or any asset drops by >15%, or abstain miss rate
exceeds 30%, the gate FAILS and deployment is blocked.

Usage:
    from tools.deploy_gate import check_deploy_gate, load_baseline, save_baseline
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

BASELINE_PATH = Path(__file__).parent.parent / "backtest_baseline.json"


def load_baseline(path: Path | None = None) -> dict | None:
    """Load the committed baseline, or None if no baseline exists."""
    p = path or BASELINE_PATH
    if p.exists():
        return json.loads(p.read_text())
    return None


def save_baseline(results: dict, path: Path | None = None) -> Path:
    """Save results as the new baseline. Only call if CWA improved."""
    p = path or BASELINE_PATH
    results["saved_at"] = datetime.now(timezone.utc).isoformat()
    p.write_text(json.dumps(results, indent=2))
    return p


def check_deploy_gate(
    baseline: dict | None,
    proposed: dict,
    max_asset_drop_pct: float = 0.15,
    max_abstain_miss: float = 0.30,
) -> dict:
    """Check if proposed results pass the deploy gate.

    Conditions (ALL must pass):
    1. Overall CWA must not regress
    2. No individual asset's CWA drops by more than max_asset_drop_pct (15%)
    3. No asset's abstain_miss_rate exceeds max_abstain_miss (30%)

    If baseline is None (first run), the gate always passes.

    Returns: {
        "passed": True/False,
        "failures": ["reason 1", "reason 2", ...],
        "summary": {
            "overall_cwa_baseline": float or None,
            "overall_cwa_proposed": float,
            "improved_assets": [...],
            "regressed_assets": [...],
        }
    }
    """
    # First run — no baseline to compare against
    if baseline is None:
        return {
            "passed": True,
            "failures": [],
            "summary": {
                "overall_cwa_baseline": None,
                "overall_cwa_proposed": proposed.get("overall_cwa", 0.0),
                "improved_assets": list(proposed.get("assets", {}).keys()),
                "regressed_assets": [],
            },
        }

    failures: list[str] = []
    improved: list[str] = []
    regressed: list[str] = []

    baseline_cwa = baseline.get("overall_cwa", 0.0)
    proposed_cwa = proposed.get("overall_cwa", 0.0)

    # Condition 1: Overall CWA must not regress
    if proposed_cwa < baseline_cwa:
        failures.append(
            f"Overall CWA regressed: {baseline_cwa:.4f} -> {proposed_cwa:.4f}"
        )

    # Per-asset checks
    baseline_assets = baseline.get("assets", {})
    proposed_assets = proposed.get("assets", {})

    for asset, proposed_data in proposed_assets.items():
        baseline_data = baseline_assets.get(asset)
        p_cwa = proposed_data.get("cwa_24h", 0.0)
        p_abstain = proposed_data.get("abstain_miss_rate", 0.0)

        if baseline_data is not None:
            b_cwa = baseline_data.get("cwa_24h", 0.0)

            # Track improvement / regression
            if p_cwa > b_cwa:
                improved.append(asset)
            elif p_cwa < b_cwa:
                regressed.append(asset)

            # Condition 2: No asset drops by more than max_asset_drop_pct
            if b_cwa > 0:
                drop = (b_cwa - p_cwa) / b_cwa
                if drop > max_asset_drop_pct:
                    failures.append(
                        f"{asset} CWA dropped {drop:.1%}: "
                        f"{b_cwa:.4f} -> {p_cwa:.4f} "
                        f"(max allowed: {max_asset_drop_pct:.0%})"
                    )
        else:
            # New asset, no baseline — counts as improved
            improved.append(asset)

        # Condition 3: Abstain miss rate
        if p_abstain > max_abstain_miss:
            failures.append(
                f"{asset} abstain_miss_rate {p_abstain:.1%} "
                f"exceeds threshold {max_abstain_miss:.0%}"
            )

    return {
        "passed": len(failures) == 0,
        "failures": failures,
        "summary": {
            "overall_cwa_baseline": baseline_cwa,
            "overall_cwa_proposed": proposed_cwa,
            "improved_assets": improved,
            "regressed_assets": regressed,
        },
    }
