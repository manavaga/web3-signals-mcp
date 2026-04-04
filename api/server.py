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
from api.middleware import (setup_x402, setup_cors, setup_usage_tracking_storage,
                           setup_proxy_scheme, get_cached_signals, set_cached_signals,
                           classify_user_agent, UsageTrackingMiddleware)

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
    setup_usage_tracking_storage(_storage)
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
setup_proxy_scheme(app)
app.add_middleware(UsageTrackingMiddleware)


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
    try:
        stats = _storage.load_accuracy_stats(days=days)
    except Exception as e:
        logger.error(f"Performance query error: {e}")
        stats = {"total": 0, "windows": {}, "error": str(e)}
    return {"days": days, "stats": stats}


@app.get("/api/signal", tags=["internal"])
def api_signal_mirror():
    return get_signals()


@app.get("/analytics", tags=["analytics"])
def get_analytics(days: int = Query(default=7, ge=1, le=90)):
    """API usage analytics — request counts, client types, endpoints, daily trends."""
    try:
        stats = _storage.load_api_analytics(days=days)
    except Exception as e:
        logger.error(f"Analytics query error: {e}")
        stats = {"total_requests": 0, "unique_ips": 0, "avg_duration_ms": 0, "by_endpoint": {}, "by_client_type": {}, "requests_per_day": {}, "by_source": {}}
    try:
        x402_stats = _storage.load_x402_analytics(days=days)
    except Exception as e:
        logger.error(f"x402 analytics error: {e}")
        x402_stats = {"total_paid_calls": 0, "total_402_challenges": 0, "estimated_revenue_usdc": 0}

    total_challenges = x402_stats.get("total_402_challenges", 0)
    total_paid = x402_stats.get("total_paid_calls", 0)

    return {
        "status": "active",
        "window_days": days,
        "total_requests": stats.get("total_requests", 0),
        "unique_clients": stats.get("unique_ips", 0),
        "avg_response_ms": stats.get("avg_duration_ms", 0),
        "by_endpoint": stats.get("by_endpoint", {}),
        "by_client_type": stats.get("by_client_type", {}),
        "requests_per_day": stats.get("requests_per_day", {}),
        "by_source": stats.get("by_source", {}),
        "x402_payments": {
            "total_paid_calls": total_paid,
            "estimated_revenue_usdc": x402_stats.get("estimated_revenue_usdc", 0),
            "total_402_challenges": total_challenges,
            "conversion_rate_pct": round(total_paid / total_challenges * 100, 1) if total_challenges > 0 else 0,
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/analytics/x402", tags=["analytics"])
def get_x402_analytics(days: int = Query(default=30, ge=1, le=90)):
    """x402 payment analytics — paid calls, revenue, conversion rate."""
    stats = _storage.load_x402_analytics(days=days)
    total_challenges = stats.get("total_402_challenges", 0)
    total_paid = stats.get("total_paid_calls", 0)
    price = float(os.getenv("SIGNAL_PRICE_USDC", "0.001"))

    return {
        "status": "active",
        "window_days": days,
        "x402_enabled": bool(os.getenv("PAY_TO")),
        "price_per_call": f"{price} USDC",
        "total_paid_calls": total_paid,
        "total_402_challenges": total_challenges,
        "total_payment_failures": stats.get("total_payment_failures", 0),
        "conversion_rate_pct": round(total_paid / total_challenges * 100, 1) if total_challenges > 0 else 0,
        "estimated_revenue_usdc": stats.get("estimated_revenue_usdc", 0),
        "by_endpoint": stats.get("by_endpoint", {}),
        "by_client_type": stats.get("by_client_type", {}),
        "paid_per_day": stats.get("paid_per_day", {}),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/analytics/agents", tags=["analytics"])
def get_agent_analytics(days: int = Query(default=7, ge=1, le=90)):
    """AI agent usage analytics — who's calling the API."""
    stats = _storage.load_api_analytics(days=days)
    client_types = stats.get("by_client_type", {})

    ai_agents = {k: v for k, v in client_types.items()
                 if k in ("claude", "openai", "google", "langchain", "crewai",
                          "autogpt", "mcp_client")}
    return {
        "window_days": days,
        "ai_agent_calls": sum(ai_agents.values()),
        "by_agent": ai_agents,
        "total_requests": stats.get("total_requests", 0),
        "ai_share_pct": round(
            sum(ai_agents.values()) / max(stats.get("total_requests", 1), 1) * 100, 1
        ),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/analytics/errors", tags=["analytics"])
def get_error_analytics(days: int = Query(default=7, ge=1, le=90)):
    """Error tracking — 5xx errors, payment failures."""
    errors = _storage.load_error_summary(days=days)
    return {"window_days": days, **errors}


@app.get("/analytics/ic", tags=["analytics"])
def get_ic_analytics():
    """Information Coefficient per scoring dimension (from shadow optimizer)."""
    shadow = _storage.load_kv_json("learning", "shadow_proposed_weights")
    if not shadow:
        return {"status": "no_data", "message": "Shadow optimizer has not run yet"}
    return {
        "status": "active",
        "ics": shadow.get("ics", {}),
        "proposed_weights": shadow.get("proposed", {}),
        "observations": shadow.get("observations", 0),
        "timestamp": shadow.get("timestamp"),
    }


@app.get("/dashboard", tags=["ui"])
def dashboard():
    try:
        from api.dashboard import DASHBOARD_HTML
        return HTMLResponse(DASHBOARD_HTML)
    except ImportError:
        return HTMLResponse("<h1>Dashboard not available</h1>")
