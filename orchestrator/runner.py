# orchestrator/runner.py
"""Scheduler — runs agents on cadence, fusion every 12h, evaluates signals."""
from __future__ import annotations
import argparse
import logging
import os
import time
from datetime import datetime, timezone

from scoring.config import load_config, load_assets
from scoring.pipeline import fuse_signals
from storage.db import Storage

logger = logging.getLogger(__name__)

SIGNAL_CADENCE_HOURS = int(os.getenv("SIGNAL_CADENCE_HOURS", "12"))


def _load_agents(config, assets_cfg):
    symbols = {a: assets_cfg.get(a).binance_symbol for a in assets_cfg.enabled_assets()}
    agents = []

    from agents.technical import TechnicalAgent
    agents.append(("technical_agent", TechnicalAgent(config.agents.technical.model_dump(), symbols),
                   config.agents.technical.cadence_minutes))

    try:
        from agents.derivatives import DerivativesAgent
        agents.append(("derivatives_agent", DerivativesAgent(config.agents.derivatives.model_dump(), symbols),
                       config.agents.derivatives.cadence_minutes))
    except ImportError:
        pass

    try:
        from agents.market import MarketAgent
        coingecko_ids = {a: assets_cfg.get(a).coingecko_id for a in assets_cfg.enabled_assets()}
        agents.append(("market_agent", MarketAgent(config.agents.market.model_dump(), symbols, coingecko_ids),
                       config.agents.market.cadence_minutes))
    except ImportError:
        pass

    try:
        from agents.narrative import NarrativeAgent
        agents.append(("narrative_agent", NarrativeAgent(config.agents.narrative.model_dump(), symbols),
                       config.agents.narrative.cadence_minutes))
    except ImportError:
        pass

    try:
        from agents.exchange_flow import ExchangeFlowAgent
        agents.append(("exchange_flow_agent", ExchangeFlowAgent(config.agents.exchange_flow.model_dump(), symbols),
                       config.agents.exchange_flow.cadence_minutes))
    except ImportError:
        pass

    return agents


def run_cycle(config, assets_cfg, storage, agents, last_runs, last_fusion, force=False):
    now = time.time()

    for name, agent, cadence_min in agents:
        last = last_runs.get(name, 0)
        if force or (now - last) >= cadence_min * 60:
            if not agent.circuit_breaker.allow_request():
                logger.warning(f"Circuit breaker open for {name}, skipping")
                continue
            logger.info(f"Running {name}...")
            result = agent.execute()
            storage.save(name, result.get("data", {}))
            last_runs[name] = now
            logger.info(f"{name}: {result['status']} ({result['meta']['duration_ms']}ms)")

    if force or (now - last_fusion) >= SIGNAL_CADENCE_HOURS * 3600:
        logger.info("Running signal fusion...")
        agent_names = [n for n, _, _ in agents]
        raw = storage.load_all_latest(agent_names)

        agent_data = {}
        for name in ["technical", "derivatives", "market", "narrative", "exchange_flow"]:
            full_name = f"{name}_agent"
            agent_data[name] = raw.get(full_name) or {}

        signals = fuse_signals(agent_data, config, assets_cfg)

        fusion_data = {asset: sig.to_dict() for asset, sig in signals.items()}
        storage.save("signal_fusion", fusion_data)

        for asset, sig in signals.items():
            entry_price = sig.targets.entry_price if sig.targets else 0
            if entry_price > 0:
                storage.save_performance_snapshot(
                    asset=asset, signal_score=sig.composite,
                    signal_direction=sig.direction,
                    price_at_signal=entry_price,
                    sources_count=sum(1 for d in sig.dimensions.values() if d.tier != "none"),
                    detail=sig.label,
                )

        logger.info(f"Fusion complete: {len(signals)} signals")
        return now

    return last_fusion


def main():
    parser = argparse.ArgumentParser(description="Web3 Signals Orchestrator")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=3600, help="Sleep between cycles (seconds)")
    parser.add_argument("--db", default="signals.db", help="SQLite path")
    args = parser.parse_args()

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    config = load_config()
    assets_cfg = load_assets()
    storage = Storage(db_path=args.db)

    agents = _load_agents(config, assets_cfg)
    last_runs = {}
    last_fusion = 0.0

    if args.once:
        run_cycle(config, assets_cfg, storage, agents, last_runs, last_fusion, force=True)
        return

    while True:
        try:
            last_fusion = run_cycle(config, assets_cfg, storage, agents, last_runs, last_fusion)
        except Exception as e:
            logger.error(f"Cycle error: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
