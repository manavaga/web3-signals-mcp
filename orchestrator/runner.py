"""
Orchestrator — runs all data agents on a schedule and stores results.

Data collection agents run every 1-2h.
Signal fusion runs every 12h (configurable via SIGNAL_CADENCE_HOURS).

Usage:
    # Run once (all agents + fusion)
    python -m orchestrator.runner --once

    # Run on loop (default 60 min interval, fusion every 12h)
    python -m orchestrator.runner

    # Custom interval
    python -m orchestrator.runner --interval 3600
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

# Per-agent cadence (minutes). Agents whose cadence hasn't elapsed are skipped.
# Override via env: AGENT_CADENCE_TECHNICAL_MIN=60, AGENT_CADENCE_WHALE_MIN=120, etc.
_AGENT_CADENCES_MIN: Dict[str, int] = {
    "technical_agent":   60,   # Hourly — daily candles don't change faster
    "derivatives_agent": 60,   # Hourly — sufficient for funding/OI updates
    "exchange_flow_agent": 120,  # Every 2h — order book/flow data
    "market_agent":      120,  # Every 2h — F&G is daily, prices slow
    "narrative_agent":   120,  # Every 2h — news/social don't change faster
}

_agent_last_run: Dict[str, float] = {}

# Signal fusion cadence (hours). Fusion only runs every N hours.
# Override via env: SIGNAL_CADENCE_HOURS=12
_SIGNAL_CADENCE_HOURS: int = int(os.getenv("SIGNAL_CADENCE_HOURS", "12"))
_last_fusion_run: float | None = None


def _should_run_fusion(force: bool = False) -> bool:
    """Check if enough time has elapsed since last fusion run."""
    global _last_fusion_run
    if force:
        return True
    if _last_fusion_run is None:
        return True  # Always run on first cycle
    elapsed_hours = (time.time() - _last_fusion_run) / 3600
    return elapsed_hours >= _SIGNAL_CADENCE_HOURS


def _should_run_agent(name: str, force: bool = False) -> bool:
    """Check if enough time has elapsed since this agent's last run."""
    if force:
        return True
    env_key = f"AGENT_CADENCE_{name.upper().replace('_AGENT', '')}_MIN"
    cadence_min = int(os.getenv(env_key, str(_AGENT_CADENCES_MIN.get(name, 15))))
    last = _agent_last_run.get(name)
    if last is None:
        return True
    return (time.time() - last) >= cadence_min * 60


def _run_agent(name: str, factory, store: Storage) -> Dict[str, Any]:
    """Run a single agent, save result, return summary."""
    start = time.time()
    try:
        agent = factory()
        result = agent.execute()
        store.save(name, result)
        _agent_last_run[name] = time.time()
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


def run_all_agents(store: Storage, force: bool = False) -> List[Dict[str, Any]]:
    """Run all data collection agents and return summaries.

    Args:
        force: If True, ignore cadence and run all agents (used for --once).
    """
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
        from exchange_flow_agent.engine import ExchangeFlowAgent
        agents.append(("exchange_flow_agent", ExchangeFlowAgent))
    except ImportError as e:
        results.append({"agent": "exchange_flow_agent", "status": "import_error", "duration_sec": 0, "errors": [str(e)]})

    for name, factory in agents:
        if not _should_run_agent(name, force=force):
            cadence = _AGENT_CADENCES_MIN.get(name, 15)
            print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {name}: SKIP (cadence {cadence}min)")
            continue
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Running {name}...")
        summary = _run_agent(name, factory, store)
        status_icon = "OK" if summary["status"] == "success" else "PARTIAL" if summary["status"] == "partial" else "ERR"
        err_count = len(summary["errors"])
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {name}: {status_icon} ({summary['duration_sec']}s, {err_count} errors)")
        results.append(summary)

    return results


def run_fusion(store: Storage, force: bool = False) -> Dict[str, Any] | None:
    """Run signal fusion on latest agent data.

    Returns None if fusion was skipped (cadence not elapsed).
    """
    global _last_fusion_run

    if not _should_run_fusion(force=force):
        elapsed_h = (time.time() - (_last_fusion_run or 0)) / 3600
        remaining_h = _SIGNAL_CADENCE_HOURS - elapsed_h
        print(f"  Signal fusion: SKIP (next run in {remaining_h:.1f}h, cadence={_SIGNAL_CADENCE_HOURS}h)")
        return None

    try:
        from signal_fusion.engine import SignalFusion
        print(f"  Running signal fusion ({_SIGNAL_CADENCE_HOURS}h cadence)...")
        fusion = SignalFusion()
        result = fusion.fuse()
        elapsed_ms = result["meta"]["duration_ms"]
        status = result["status"]
        _last_fusion_run = time.time()
        print(f"  Signal fusion: {status} ({elapsed_ms}ms)")
        return {"status": status, "duration_ms": elapsed_ms, "errors": result["meta"]["errors"]}
    except Exception as exc:
        print(f"  Signal fusion: ERROR - {exc}")
        return {"status": "error", "duration_ms": 0, "errors": [str(exc)]}


def main():
    parser = argparse.ArgumentParser(description="Run signal agents on a schedule")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=3600, help="Seconds between runs (default: 3600 = 60 min)")
    parser.add_argument("--db", type=str, default="signals.db", help="SQLite database path (ignored if DATABASE_URL set)")
    args = parser.parse_args()

    store = Storage(args.db)
    print(f"Orchestrator starting (backend={store.backend}, interval={args.interval}s, fusion_cadence={_SIGNAL_CADENCE_HOURS}h)")
    print(f"DATABASE_URL: {'set' if os.getenv('DATABASE_URL') else 'not set (using SQLite)'}")
    print()

    run_count = 0
    while True:
        run_count += 1
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"=== Run #{run_count} at {ts} ===")

        # Run all agents (force=True on first run or --once to ignore cadence)
        total_start = time.time()
        force = args.once or run_count == 1
        agent_results = run_all_agents(store, force=force)
        total_agent_time = time.time() - total_start

        # Run fusion (gated by cadence — skips if <12h since last run)
        fusion_result = run_fusion(store, force=force)

        # Evaluate pending signals (runs every cycle, evaluates signals >48h old)
        try:
            from signal_fusion.evaluator import SignalEvaluator
            evaluator = SignalEvaluator(store)
            eval_results = evaluator.evaluate_pending()
            if eval_results:
                print(f"  Evaluated {len(eval_results)} signals")
                cwa = evaluator.compute_cwa(days=30)
                print(f"  CWA(30d): {cwa['cwa']:.3f} | Accuracy: {cwa['raw_accuracy']:.3f} | Coverage: {cwa['coverage']:.3f}")
        except Exception as exc:
            print(f"  Signal evaluation: ERROR - {exc}")

        # Self-learning weight optimizer (runs after fusion, when we have new evaluations)
        if fusion_result and fusion_result.get("status") != "error":
            try:
                from signal_fusion.self_learner import SelfLearner
                from signal_fusion.engine import SignalFusion
                sf = SignalFusion()
                learner = SelfLearner(store, sf.profile)
                opt_result = learner.optimize()
                print(f"  Self-learner: {opt_result['action']} | CWA: {opt_result['current_cwa']:.4f} | {opt_result.get('details', '')}")
            except Exception as exc:
                print(f"  Self-learner: ERROR - {exc}")

        total_time = time.time() - total_start
        success_count = sum(1 for r in agent_results if r["status"] == "success")
        partial_count = sum(1 for r in agent_results if r["status"] == "partial")
        error_count = sum(1 for r in agent_results if r["status"] in ("error", "import_error"))

        fusion_status = fusion_result["status"] if fusion_result else "skipped"
        print(f"\n  Total: {total_time:.0f}s | Agents: {success_count} ok, {partial_count} partial, {error_count} error | Fusion: {fusion_status}")
        print()

        if args.once:
            sys.exit(0 if error_count == 0 else 1)

        print(f"  Sleeping {args.interval}s until next run...\n")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
