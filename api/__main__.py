# api/__main__.py
"""Entry point: python -m api — starts API + background orchestrator."""
import os
import logging
import threading
import time
import uvicorn


def _resolve_port() -> int:
    port = os.getenv("PORT", "8000")
    p = int(port)
    if p == 5432:
        return 8000
    return p


def _run_orchestrator_background():
    """Run the orchestrator loop in a background thread."""
    logger = logging.getLogger("orchestrator.background")
    time.sleep(10)  # let API start first

    try:
        from scoring.config import load_config, load_assets
        from orchestrator.runner import _load_agents, run_cycle
        from storage.db import Storage

        config = load_config()
        assets_cfg = load_assets()
        storage = Storage(db_path=os.getenv("DB_PATH", "signals.db"))
        agents = _load_agents(config, assets_cfg)
        last_runs = {}
        last_fusion = 0.0
        interval = int(os.getenv("ORCHESTRATOR_INTERVAL", "3600"))

        logger.info("Background orchestrator started (interval=%ds)", interval)

        # First run: force all agents
        last_fusion = run_cycle(config, assets_cfg, storage, agents,
                                last_runs, last_fusion, force=True)
        logger.info("Initial orchestrator cycle complete")

        while True:
            time.sleep(interval)
            try:
                last_fusion = run_cycle(config, assets_cfg, storage, agents,
                                        last_runs, last_fusion)
            except Exception as e:
                logger.error(f"Orchestrator cycle error: {e}")

    except Exception as e:
        logger.error(f"Orchestrator startup failed: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    # Start orchestrator in background thread
    if os.getenv("DISABLE_ORCHESTRATOR") != "1":
        t = threading.Thread(target=_run_orchestrator_background, daemon=True)
        t.start()

    port = _resolve_port()
    uvicorn.run("api.server:app", host="0.0.0.0", port=port, log_level="info")
