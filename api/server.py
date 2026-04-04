# api/server.py
"""FastAPI application — routes, lifecycle, x402."""
from __future__ import annotations
import os
import time
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from scoring.config import load_config, load_assets, AppConfig, AssetsConfig
from scoring.pipeline import fuse_signals
from storage.db import Storage
from api.middleware import setup_x402, setup_cors, get_cached_signals, set_cached_signals

logger = logging.getLogger(__name__)

_config: Optional[AppConfig] = None
_assets: Optional[AssetsConfig] = None
_storage: Optional[Storage] = None
_start_time: float = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _assets, _storage, _start_time
    _config = load_config()
    _assets = load_assets()
    _storage = Storage(db_path=os.getenv("DB_PATH", "signals.db"))
    _start_time = time.time()
    logger.info(f"Loaded config: {len(_assets.enabled_assets())} enabled assets")
    yield


app = FastAPI(
    title="Web3 Signals API",
    description="AI-powered crypto signal intelligence — 5 agents, 20 assets, x402 micropayments",
    version="1.0.0",
    lifespan=lifespan,
)

setup_cors(app)
setup_x402(app)


@app.get("/", tags=["info"])
def root():
    return {
        "name": "Web3 Signals API",
        "version": "1.0.0",
        "assets": _assets.enabled_assets() if _assets else [],
        "endpoints": ["/health", "/signal", "/signal/{asset}", "/performance"],
    }


@app.get("/health", tags=["info"])
def health():
    uptime = int(time.time() - _start_time)
    return {
        "status": "healthy",
        "uptime_seconds": uptime,
        "enabled_assets": len(_assets.enabled_assets()) if _assets else 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/signal", tags=["signals"])
def get_signals():
    cached = get_cached_signals()
    if cached:
        return cached

    agent_names = ["technical_agent", "derivatives_agent", "market_agent",
                   "narrative_agent", "exchange_flow_agent"]
    raw = _storage.load_all_latest(agent_names)

    agent_data = {}
    for name in ["technical", "derivatives", "market", "narrative", "exchange_flow"]:
        full_name = f"{name}_agent"
        agent_data[name] = raw.get(full_name) or {}

    signals = fuse_signals(agent_data, _config, _assets)

    response = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "regime": signals.get("BTC", next(iter(signals.values()))).regime.regime if signals else "unknown",
        "signals": {asset: sig.to_dict() for asset, sig in signals.items()},
    }
    set_cached_signals(response)
    return response


@app.get("/signal/{asset}", tags=["signals"])
def get_signal(asset: str):
    asset = asset.upper()
    if _assets and asset not in _assets.enabled_assets():
        raise HTTPException(404, f"Asset {asset} not found or disabled")

    all_signals = get_signals()
    sig = all_signals.get("signals", {}).get(asset)
    if not sig:
        raise HTTPException(404, f"No signal for {asset}")
    return {"asset": asset, "signal": sig, "timestamp": all_signals["timestamp"]}


@app.get("/performance", tags=["performance"])
def get_performance(days: int = Query(default=30, ge=1, le=90)):
    stats = _storage.load_accuracy_stats(days=days)
    return {"days": days, "stats": stats}


@app.get("/api/signal", tags=["internal"])
def api_signal_mirror():
    return get_signals()


@app.get("/dashboard", tags=["ui"])
def dashboard():
    try:
        from api.dashboard import DASHBOARD_HTML
        return HTMLResponse(DASHBOARD_HTML)
    except ImportError:
        return HTMLResponse("<h1>Dashboard not available</h1>")
