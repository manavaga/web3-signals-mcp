"""
Orchestrator — runs all data agents on a schedule and stores results.

Usage:
    # Run once (all agents)
    python -m orchestrator.runner --once

    # Run on loop (default 15 min interval)
    python -m orchestrator.runner

    # Custom interval
    python -m orchestrator.runner --interval 600
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List

from shared.storage import Storage


def _run_agent(name: str, factory, store: Storage) -> Dict[str, Any]:
    """Run a single agent, save result, return summary."""
    start = time.time()
    try:
        agent = factory()
        result = agent.execute()
        store.save(name, result)
        elapsed = time.time() - start
        return {
            "agent": name,
            "status": result["status"],
            "duration_sec": round(elapsed, 1),
            "errors": result["meta"]["errors"],
        }
    except Exception as exc:
        elapsed = time.time() - start
        return {
            "agent": name,
            "status": "error",
            "duration_sec": round(elapsed, 1),
            "errors": [traceback.format_exc()],
        }


def run_all_agents(store: Storage) -> List[Dict[str, Any]]:
    """Run all data collection agents and return summaries."""
    results: List[Dict[str, Any]] = []

    # Import agents here to avoid import errors if one agent's deps are missing
    agents = []

    try:
        from technical_agent.engine import TechnicalAgent
        agents.append(("technical_agent", TechnicalAgent))
    except ImportError as e:
        results.append({"agent": "technical_agent", "status": "import_error", "duration_sec": 0, "errors": [str(e)]})

    try:
        from derivatives_agent.engine import DerivativesAgent
        agents.append(("derivatives_agent", DerivativesAgent))
    except ImportError as e:
        results.append({"agent": "derivatives_agent", "status": "import_error", "duration_sec": 0, "errors": [str(e)]})

    try:
        from market_agent.engine import MarketAgent
        agents.append(("market_agent", MarketAgent))
    except ImportError as e:
        results.append({"agent": "market_agent", "status": "import_error", "duration_sec": 0, "errors": [str(e)]})

    try:
        from narrative_agent.engine import NarrativeAgent
        agents.append(("narrative_agent", NarrativeAgent))
    except ImportError as e:
        results.append({"agent": "narrative_agent", "status": "import_error", "duration_sec": 0, "errors": [str(e)]})

    try:
        from whale_agent.engine import WhaleAgent
        agents.append(("whale_agent", WhaleAgent))
    except ImportError as e:
        results.append({"agent": "whale_agent", "status": "import_error", "duration_sec": 0, "errors": [str(e)]})

    for name, factory in agents:
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Running {name}...")
        summary = _run_agent(name, factory, store)
        status_icon = "OK" if summary["status"] == "success" else "PARTIAL" if summary["status"] == "partial" else "ERR"
        err_count = len(summary["errors"])
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {name}: {status_icon} ({summary['duration_sec']}s, {err_count} errors)")
        results.append(summary)

    return results


def run_fusion(store: Storage) -> Dict[str, Any]:
    """Run signal fusion on latest agent data."""
    try:
        from signal_fusion.engine import SignalFusion
        fusion = SignalFusion()
        result = fusion.fuse()
        elapsed_ms = result["meta"]["duration_ms"]
        status = result["status"]
        print(f"  Signal fusion: {status} ({elapsed_ms}ms)")
        return {"status": status, "duration_ms": elapsed_ms, "errors": result["meta"]["errors"]}
    except Exception as exc:
        print(f"  Signal fusion: ERROR - {exc}")
        return {"status": "error", "duration_ms": 0, "errors": [str(exc)]}


def main():
    parser = argparse.ArgumentParser(description="Run signal agents on a schedule")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=900, help="Seconds between runs (default: 900 = 15 min)")
    parser.add_argument("--db", type=str, default="signals.db", help="SQLite database path (ignored if DATABASE_URL set)")
    args = parser.parse_args()

    store = Storage(args.db)
    print(f"Orchestrator starting (backend={store.backend}, interval={args.interval}s)")
    print(f"DATABASE_URL: {'set' if os.getenv('DATABASE_URL') else 'not set (using SQLite)'}")
    print()

    run_count = 0
    while True:
        run_count += 1
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"=== Run #{run_count} at {ts} ===")

        # Run all agents
        total_start = time.time()
        agent_results = run_all_agents(store)
        total_agent_time = time.time() - total_start

        # Run fusion
        fusion_result = run_fusion(store)

        total_time = time.time() - total_start
        success_count = sum(1 for r in agent_results if r["status"] == "success")
        partial_count = sum(1 for r in agent_results if r["status"] == "partial")
        error_count = sum(1 for r in agent_results if r["status"] in ("error", "import_error"))

        print(f"\n  Total: {total_time:.0f}s | Agents: {success_count} ok, {partial_count} partial, {error_count} error | Fusion: {fusion_result['status']}")
        print()

        if args.once:
            sys.exit(0 if error_count == 0 else 1)

        print(f"  Sleeping {args.interval}s until next run...\n")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
