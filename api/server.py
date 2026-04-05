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
    pay_to = os.getenv("PAY_TO", "")
    price = os.getenv("SIGNAL_PRICE_USDC", "0.001")
    base_url = os.getenv("BASE_URL", "https://confident-empathy-production-fac6.up.railway.app")
    return {
        "name": "Web3 Signals API",
        "version": "2.0.0",
        "description": "AI-powered crypto signal intelligence — 3 agents, 20 assets, x402 micropayments",
        "assets": _assets.enabled_assets() if _assets else [],
        "agents": ["technical", "derivatives", "market"],
        "endpoints": {
            "signals": {
                "/signal": "All asset signals with dimensions, targets, regime",
                "/signal/{asset}": "Single asset signal (e.g. /signal/BTC)",
            },
            "analytics": {
                "/analytics": "API usage analytics — requests, clients, trends",
                "/analytics/x402": "x402 payment analytics — revenue, conversion",
                "/analytics/agents": "AI agent usage — who's calling the API",
                "/analytics/errors": "Error tracking — 5xx, payment failures",
                "/analytics/ic": "Information Coefficient per scoring dimension",
            },
            "performance": {
                "/performance": "Signal accuracy stats (TP/SL hit rates)",
            },
            "ui": {
                "/dashboard": "Live web dashboard (5 tabs)",
            },
            "discovery": {
                "/.well-known/agent.json": "AI agent discovery (OpenAI/Anthropic spec)",
                "/llms.txt": "LLM-readable API description",
                "/robots.txt": "Crawler directives",
            },
            "info": {
                "/": "This endpoint",
                "/health": "Health check + uptime",
            },
        },
        "x402": {
            "enabled": bool(pay_to),
            "price_per_call": f"{price} USDC",
            "network": "Base (EIP-155:8453)",
            "currency": "USDC",
            "paid_endpoints": ["/signal", "/signal/{asset}", "/performance/reputation"],
        },
        "links": {
            "dashboard": f"{base_url}/dashboard",
            "github": "https://github.com/manavaga/web3-signals-mcp",
            "agent_discovery": f"{base_url}/.well-known/agent.json",
        },
    }


@app.get("/health", tags=["info"])
def health():
    uptime = int(time.time() - _start_time)
    # Check agent data freshness
    agent_status = {}
    agent_names = ["technical_agent", "derivatives_agent", "market_agent"]
    try:
        raw = _storage.load_all_latest(agent_names) if _storage else {}
        for name in agent_names:
            data = raw.get(name)
            if data:
                ts = data.get("_timestamp", data.get("timestamp", ""))
                agent_status[name] = {"status": "active", "last_update": ts}
            else:
                agent_status[name] = {"status": "no_data"}
    except Exception:
        agent_status = {n: {"status": "unknown"} for n in agent_names}

    pay_to = os.getenv("PAY_TO", "")
    return {
        "status": "healthy",
        "uptime_seconds": uptime,
        "uptime_human": f"{uptime // 3600}h {(uptime % 3600) // 60}m",
        "enabled_assets": len(_assets.enabled_assets()) if _assets else 0,
        "agents": agent_status,
        "x402_enabled": bool(pay_to),
        "storage_backend": "postgres" if os.getenv("DATABASE_URL") else "sqlite",
        "orchestrator_enabled": os.getenv("DISABLE_ORCHESTRATOR") != "1",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/signal", tags=["signals"])
def get_signals():
    cached = get_cached_signals()
    if cached:
        return cached

    agent_names = ["technical_agent", "derivatives_agent", "market_agent"]
    raw = _storage.load_all_latest(agent_names)

    agent_data = {}
    for name in ["technical", "derivatives", "market"]:
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


@app.get("/api/trades", tags=["trades"])
def get_trades(days: int = Query(default=30, ge=1, le=180)):
    """Trade log and P&L statistics."""
    try:
        stats = _storage.load_trade_stats(days=days)
    except Exception as e:
        logger.error(f"Trades query error: {e}")
        stats = {"total_trades": 0, "error": str(e)}
    return {"days": days, **stats}


@app.get("/dashboard", tags=["ui"])
def dashboard():
    try:
        from api.dashboard import DASHBOARD_HTML
        return HTMLResponse(DASHBOARD_HTML)
    except ImportError:
        return HTMLResponse("<h1>Dashboard not available</h1>")


# ---------------------------------------------------------------------------
# Discovery endpoints
# ---------------------------------------------------------------------------

@app.get("/.well-known/agent.json", tags=["discovery"])
def agent_discovery():
    """AI agent discovery — OpenAI/Anthropic agent protocol."""
    base_url = os.getenv("BASE_URL", "https://confident-empathy-production-fac6.up.railway.app")
    price = os.getenv("SIGNAL_PRICE_USDC", "0.001")
    return {
        "name": "Web3 Signals",
        "description": "AI-powered crypto signal intelligence for 20 assets. "
                       "3 independent agents (technical, derivatives, market) "
                       "produce directional signals with target prices and stop losses. "
                       "Monetized via x402 micropayments ($0.001 USDC per call on Base).",
        "version": "2.0.0",
        "protocol": "http",
        "capabilities": ["crypto-signals", "market-analysis", "x402-payments"],
        "endpoints": {
            "signals": {"url": f"{base_url}/signal", "method": "GET",
                        "description": "Get all asset signals with dimensions, targets, regime",
                        "payment": {"amount": price, "currency": "USDC", "network": "Base"}},
            "signal_by_asset": {"url": f"{base_url}/signal/{{asset}}", "method": "GET",
                                "description": "Get signal for a specific asset (e.g. BTC, ETH, SOL)",
                                "payment": {"amount": price, "currency": "USDC", "network": "Base"}},
            "health": {"url": f"{base_url}/health", "method": "GET",
                       "description": "Health check — free, no payment required"},
            "performance": {"url": f"{base_url}/performance", "method": "GET",
                            "description": "Signal accuracy stats"},
            "analytics": {"url": f"{base_url}/analytics", "method": "GET",
                          "description": "API usage analytics"},
        },
        "assets": _assets.enabled_assets() if _assets else [],
        "authentication": {"type": "x402", "network": "eip155:8453", "currency": "USDC"},
        "contact": {"github": "https://github.com/manavaga/web3-signals-mcp"},
    }


@app.get("/llms.txt", tags=["discovery"], response_class=HTMLResponse)
def llms_txt():
    """LLM-readable API description."""
    base_url = os.getenv("BASE_URL", "https://confident-empathy-production-fac6.up.railway.app")
    assets = ", ".join(_assets.enabled_assets()) if _assets else "BTC, ETH, SOL, ..."
    return f"""# Web3 Signals API
> AI-powered crypto signal intelligence for 20 assets

## What This API Does
Produces directional trading signals (BUY/SELL/NEUTRAL) for crypto assets by fusing data from 3 independent AI agents: technical analysis, derivatives data, and market metrics. Each signal includes target prices, stop losses, confidence levels, and dimensional breakdowns.

## Endpoints

### Free Endpoints
- GET {base_url}/health — Health check, uptime, agent status
- GET {base_url}/analytics — API usage analytics
- GET {base_url}/dashboard — Web dashboard (browser)

### Paid Endpoints (x402 — $0.001 USDC on Base)
- GET {base_url}/signal — All 20 asset signals
- GET {base_url}/signal/BTC — Single asset signal
- GET {base_url}/performance — Signal accuracy stats

## Assets Tracked
{assets}

## Payment
Uses x402 protocol. Send $0.001 USDC on Base network per API call.
Payment header: x-payment or payment-signature

## Agent Discovery
GET {base_url}/.well-known/agent.json
"""


@app.get("/robots.txt", tags=["discovery"], response_class=HTMLResponse)
def robots_txt():
    """Crawler directives."""
    base_url = os.getenv("BASE_URL", "https://confident-empathy-production-fac6.up.railway.app")
    return f"""User-agent: *
Allow: /
Allow: /health
Allow: /dashboard
Allow: /.well-known/
Allow: /llms.txt

Sitemap: {base_url}/health
"""
