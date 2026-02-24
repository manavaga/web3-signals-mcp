"""
Web3 Signals API — FastAPI server.

Endpoints:
    GET /                           Welcome + links
    GET /health                     Agent status, last run, uptime
    GET /signal                     Full fusion (portfolio + 20 signals + LLM insights)
    GET /signal/{asset}             Single asset signal
    GET /performance/reputation     Public reputation score (30-day rolling accuracy)
    GET /performance/{asset}        Per-asset accuracy breakdown
    GET /analytics                  API usage analytics (user-agents, requests/day)
    GET /.well-known/agent.json     A2A agent discovery card
    GET /.well-known/agents.md      AGENTS.md (Agentic AI Foundation standard)
    GET /mcp/sse                    MCP SSE transport for remote AI agents
    GET /docs                       Auto-generated OpenAPI docs
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

from shared.storage import Storage
from signal_fusion.engine import SignalFusion
from api.dashboard import DASHBOARD_HTML

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

        # Run signal fusion and save to storage (pre-computes so /signal is instant)
        try:
            fusion = SignalFusion()
            fusion_result = fusion.fuse()
            store.save("signal_fusion", fusion_result)
            f_status = fusion_result.get("status", "unknown")
            f_ms = fusion_result.get("meta", {}).get("duration_ms", 0)
            print(f"  signal_fusion: {f_status} ({f_ms}ms)")
        except Exception as exc:
            print(f"  signal_fusion: CRASH — {exc}")

        # --- 12-hour LLM Sentiment Cycle ---
        # Runs narrative LLM sentiment analysis every 12 hours (not every 15 min)
        # to keep costs low (~$0.02/day vs $3-5/day at 15 min intervals)
        try:
            llm_cycle_hours = int(os.getenv("LLM_SENTIMENT_CYCLE_HOURS", "12"))
            last_llm_run = store.load_kv("llm_cycle", "last_run")
            now_ts = time.time()

            should_run_llm = False
            if last_llm_run is None:
                should_run_llm = True
            elif (now_ts - last_llm_run) >= llm_cycle_hours * 3600:
                should_run_llm = True

            if should_run_llm:
                print(f"  [LLM] Running 12-hour narrative sentiment analysis...")
                try:
                    from narrative_agent.engine import NarrativeAgent
                    narrator = NarrativeAgent()
                    llm_result = narrator.run_llm_sentiment(store)
                    store.save_kv("llm_cycle", "last_run", now_ts)
                    print(f"  [LLM] Done: {llm_result}")
                except Exception as llm_exc:
                    print(f"  [LLM] Error: {llm_exc}")
        except Exception as exc:
            print(f"  llm_cycle: {exc}")

        # Record performance snapshot for accuracy tracking
        try:
            _record_performance_snapshot(store)
        except Exception as exc:
            print(f"  performance snapshot: {exc}")

        # Evaluate old snapshots for accuracy (every 4 hours)
        try:
            eval_interval_hours = int(os.getenv("PERF_EVAL_INTERVAL_HOURS", "4"))
            last_eval = store.load_kv("perf_eval", "last_run")
            now_ts = time.time()
            should_eval = False
            if last_eval is None:
                should_eval = True
            elif (now_ts - last_eval) >= eval_interval_hours * 3600:
                should_eval = True

            if should_eval:
                print(f"  [PERF] Running accuracy evaluation...")
                _evaluate_old_snapshots(store)
                store.save_kv("perf_eval", "last_run", now_ts)
        except Exception as exc:
            print(f"  performance eval: {exc}")

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{ts}] Orchestrator: done. Sleeping {interval}s.\n")

        # Sleep in small increments so we can stop quickly
        for _ in range(interval):
            if not _orchestrator_running:
                return
            time.sleep(1)


def _record_performance_snapshot(store: Storage) -> None:
    """
    Record performance snapshots — max 1 per asset per 12 hours.
    This avoids inflating sample size with near-identical correlated snapshots.
    Direction is derived from composite_score: >60 bullish, <40 bearish, else neutral.
    """
    import re as _re

    # Check if we should snapshot (1 per 12 hours)
    snapshot_interval = int(os.getenv("PERF_SNAPSHOT_INTERVAL_HOURS", "12"))
    last_snapshot_ts = store.load_kv("perf_snapshot", "last_run")
    now_ts = time.time()
    if last_snapshot_ts is not None and (now_ts - last_snapshot_ts) < snapshot_interval * 3600:
        return  # Too soon, skip

    market = store.load_latest("market_agent")
    fusion = store.load_latest("signal_fusion")
    if not market or not fusion:
        return

    per_asset = market.get("data", {}).get("per_asset", {})
    signals = fusion.get("data", {}).get("signals", {})

    saved = 0
    for asset, price_data in per_asset.items():
        price = price_data.get("price")
        sig = signals.get(asset, {})
        score = sig.get("composite_score")
        if price is None or score is None:
            continue

        # Derive direction from score threshold
        if score > 60:
            direction = "bullish"
        elif score < 40:
            direction = "bearish"
        else:
            direction = "neutral"

        # Count sources from narrative dimension
        narrative_dim = sig.get("dimensions", {}).get("narrative", {})
        detail = narrative_dim.get("detail", "")
        sources = 0
        m = _re.search(r"(\d+)\s+sources", detail)
        if m:
            sources = int(m.group(1))

        # Build detail string from all dimensions
        dim_details = []
        for dim_name, dim_data in sig.get("dimensions", {}).items():
            d = dim_data.get("detail", "")
            if d and d not in ("no data", "no scorer"):
                dim_details.append(f"{dim_name}: {d}")
        full_detail = "; ".join(dim_details) if dim_details else ""

        store.save_performance_snapshot(
            asset=asset,
            signal_score=score,
            signal_direction=direction,
            price_at_signal=price,
            sources_count=sources,
            detail=full_detail,
        )
        saved += 1

    if saved:
        store.save_kv("perf_snapshot", "last_run", now_ts)
        print(f"  performance: saved {saved} snapshots (next in {snapshot_interval}h)")


def _evaluate_old_snapshots(store: Storage) -> None:
    """
    Check snapshots that are 24h/48h/7d old and evaluate accuracy.
    Fetches current prices from CoinGecko (free, no key needed).
    """
    import json
    from urllib.request import Request, urlopen

    # CoinGecko asset ID mapping
    COINGECKO_IDS = {
        "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin",
        "XRP": "ripple", "ADA": "cardano", "AVAX": "avalanche-2", "DOT": "polkadot",
        "MATIC": "matic-network", "LINK": "chainlink", "UNI": "uniswap", "ATOM": "cosmos",
        "LTC": "litecoin", "FIL": "filecoin", "NEAR": "near", "APT": "aptos",
        "ARB": "arbitrum", "OP": "optimism", "INJ": "injective-protocol", "SUI": "sui",
    }

    # Fetch current prices for all assets in one call
    ids_str = ",".join(COINGECKO_IDS.values())
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids_str}&vs_currencies=usd"
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=15) as resp:
            price_data = json.loads(resp.read().decode())
    except Exception as exc:
        print(f"  performance eval: CoinGecko fetch failed — {exc}")
        return

    # Build asset → price map
    current_prices = {}
    for asset, cg_id in COINGECKO_IDS.items():
        p = price_data.get(cg_id, {}).get("usd")
        if p is not None:
            current_prices[asset] = float(p)

    if not current_prices:
        print("  performance eval: no prices from CoinGecko")
        return

    # Evaluate each window: 24h, 48h, 7d (168h)
    windows = [(24, 24), (48, 48), (168, 168)]
    total_evaluated = 0

    for window_hours, min_age in windows:
        snapshots = store.load_unevaluated_snapshots(window_hours, min_age)
        if not snapshots:
            continue

        for snap in snapshots:
            asset = snap["asset"]
            price_now = current_prices.get(asset)
            if price_now is None:
                continue

            price_at_signal = snap["price_at_signal"]
            direction = snap["signal_direction"]

            # Calculate accuracy
            pct_change = (price_now - price_at_signal) / price_at_signal * 100

            if direction == "bullish":
                hit = pct_change > 0
            elif direction == "bearish":
                hit = pct_change < 0
            else:  # neutral
                hit = abs(pct_change) <= 2.0

            store.save_performance_accuracy(
                snapshot_id=snap["id"],
                window_hours=window_hours,
                price_at_window=price_now,
                direction_correct=hit,
            )
            total_evaluated += 1

    if total_evaluated:
        print(f"  performance eval: evaluated {total_evaluated} snapshots")


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
# Usage Tracking Middleware
# ---------------------------------------------------------------------------
class UsageTrackingMiddleware(BaseHTTPMiddleware):
    """Logs every API request for analytics — user-agent, endpoint, duration."""

    # Skip tracking for static/noisy paths
    SKIP_PATHS = {"/favicon.ico", "/openapi.json"}

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration_ms = (time.time() - start) * 1000

        path = request.url.path
        if path in self.SKIP_PATHS:
            return response

        # Fire-and-forget: don't slow down the response
        try:
            if _store:
                ua = request.headers.get("user-agent", "")
                client_ip = request.client.host if request.client else ""
                _store.save_api_request(
                    endpoint=path,
                    method=request.method,
                    user_agent=ua,
                    status_code=response.status_code,
                    duration_ms=round(duration_ms, 1),
                    client_ip=client_ip,
                )
        except Exception:
            pass  # Never break the response for tracking

        return response


app.add_middleware(UsageTrackingMiddleware)


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
            "/dashboard": "Live signal intelligence dashboard (open in browser)",
            "/health": "Agent status and uptime",
            "/signal": "Full fusion — portfolio + 20 signals + LLM insights",
            "/signal/{asset}": "Single asset signal (e.g. /signal/BTC)",
            "/performance/reputation": "Public reputation score — 30-day signal accuracy",
            "/performance/{asset}": "Per-asset accuracy breakdown",
            "/analytics": "API usage analytics — who's using us, request trends",
            "/api/history": "Paginated history of all agent runs",
            "/.well-known/agent.json": "A2A agent discovery card (Google A2A protocol)",
            "/.well-known/agents.md": "AGENTS.md — Agentic AI Foundation discovery",
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

    # 1. Check in-memory cache (instant)
    if _cached_result and _cache_timestamp:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(_cache_timestamp)).total_seconds()
        if age < CACHE_TTL_SEC:
            return _cached_result

    # 2. Try loading pre-computed fusion from storage (fast, ~10ms)
    #    The orchestrator runs fusion every 15 min and saves it to Postgres.
    if _store:
        stored = _store.load_latest("signal_fusion")
        if stored:
            _cached_result = stored
            _cache_timestamp = datetime.now(timezone.utc).isoformat()
            return stored

    # 3. Fallback: compute live (slow — only runs on very first request before
    #    orchestrator has completed its first cycle)
    if not _fusion:
        raise HTTPException(status_code=503, detail="Fusion engine not initialized")

    result = _fusion.fuse()

    # Cache and save the live result
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
# GET /performance/reputation — Public reputation score (agent-facing)
# ---------------------------------------------------------------------------
@app.get("/performance/reputation", tags=["performance"])
async def get_reputation():
    """
    Public reputation endpoint. AI agents use this to verify signal quality
    before subscribing. Shows rolling 30-day accuracy across all timeframes.
    """
    if not _store:
        raise HTTPException(status_code=503, detail="Storage not initialized")

    stats = _store.load_accuracy_stats(days=30)
    total_snapshots = _store.count_snapshots(days=30)

    if stats["total"] == 0:
        return {
            "status": "collecting_data",
            "message": "Performance tracking is active. Accuracy data will appear after 24h of signal history.",
            "snapshots_collected": total_snapshots,
            "started_tracking": datetime.now(timezone.utc).isoformat(),
        }

    accuracy = round(stats["hits"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0
    reputation_score = int(round(accuracy))

    return {
        "status": "active",
        "reputation_score": reputation_score,
        "accuracy_30d": accuracy,
        "signals_evaluated": stats["total"],
        "signals_correct": stats["hits"],
        "signals_wrong": stats["total"] - stats["hits"],
        "by_timeframe": stats["by_timeframe"],
        "by_asset": stats["by_asset"],
        "snapshots_collected_30d": total_snapshots,
        "methodology": {
            "direction_extraction": "score >60 = bullish, <40 = bearish, 40-60 = neutral",
            "neutral_threshold": "price move ≤2% = correct for neutral signals",
            "scoring": "binary (hit/miss)",
            "window": "30-day rolling",
            "timeframes": ["24h", "48h", "7d"],
            "price_source": "CoinGecko",
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /performance — Accuracy overview
# ---------------------------------------------------------------------------
@app.get("/performance", tags=["performance"])
async def get_performance():
    """Redirects to /performance/reputation for backward compatibility."""
    return await get_reputation()


# ---------------------------------------------------------------------------
# GET /performance/{asset} — Per-asset accuracy
# ---------------------------------------------------------------------------
@app.get("/performance/{asset}", tags=["performance"])
async def get_asset_performance(asset: str):
    """Per-asset accuracy breakdown."""
    asset = asset.upper()
    if not _store:
        raise HTTPException(status_code=503, detail="Storage not initialized")

    stats = _store.load_accuracy_stats(days=30)

    if stats["total"] == 0:
        return {
            "status": "collecting_data",
            "message": "Performance tracking is active. Check back after 24h.",
        }

    asset_accuracy = stats["by_asset"].get(asset)
    if asset_accuracy is None:
        valid = list(stats["by_asset"].keys())
        raise HTTPException(
            status_code=404,
            detail=f"No accuracy data for '{asset}'. Assets with data: {valid}",
        )

    overall = round(stats["hits"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0

    return {
        "asset": asset,
        "accuracy_30d": asset_accuracy,
        "overall_accuracy_30d": overall,
        "reputation_score": int(round(overall)),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /analytics — API usage analytics (public)
# ---------------------------------------------------------------------------
@app.get("/analytics", tags=["analytics"])
async def get_analytics(days: int = Query(7, ge=1, le=90, description="Number of days to aggregate")):
    """
    Public API usage analytics. Shows request counts, user-agent breakdown
    (AI agents vs browsers vs bots), endpoint popularity, and daily trends.
    """
    if not _store:
        raise HTTPException(status_code=503, detail="Storage not initialized")

    stats = _store.load_api_analytics(days=days)

    return {
        "status": "active",
        "window_days": days,
        "total_requests": stats["total_requests"],
        "unique_clients": stats["unique_ips"],
        "avg_response_ms": stats["avg_duration_ms"],
        "by_endpoint": stats["by_endpoint"],
        "by_client_type": stats["by_user_agent_type"],
        "requests_per_day": stats["requests_per_day"],
        "top_user_agents": stats["top_user_agents"],
        "last_updated": datetime.now(timezone.utc).isoformat(),
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
                "name": "get_reputation",
                "description": "Get public reputation score — rolling 30-day signal accuracy across 24h/48h/7d timeframes, per-asset breakdown",
                "endpoint": f"{base_url}/performance/reputation",
                "method": "GET",
            },
            {
                "name": "get_analytics",
                "description": "API usage analytics — request counts, client types, daily trends",
                "endpoint": f"{base_url}/analytics",
                "method": "GET",
            },
            {
                "name": "health_check",
                "description": "Check agent status, data freshness, and uptime",
                "endpoint": f"{base_url}/health",
                "method": "GET",
            },
        ],
        "protocols": {
            "rest": f"{base_url}/docs",
            "mcp_sse": f"{base_url}/mcp/sse",
            "a2a": f"{base_url}/.well-known/agent.json",
            "agents_md": f"{base_url}/.well-known/agents.md",
        },
        "assets_covered": [
            "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT",
            "MATIC", "LINK", "UNI", "ATOM", "LTC", "FIL", "NEAR", "APT",
            "ARB", "OP", "INJ", "SUI",
        ],
        "update_frequency": "Every 15 minutes",
        "pricing": "Free (x402 micropayments coming soon)",
    }


# ---------------------------------------------------------------------------
# AGENTS.md — Agentic AI Foundation discovery
# ---------------------------------------------------------------------------
_AGENTS_MD = """# Web3 Signals Agent

## Identity
- **Name**: Web3 Signals Agent
- **Description**: AI-powered crypto signal intelligence for 20 assets. Fuses whale tracking, derivatives positioning, technical analysis, narrative momentum, and market data into scored signals with LLM insights.
- **Version**: 0.1.0
- **Provider**: Web3 Signals

## Capabilities
- Provides composite buy/sell/neutral signals for 20 crypto assets
- Portfolio-level risk assessment and market regime detection
- LLM-generated cross-dimensional insights
- Signal accuracy tracking with rolling 30-day reputation score
- Historical signal data with full audit trail

## Protocols
- **REST API**: OpenAPI-documented endpoints at /docs
- **MCP**: Model Context Protocol server (SSE transport at /mcp/sse)
- **A2A**: Agent-to-Agent discovery card at /.well-known/agent.json

## Endpoints
| Endpoint | Method | Description | Auth |
|----------|--------|-------------|------|
| /signal | GET | All 20 asset signals with portfolio summary | None |
| /signal/{asset} | GET | Single asset signal (e.g. /signal/BTC) | None |
| /performance/reputation | GET | 30-day rolling accuracy score | None |
| /performance/{asset} | GET | Per-asset accuracy breakdown | None |
| /health | GET | Agent status and uptime | None |
| /analytics | GET | API usage analytics | None |
| /api/history | GET | Historical signal runs (paginated) | None |

## Assets Covered
BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, MATIC, LINK, UNI, ATOM, LTC, FIL, NEAR, APT, ARB, OP, INJ, SUI

## Data Sources
1. Whale tracking (on-chain flows + exchange movements)
2. Technical analysis (RSI, MACD, MA via Binance)
3. Derivatives positioning (funding rate, OI, long/short ratio)
4. Narrative momentum (Reddit, News, CoinGecko trending)
5. Market data (price, volume, Fear & Greed Index)

## Update Frequency
- Signals refresh every 15 minutes
- LLM sentiment analysis every 12 hours
- Performance evaluation every 4 hours

## Pricing
Free (x402 micropayments coming soon)

## Contact
- API Docs: /docs
- Dashboard: /dashboard
"""


@app.get("/.well-known/agents.md", tags=["discovery"], include_in_schema=False)
async def agents_md():
    """AGENTS.md — Agentic AI Foundation standard for agent discovery."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(_AGENTS_MD, media_type="text/markdown")


# ---------------------------------------------------------------------------
# GET /dashboard — Production UI
# ---------------------------------------------------------------------------
@app.get("/dashboard", tags=["ui"], include_in_schema=False)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


# ---------------------------------------------------------------------------
# GET /api/history — Paginated history of fusion runs (each 15-min cycle)
# ---------------------------------------------------------------------------
@app.get("/api/history", tags=["signals"])
async def get_signal_history(
    agent: str = Query("signal_fusion", description="Agent name to get history for"),
    limit: int = Query(50, ge=1, le=200, description="Number of rows to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    """
    Returns paginated historical rows for any agent.
    Each row = one 15-minute orchestrator cycle.
    """
    if not _store:
        raise HTTPException(status_code=503, detail="Storage not initialized")

    valid_agents = [
        "signal_fusion", "technical_agent", "derivatives_agent",
        "market_agent", "narrative_agent", "whale_agent",
    ]
    if agent not in valid_agents:
        raise HTTPException(status_code=400, detail=f"Invalid agent. Valid: {valid_agents}")

    rows = _store.load_history(agent, limit=limit, offset=offset)
    total = _store.count_rows(agent)

    return {
        "agent": agent,
        "total_rows": total,
        "limit": limit,
        "offset": offset,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# MCP SSE Transport — direct routes on /mcp/sse and /mcp/messages
# ---------------------------------------------------------------------------
try:
    from mcp.server.sse import SseServerTransport
    from mcp_server.server import mcp as mcp_server_instance
    from starlette.responses import Response

    # Create SSE transport with /mcp/messages as the POST endpoint
    _mcp_sse_transport = SseServerTransport("/mcp/messages")

    @app.get("/mcp/sse", include_in_schema=False)
    async def mcp_sse_endpoint(request: Request):
        """MCP SSE endpoint — AI agents connect here for real-time tool access."""
        from starlette.responses import Response as StarletteResponse

        async with _mcp_sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp_server_instance._mcp_server.run(
                streams[0],
                streams[1],
                mcp_server_instance._mcp_server.create_initialization_options(),
            )
        return StarletteResponse()

    # Mount the messages POST handler
    from starlette.routing import Mount
    app.router.routes.append(
        Mount("/mcp/messages", app=_mcp_sse_transport.handle_post_message)
    )

    print("MCP SSE transport mounted at /mcp/sse")
except ImportError as e:
    print(f"MCP SSE mount skipped — {e}")
except Exception as e:
    print(f"MCP SSE mount error — {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("api.server:app", host="0.0.0.0", port=port, reload=False)
