"""
Web3 Signals API — FastAPI server.

Endpoints:
    GET /                  Welcome + links
    GET /health            Agent status, last run, uptime
    GET /signal            Full fusion (portfolio + 20 signals + LLM insights)
    GET /signal/{asset}    Single asset signal
    GET /performance       Signal accuracy tracking
    GET /performance/{asset}  Per-asset accuracy
    GET /docs              Auto-generated OpenAPI docs
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from shared.storage import Storage
from signal_fusion.engine import SignalFusion

# ---------------------------------------------------------------------------
# Globals — set on startup
# ---------------------------------------------------------------------------
_store: Optional[Storage] = None
_fusion: Optional[SignalFusion] = None
_cached_result: Optional[Dict[str, Any]] = None
_cache_timestamp: Optional[str] = None
_boot_time: Optional[str] = None
_orchestrator_thread: Optional[threading.Thread] = None
_orchestrator_running = False

CACHE_TTL_SEC = int(os.getenv("CACHE_TTL_SEC", "300"))  # 5 min default


# ---------------------------------------------------------------------------
# Background orchestrator — runs all agents every N seconds
# ---------------------------------------------------------------------------
def _orchestrator_loop(store: Storage, interval: int) -> None:
    """Background thread: run all 5 agents + save to storage."""
    global _orchestrator_running

    # Delay first run by 5 seconds to let the server boot
    time.sleep(5)

    while _orchestrator_running:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{ts}] Orchestrator: starting agent run...")

        agents = []

        try:
            from technical_agent.engine import TechnicalAgent
            agents.append(("technical_agent", TechnicalAgent))
        except ImportError as e:
            print(f"  technical_agent: import error — {e}")

        try:
            from derivatives_agent.engine import DerivativesAgent
            agents.append(("derivatives_agent", DerivativesAgent))
        except ImportError as e:
            print(f"  derivatives_agent: import error — {e}")

        try:
            from market_agent.engine import MarketAgent
            agents.append(("market_agent", MarketAgent))
        except ImportError as e:
            print(f"  market_agent: import error — {e}")

        try:
            from narrative_agent.engine import NarrativeAgent
            agents.append(("narrative_agent", NarrativeAgent))
        except ImportError as e:
            print(f"  narrative_agent: import error — {e}")

        try:
            from whale_agent.engine import WhaleAgent
            agents.append(("whale_agent", WhaleAgent))
        except ImportError as e:
            print(f"  whale_agent: import error — {e}")

        for name, factory in agents:
            try:
                agent = factory()
                result = agent.execute()
                store.save(name, result)
                status = result["status"]
                ms = result["meta"]["duration_ms"]
                errs = len(result["meta"]["errors"])
                print(f"  {name}: {status} ({ms}ms, {errs} errors)")
            except Exception as exc:
                print(f"  {name}: CRASH — {exc}")

        # Record performance snapshot for accuracy tracking
        try:
            _record_performance_snapshot(store)
        except Exception as exc:
            print(f"  performance snapshot: {exc}")

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{ts}] Orchestrator: done. Sleeping {interval}s.\n")

        # Sleep in small increments so we can stop quickly
        for _ in range(interval):
            if not _orchestrator_running:
                return
            time.sleep(1)


def _record_performance_snapshot(store: Storage) -> None:
    """
    After agents run, record current prices + signal scores for later accuracy evaluation.
    Uses market_agent prices + fusion scores.
    """
    market = store.load_latest("market_agent")
    fusion = store.load_latest("signal_fusion")
    if not market or not fusion:
        return

    per_asset = market.get("data", {}).get("per_asset", {})
    signals = fusion.get("data", {}).get("signals", {})
    now = datetime.now(timezone.utc).isoformat()

    for asset, price_data in per_asset.items():
        price = price_data.get("price")
        sig = signals.get(asset, {})
        score = sig.get("composite_score")
        label = sig.get("label")
        direction = sig.get("direction")

        if price is not None and score is not None:
            # Store as a JSON blob in kv store
            import json
            snapshot = json.dumps({
                "price": price,
                "score": score,
                "label": label,
                "direction": direction,
                "timestamp": now,
            })
            # Use timestamp-based key so we can query history
            store.save_kv("perf_snapshots", f"{asset}:{now}", price)
            store.save_kv("perf_scores", f"{asset}:{now}", score)


# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store, _fusion, _boot_time, _orchestrator_thread, _orchestrator_running

    _boot_time = datetime.now(timezone.utc).isoformat()
    _store = Storage()
    _fusion = SignalFusion()

    # Start background orchestrator
    interval = int(os.getenv("ORCHESTRATOR_INTERVAL_SEC", "900"))  # 15 min
    _orchestrator_running = True
    _orchestrator_thread = threading.Thread(
        target=_orchestrator_loop,
        args=(_store, interval),
        daemon=True,
        name="orchestrator",
    )
    _orchestrator_thread.start()
    print(f"Orchestrator started (interval={interval}s)")

    yield

    # Shutdown
    _orchestrator_running = False
    if _orchestrator_thread:
        _orchestrator_thread.join(timeout=5)
    print("Orchestrator stopped")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Web3 Signals API",
    description=(
        "AI-powered crypto signal intelligence for 20 assets. "
        "Fuses whale activity, derivatives positioning, technical analysis, "
        "narrative momentum, and market data into scored signals with LLM insights."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------
@app.get("/", tags=["info"])
async def root():
    return {
        "name": "Web3 Signals API",
        "version": "0.1.0",
        "description": "AI-powered crypto signal intelligence for 20 assets",
        "endpoints": {
            "/health": "Agent status and uptime",
            "/signal": "Full fusion — portfolio + 20 signals + LLM insights",
            "/signal/{asset}": "Single asset signal (e.g. /signal/BTC)",
            "/performance": "Signal accuracy tracking",
            "/docs": "OpenAPI documentation",
        },
        "assets": [
            "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT",
            "MATIC", "LINK", "UNI", "ATOM", "LTC", "FIL", "NEAR", "APT",
            "ARB", "OP", "INJ", "SUI",
        ],
        "data_sources": [
            "Whale tracking (Twitter + Etherscan + Blockchain.com + exchange flow)",
            "Technical analysis (RSI, MACD, MA via Binance)",
            "Derivatives (Long/Short ratio, funding rate, OI via Binance Futures)",
            "Narrative momentum (Twitter + Reddit + News + CoinGecko Trending)",
            "Market data (Price, Volume, Fear & Greed, DexScreener)",
        ],
    }


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------
@app.get("/health", tags=["info"])
async def health():
    agent_names = ["technical_agent", "derivatives_agent", "market_agent", "narrative_agent", "whale_agent"]
    agent_status = {}

    for name in agent_names:
        latest = _store.load_latest(name) if _store else None
        if latest:
            agent_status[name] = {
                "status": latest.get("status", "unknown"),
                "last_run": latest.get("timestamp"),
                "duration_ms": latest.get("meta", {}).get("duration_ms"),
                "errors": len(latest.get("meta", {}).get("errors", [])),
            }
        else:
            agent_status[name] = {"status": "no_data", "last_run": None}

    # Fusion status
    fusion_latest = _store.load_latest("signal_fusion") if _store else None
    fusion_status = {
        "status": fusion_latest.get("status") if fusion_latest else "no_data",
        "last_run": fusion_latest.get("timestamp") if fusion_latest else None,
    }

    return {
        "status": "healthy",
        "boot_time": _boot_time,
        "storage_backend": _store.backend if _store else "none",
        "agents": agent_status,
        "fusion": fusion_status,
    }


# ---------------------------------------------------------------------------
# GET /signal — Full fusion output
# ---------------------------------------------------------------------------
@app.get("/signal", tags=["signals"])
async def get_signal():
    global _cached_result, _cache_timestamp

    # Check cache
    if _cached_result and _cache_timestamp:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(_cache_timestamp)).total_seconds()
        if age < CACHE_TTL_SEC:
            return _cached_result

    # Run fusion on latest agent data
    if not _fusion:
        raise HTTPException(status_code=503, detail="Fusion engine not initialized")

    result = _fusion.fuse()

    # Cache the result
    _cached_result = result
    _cache_timestamp = datetime.now(timezone.utc).isoformat()

    return result


# ---------------------------------------------------------------------------
# GET /signal/{asset} — Single asset
# ---------------------------------------------------------------------------
@app.get("/signal/{asset}", tags=["signals"])
async def get_asset_signal(asset: str):
    asset = asset.upper()

    # Get the full fusion (cached if possible)
    full = await get_signal()

    signals = full.get("data", {}).get("signals", {})
    if asset not in signals:
        valid = list(signals.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Asset '{asset}' not found. Valid assets: {valid}",
        )

    sig = signals[asset]
    portfolio = full.get("data", {}).get("portfolio_summary", {})

    return {
        "asset": asset,
        "timestamp": full.get("timestamp"),
        "signal": sig,
        "market_context": {
            "regime": portfolio.get("market_regime"),
            "risk_level": portfolio.get("risk_level"),
            "signal_momentum": portfolio.get("signal_momentum"),
        },
    }


# ---------------------------------------------------------------------------
# GET /performance — Accuracy tracking
# ---------------------------------------------------------------------------
@app.get("/performance", tags=["performance"])
async def get_performance():
    """
    Signal accuracy: how well did our signals predict price moves?
    Needs at least 24h of data to start showing results.
    """
    if not _store:
        raise HTTPException(status_code=503, detail="Storage not initialized")

    # Load recent fusion runs (last 7 days)
    recent_fusions = _store.load_recent("signal_fusion", days=7)

    if len(recent_fusions) < 2:
        return {
            "status": "insufficient_data",
            "message": "Need at least 24h of signal history to calculate accuracy. Check back later.",
            "total_fusion_runs": len(recent_fusions),
            "data_collection_started": recent_fusions[0].get("timestamp") if recent_fusions else None,
        }

    # Evaluate accuracy: for each old signal, check if direction was correct
    # by comparing price at signal time vs current price from latest market data
    market_latest = _store.load_latest("market_agent")
    if not market_latest:
        return {"status": "no_market_data", "message": "Waiting for market data."}

    current_prices = {}
    for asset, data in market_latest.get("data", {}).get("per_asset", {}).items():
        current_prices[asset] = data.get("price", 0)

    # Use the oldest available fusion run as the "prediction"
    # and current prices as the "outcome"
    oldest_run = recent_fusions[-1]  # oldest (list is DESC)
    oldest_signals = oldest_run.get("data", {}).get("signals", {})
    oldest_prices = {}

    # Get prices at the time of that signal from the market snapshot closest to it
    oldest_market = None
    market_runs = _store.load_recent("market_agent", days=7)
    if market_runs:
        oldest_market = market_runs[-1]  # oldest market snapshot
        for asset, data in oldest_market.get("data", {}).get("per_asset", {}).items():
            oldest_prices[asset] = data.get("price", 0)

    results = {}
    correct_count = 0
    total_count = 0

    for asset, sig in oldest_signals.items():
        old_price = oldest_prices.get(asset, 0)
        new_price = current_prices.get(asset, 0)
        if old_price <= 0 or new_price <= 0:
            continue

        price_change_pct = ((new_price - old_price) / old_price) * 100
        direction = sig.get("direction", "neutral")
        score = sig.get("composite_score", 50)

        # Was the signal correct?
        if direction == "buy" and price_change_pct > 0:
            correct = True
        elif direction == "sell" and price_change_pct < 0:
            correct = True
        elif direction == "neutral" and abs(price_change_pct) < 3:
            correct = True
        else:
            correct = False

        if direction != "neutral":
            total_count += 1
            if correct:
                correct_count += 1

        results[asset] = {
            "signal_score": score,
            "signal_direction": direction,
            "signal_label": sig.get("label"),
            "price_at_signal": round(old_price, 2),
            "price_now": round(new_price, 2),
            "price_change_pct": round(price_change_pct, 2),
            "was_correct": correct,
        }

    accuracy = round((correct_count / total_count) * 100, 1) if total_count > 0 else None

    return {
        "status": "active",
        "accuracy_pct": accuracy,
        "signals_evaluated": total_count,
        "signals_correct": correct_count,
        "evaluation_window": {
            "signal_time": oldest_run.get("timestamp"),
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        },
        "total_fusion_runs_7d": len(recent_fusions),
        "per_asset": results,
    }


# ---------------------------------------------------------------------------
# GET /performance/{asset}
# ---------------------------------------------------------------------------
@app.get("/performance/{asset}", tags=["performance"])
async def get_asset_performance(asset: str):
    asset = asset.upper()
    full_perf = await get_performance()

    if full_perf.get("status") == "insufficient_data":
        return full_perf

    per_asset = full_perf.get("per_asset", {})
    if asset not in per_asset:
        raise HTTPException(status_code=404, detail=f"No performance data for '{asset}'")

    return {
        "asset": asset,
        "overall_accuracy_pct": full_perf.get("accuracy_pct"),
        "evaluation_window": full_perf.get("evaluation_window"),
        **per_asset[asset],
    }


# ---------------------------------------------------------------------------
# A2A Agent Card — /.well-known/agent.json
# ---------------------------------------------------------------------------
@app.get("/.well-known/agent.json", tags=["discovery"], include_in_schema=False)
async def agent_card():
    """Agent-to-Agent discovery card (Google A2A protocol)."""
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    return {
        "name": "Web3 Signals Agent",
        "description": (
            "AI-powered crypto signal intelligence. Fuses whale tracking, "
            "derivatives positioning, technical analysis, narrative momentum, "
            "and market data into scored signals for 20 crypto assets. "
            "Includes LLM-generated cross-dimensional insights."
        ),
        "url": base_url,
        "version": "0.1.0",
        "capabilities": [
            {
                "name": "get_all_signals",
                "description": "Get scored signals for all 20 crypto assets with portfolio summary and LLM insights",
                "endpoint": f"{base_url}/signal",
                "method": "GET",
            },
            {
                "name": "get_asset_signal",
                "description": "Get signal for a specific crypto asset (e.g. BTC, ETH, SOL)",
                "endpoint": f"{base_url}/signal/{{asset}}",
                "method": "GET",
            },
            {
                "name": "get_performance",
                "description": "Get signal accuracy tracking — how well past signals predicted price moves",
                "endpoint": f"{base_url}/performance",
                "method": "GET",
            },
            {
                "name": "health_check",
                "description": "Check agent status, data freshness, and uptime",
                "endpoint": f"{base_url}/health",
                "method": "GET",
            },
        ],
        "assets_covered": [
            "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT",
            "MATIC", "LINK", "UNI", "ATOM", "LTC", "FIL", "NEAR", "APT",
            "ARB", "OP", "INJ", "SUI",
        ],
        "update_frequency": "Every 15 minutes",
        "pricing": "Free (x402 micropayments coming soon)",
    }


# ---------------------------------------------------------------------------
# GET /debug/db — Diagnose database connectivity (temporary)
# ---------------------------------------------------------------------------
@app.get("/debug/db", tags=["debug"], include_in_schema=False)
async def debug_db():
    import socket
    results = {"DATABASE_URL_set": bool(os.getenv("DATABASE_URL"))}

    # DNS resolution tests
    dns_tests = {}
    for host in ["postgres", "postgres.railway.internal", "Postgres", "Postgres.railway.internal"]:
        try:
            ip = socket.gethostbyname(host)
            dns_tests[host] = ip
        except Exception as e:
            dns_tests[host] = f"FAILED: {e}"
    results["dns_resolution"] = dns_tests

    # Actual DB connection test
    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        results["database_url_host"] = db_url.split("@")[1].split("/")[0] if "@" in db_url else "parse_error"
        try:
            import psycopg2
            conn = psycopg2.connect(db_url, connect_timeout=5)
            cur = conn.cursor()
            cur.execute("SELECT version()")
            ver = cur.fetchone()[0]
            conn.close()
            results["connection"] = "SUCCESS"
            results["pg_version"] = ver
        except Exception as e:
            results["connection"] = f"FAILED: {e}"
    else:
        results["connection"] = "No DATABASE_URL — using SQLite"

    # Check all env vars containing RAILWAY or PG
    railway_vars = {k: v for k, v in os.environ.items() if "RAILWAY" in k or "PG" in k or "DATABASE" in k}
    results["railway_env_vars"] = railway_vars

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("api.server:app", host="0.0.0.0", port=port, reload=False)
