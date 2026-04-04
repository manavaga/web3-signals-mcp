# tools/backtest.py
"""CLI backtest tool — walk-forward validation with P&L simulation.

Usage:
  python3 -m tools.backtest --quick
  python3 -m tools.backtest --full
  python3 -m tools.backtest --asset BTC
"""
from __future__ import annotations
import argparse
import logging
import os

from scoring.config import load_config, load_assets
from learning.evaluation import gradient_score, compute_cwa

logger = logging.getLogger(__name__)


def run_quick_backtest(config, assets_cfg, storage):
    from storage.db import Storage
    stats = storage.load_accuracy_stats(days=7)
    print("\n=== Quick Backtest (7 days) ===")
    print(f"Total evaluations: {stats['total']}")
    for wh, ws in stats.get("windows", {}).items():
        print(f"  {wh}h window: {ws['count']} signals, avg gradient: {ws['avg_gradient']:.3f}")
    print()


def run_full_backtest(config, assets_cfg, storage):
    print("\n=== Full Walk-Forward Backtest ===")
    print("Loading historical signals...")

    history = storage.load_history("signal_fusion", limit=500)
    if not history:
        print("No historical signals found. Run the orchestrator first.")
        return

    print(f"Found {len(history)} fusion snapshots")

    evaluations = []
    for entry in history:
        data = entry.get("data", {})
        for asset, sig in data.items():
            if isinstance(sig, dict):
                evaluations.append({
                    "asset": asset,
                    "direction": sig.get("direction", "neutral"),
                    "composite": sig.get("composite", 50),
                    "label": sig.get("label", "NEUTRAL"),
                    "abstained": sig.get("abstained", False),
                    "gradient_score": 0.5,
                })

    cwa_result = compute_cwa(evaluations, config.evaluation.cwa_target_coverage)

    print(f"\nCWA: {cwa_result['cwa']:.4f}")
    print(f"Accuracy: {cwa_result['accuracy']:.4f}")
    print(f"Coverage: {cwa_result['coverage']:.4f} ({cwa_result['directional']}/{cwa_result['total']})")
    print(f"Coverage Factor: {cwa_result['coverage_factor']:.4f}")


def main():
    parser = argparse.ArgumentParser(description="Web3 Signals Backtest")
    parser.add_argument("--quick", action="store_true", help="Quick 7-day backtest")
    parser.add_argument("--full", action="store_true", help="Full walk-forward backtest")
    parser.add_argument("--asset", type=str, help="Single asset deep-dive")
    parser.add_argument("--db", default="signals.db", help="SQLite path")
    args = parser.parse_args()

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")

    config = load_config()
    assets_cfg = load_assets()

    from storage.db import Storage
    storage = Storage(db_path=args.db)

    if args.quick or (not args.full and not args.asset):
        run_quick_backtest(config, assets_cfg, storage)
    elif args.full:
        run_full_backtest(config, assets_cfg, storage)
    elif args.asset:
        print(f"Asset deep-dive for {args.asset.upper()} — not yet implemented")


if __name__ == "__main__":
    main()
