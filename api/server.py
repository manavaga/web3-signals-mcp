"""
Web3 Signals API — FastAPI server.

Endpoints:
    GET /                           Welcome + links
    GET /health                     Agent status, last run, uptime
    GET /signal                     Full fusion (portfolio + 20 signals + LLM insights)  [x402 paid]
    GET /signal/{asset}             Single asset signal  [x402 paid]
    GET /performance/reputation     Public reputation score (30-day rolling accuracy)  [x402 paid]
    GET /performance/{asset}        Per-asset accuracy breakdown
    GET /analytics                  API usage analytics (user-agents, requests/day)
    GET /api/signal                 Internal free signal endpoint (dashboard)
    GET /api/performance/reputation Internal free reputation endpoint (dashboard)
    POST /admin/reset-accuracy      Reset tainted accuracy data (admin token required)
    GET /.well-known/agent.json     A2A agent discovery card
    GET /.well-known/agents.md      AGENTS.md (Agentic AI Foundation standard)
    GET /mcp/sse                    MCP SSE transport for remote AI agents
    GET /docs                       Auto-generated OpenAPI docs
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

from shared.storage import Storage
from signal_fusion.engine import SignalFusion
from api.dashboard import DASHBOARD_HTML

# ---------------------------------------------------------------------------
# Logging — structured output for Railway
# ---------------------------------------------------------------------------
_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("web3signals")

# x402 payment gate (enabled when PAY_TO env var is set)
try:
    from x402.http import HTTPFacilitatorClient, FacilitatorConfig, PaymentOption
    from x402.http.facilitator_client_base import CreateHeadersAuthProvider
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI
    from x402.http.types import RouteConfig as X402RouteConfig
    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.server import x402ResourceServer
    _X402_AVAILABLE = True
    logger.info("x402: imports OK")
except ImportError as _x402_err:
    _X402_AVAILABLE = False
    logger.error("x402: IMPORT FAILED — %s", _x402_err)
except Exception as _x402_err:
    _X402_AVAILABLE = False
    logger.error("x402: UNEXPECTED ERROR — %s", _x402_err)

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
# x402 Payment Gate — discoverable micropayments for AI agents
# ---------------------------------------------------------------------------
_PAY_TO = os.getenv("PAY_TO", "")
_X402_FACILITATOR_URL = os.getenv("X402_FACILITATOR_URL", "https://api.cdp.coinbase.com/platform/v2/x402")
_CDP_API_KEY_ID = os.getenv("CDP_API_KEY_ID", "")
_CDP_API_KEY_SECRET = os.getenv("CDP_API_KEY_SECRET", "")
_X402_ENABLED = bool(_PAY_TO) and _X402_AVAILABLE

_x402_server = None
_x402_routes: dict = {}

_x402_init_error: Optional[str] = None  # stored for /health diagnostics


def _build_cdp_auth_provider():
    """Build x402 AuthProvider that generates CDP JWT Bearer tokens.

    The CDP facilitator at api.cdp.coinbase.com requires Ed25519 JWTs.
    Uses the cdp-sdk's generate_jwt() if available, otherwise returns None.
    """
    if not _CDP_API_KEY_ID or not _CDP_API_KEY_SECRET:
        logger.warning("x402: no CDP_API_KEY_ID/SECRET — facilitator may reject unauthenticated requests")
        return None

    try:
        from cdp.auth import generate_jwt, JwtOptions

        def create_headers():
            headers = {}
            for endpoint, method in [("supported", "GET"), ("verify", "POST"), ("settle", "POST")]:
                opts = JwtOptions(
                    api_key_id=_CDP_API_KEY_ID,
                    api_key_secret=_CDP_API_KEY_SECRET,
                    request_method=method,
                    request_host="api.cdp.coinbase.com",
                    request_path=f"/platform/v2/x402/{endpoint}",
                )
                jwt_token = generate_jwt(opts)
                headers[endpoint] = {
                    "Authorization": f"Bearer {jwt_token}",
                    "Content-Type": "application/json",
                }
            return headers

        logger.info("x402: CDP auth configured (key_id=%s...)", _CDP_API_KEY_ID[:12])
        return CreateHeadersAuthProvider(create_headers)
    except ImportError:
        logger.warning("x402: cdp-sdk not installed — cannot authenticate with CDP facilitator")
        return None
    except Exception as e:
        logger.error("x402: CDP auth setup failed — %s", e)
        return None


if _X402_ENABLED:
    _auth_provider = _build_cdp_auth_provider()
    _facilitator = HTTPFacilitatorClient(FacilitatorConfig(
        url=_X402_FACILITATOR_URL,
        auth_provider=_auth_provider,
    ))
    _x402_server = x402ResourceServer(_facilitator)
    _x402_server.register("eip155:8453", ExactEvmServerScheme())  # Base mainnet

    # Register Bazaar discovery extension so the CDP facilitator indexes us
    try:
        from x402.extensions.bazaar import (
            bazaar_resource_server_extension,
            declare_discovery_extension,
        )
        _x402_server.register_extension(bazaar_resource_server_extension)
        logger.info("x402: Bazaar discovery extension registered")
    except ImportError:
        logger.warning("x402: Bazaar extension not available — skipping discovery")
        declare_discovery_extension = None

    # Eagerly initialize: connect to facilitator NOW, not on first request.
    # If facilitator is unreachable, disable x402 gracefully (app stays up, routes free).
    try:
        _x402_server.initialize()
        logger.info("x402: facilitator OK (%s)", _X402_FACILITATOR_URL)
    except Exception as _init_exc:
        _x402_init_error = str(_init_exc)
        logger.error("x402: FACILITATOR INIT FAILED — %s", _init_exc)
        logger.warning("x402: disabling payment gate — routes will be free until fixed")
        _X402_ENABLED = False
        _x402_server = None

if _X402_ENABLED:
    # Route patterns: x402 uses glob syntax (* for wildcards, not {param})
    _payment_option = PaymentOption(
        scheme="exact", pay_to=_PAY_TO, price="$0.001",
        network="eip155:8453",
    )

    # Build Bazaar discovery extensions using the official SDK helper
    _bazaar_signal = (
        declare_discovery_extension(input={"method": "GET"})
        if declare_discovery_extension else {}
    )
    _bazaar_signal_asset = (
        declare_discovery_extension(
            input={"method": "GET", "path_params": {"asset": "BTC"}},
        )
        if declare_discovery_extension else {}
    )
    _bazaar_reputation = (
        declare_discovery_extension(input={"method": "GET"})
        if declare_discovery_extension else {}
    )

    _x402_routes = {
        "GET /signal": X402RouteConfig(
            accepts=[_payment_option],
            description=(
                "Full crypto signal fusion: 20 assets scored 0-100 with whale, "
                "derivatives, technical, narrative, and market dimensions. "
                "Portfolio summary + LLM insights."
            ),
            mime_type="application/json",
            extensions=_bazaar_signal,
        ),
        "GET /signal/*": X402RouteConfig(
            accepts=[_payment_option],
            description=(
                "Single asset crypto signal: 6-dimension composite score (0-100), "
                "direction, momentum, and market context."
            ),
            mime_type="application/json",
            extensions=_bazaar_signal_asset,
        ),
        "GET /performance/reputation": X402RouteConfig(
            accepts=[_payment_option],
            description=(
                "30-day rolling signal accuracy at 24h/48h windows, "
                "per-asset breakdown. Verifiable reputation score."
            ),
            mime_type="application/json",
            extensions=_bazaar_reputation,
        ),
    }
    logger.info("x402: configured %d paid routes (pay_to=%s...)", len(_x402_routes), _PAY_TO[:10])


# ---------------------------------------------------------------------------
# Background orchestrator — runs all agents every N seconds
# ---------------------------------------------------------------------------

# Per-agent cadence: how often each agent should re-fetch (in minutes).
# Agents whose cadence hasn't elapsed are skipped that cycle.
# Env-overridable: AGENT_CADENCE_TECHNICAL_MIN=15, etc.
_AGENT_CADENCES_MIN: dict[str, int] = {
    "technical_agent":   15,   # Price action is fast, RSI/MACD shift on short candles
    "derivatives_agent": 15,   # Lead indicators need frequent sampling (funding delta, OI div)
    "whale_agent":       30,   # Whale transactions are sporadic, 30min catches everything
    "market_agent":      30,   # F&G updates daily, CoinGecko dominance moves slowly
    "narrative_agent":   60,   # Reddit/news don't change meaningfully every 15min
}

_agent_last_run: dict[str, float] = {}  # agent_name -> timestamp of last successful run


def _should_run_agent(name: str) -> bool:
    """Check if enough time has elapsed since this agent's last run."""
    env_key = f"AGENT_CADENCE_{name.upper().replace('_AGENT', '')}_MIN"
    cadence_min = int(os.getenv(env_key, str(_AGENT_CADENCES_MIN.get(name, 15))))
    cadence_sec = cadence_min * 60

    last = _agent_last_run.get(name)
    if last is None:
        return True  # first run — always execute
    return (time.time() - last) >= cadence_sec


def _orchestrator_loop(store: Storage, interval: int) -> None:
    """Background thread: run all 5 agents + save to storage."""
    global _orchestrator_running

    # Delay first run by 5 seconds to let the server boot
    time.sleep(5)

    while _orchestrator_running:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        logger.info("[%s] Orchestrator: starting agent run...", ts)

        agents = []

        try:
            from technical_agent.engine import TechnicalAgent
            agents.append(("technical_agent", TechnicalAgent))
        except ImportError as e:
            logger.error("  technical_agent: import error — %s", e)

        try:
            from derivatives_agent.engine import DerivativesAgent
            agents.append(("derivatives_agent", DerivativesAgent))
        except ImportError as e:
            logger.error("  derivatives_agent: import error — %s", e)

        try:
            from market_agent.engine import MarketAgent
            agents.append(("market_agent", MarketAgent))
        except ImportError as e:
            logger.error("  market_agent: import error — %s", e)

        try:
            from narrative_agent.engine import NarrativeAgent
            agents.append(("narrative_agent", NarrativeAgent))
        except ImportError as e:
            logger.error("  narrative_agent: import error — %s", e)

        try:
            from whale_agent.engine import WhaleAgent
            agents.append(("whale_agent", WhaleAgent))
        except ImportError as e:
            logger.error("  whale_agent: import error — %s", e)

        ran_any = False
        agent_timeout = int(os.getenv("AGENT_TIMEOUT_SEC", "120"))
        _agent_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        for name, factory in agents:
            if not _should_run_agent(name):
                logger.debug("  %s: skipped (cadence not elapsed)", name)
                continue
            try:
                agent = factory()
                future = _agent_executor.submit(agent.execute)
                result = future.result(timeout=agent_timeout)
                store.save(name, result)
                _agent_last_run[name] = time.time()
                ran_any = True
                status = result["status"]
                ms = result["meta"]["duration_ms"]
                errs = len(result["meta"]["errors"])
                logger.info("  %s: %s (%sms, %s errors)", name, status, ms, errs)
            except concurrent.futures.TimeoutError:
                logger.error("  %s: TIMEOUT after %ss — skipping", name, agent_timeout)
                if _store:
                    _store.save_error_event(
                        error_type="agent_timeout",
                        source=name,
                        message=f"Timeout after {agent_timeout}s",
                    )
                # Cancel the future and recreate executor to abandon hung thread
                future.cancel()
                _agent_executor.shutdown(wait=False)
                _agent_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            except Exception as exc:
                logger.error("  %s: CRASH — %s", name, exc)
                if _store:
                    _store.save_error_event(
                        error_type="agent_crash",
                        source=name,
                        message=str(exc)[:500],
                    )
        _agent_executor.shutdown(wait=False)

        # Run signal fusion only if at least one agent produced new data
        if ran_any:
            try:
                fusion = SignalFusion()
                fusion_result = fusion.fuse()
                store.save("signal_fusion", fusion_result)
                f_status = fusion_result.get("status", "unknown")
                f_ms = fusion_result.get("meta", {}).get("duration_ms", 0)
                logger.info("  signal_fusion: %s (%sms)", f_status, f_ms)
            except Exception as exc:
                logger.error("  signal_fusion: CRASH — %s", exc)
        else:
            logger.info("  signal_fusion: skipped (no new agent data)")

        # --- 24-hour LLM Event Extraction Cycle ---
        # Runs narrative LLM event extraction once per day.
        # This is the ONLY LLM call that enhances scores (via event scoring).
        # Cost: ~$0.01/day. Override: LLM_SENTIMENT_CYCLE_HOURS=12 for faster.
        try:
            llm_cycle_hours = int(os.getenv("LLM_SENTIMENT_CYCLE_HOURS", "24"))
            last_llm_run = store.load_kv("llm_cycle", "last_run")
            now_ts = time.time()

            should_run_llm = False
            if last_llm_run is None:
                should_run_llm = True
            elif (now_ts - last_llm_run) >= llm_cycle_hours * 3600:
                should_run_llm = True

            if should_run_llm:
                logger.info("  [LLM] Running daily narrative event extraction...")
                try:
                    from narrative_agent.engine import NarrativeAgent
                    narrator = NarrativeAgent()
                    llm_result = narrator.run_llm_sentiment(store)
                    store.save_kv("llm_cycle", "last_run", now_ts)
                    logger.info("  [LLM] Done: %s", llm_result)
                except Exception as llm_exc:
                    logger.error("  [LLM] Error: %s", llm_exc)
        except Exception as exc:
            logger.error("  llm_cycle: %s", exc)

        # Record performance snapshot for accuracy tracking
        try:
            _record_performance_snapshot(store)
        except Exception as exc:
            logger.error("  performance snapshot: %s", exc)

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
                logger.info("  [PERF] Running accuracy evaluation...")
                _evaluate_old_snapshots(store)
                store.save_kv("perf_eval", "last_run", now_ts)

                # Compute and cache IC (Information Coefficient) after accuracy eval
                try:
                    for wh in [24, 48]:
                        ic_result = store.compute_ic(window_hours=wh, days=30)
                        if ic_result.get("total_observations", 0) > 0:
                            store.save_kv_json("ic_tracking", f"ic_{wh}h_30d", ic_result)
                            logger.info("  [IC] %sh IC computed: overall=%.4f, %s observations",
                                       wh, ic_result.get("overall_ic") or 0,
                                       ic_result.get("total_observations", 0))
                except Exception as ic_exc:
                    logger.error("  IC computation: %s", ic_exc)

                # Trigger weight optimizer after evaluation
                try:
                    from shared.profile_loader import load_profile
                    from signal_fusion.optimizer import WeightOptimizer
                    from pathlib import Path

                    profile_path = Path(__file__).resolve().parent.parent / "signal_fusion" / "profiles" / "default.yaml"
                    profile = load_profile(profile_path)
                    optimizer = WeightOptimizer(store, profile)

                    if optimizer.is_enabled() and optimizer.should_optimize():
                        logger.info("  [LEARN] Running weight optimization...")
                        new_weights = optimizer.compute_and_apply()
                        if new_weights:
                            logger.info("  [LEARN] Updated weights: %s", new_weights)
                        else:
                            logger.info("  [LEARN] Not enough data for optimization yet")
                except Exception as opt_exc:
                    logger.error("  weight optimizer: %s", opt_exc)

        except Exception as exc:
            logger.error("  performance eval: %s", exc)

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        logger.info("[%s] Orchestrator: done. Sleeping %ss.", ts, interval)

        # Sleep in small increments so we can stop quickly
        for _ in range(interval):
            if not _orchestrator_running:
                return
            time.sleep(1)


def _record_performance_snapshot(store: Storage) -> None:
    """
    Record performance snapshots — max 1 per asset per 12 hours.
    This avoids inflating sample size with near-identical correlated snapshots.
    Direction comes directly from the fusion engine output (which accounts for
    abstain zones and YAML label thresholds).
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

        # Use the fusion engine's direction directly — it already accounts
        # for abstain zones (42-58) and YAML label thresholds.
        # Map fusion directions: "buy" → "bullish", "sell" → "bearish", "neutral" → "neutral"
        fusion_direction = sig.get("direction", "neutral")
        if fusion_direction == "buy":
            direction = "bullish"
        elif fusion_direction == "sell":
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

        snap_id = store.save_performance_snapshot(
            asset=asset,
            signal_score=score,
            signal_direction=direction,
            price_at_signal=price,
            sources_count=sources,
            detail=full_detail,
        )

        # Save per-dimension numeric scores for IC tracking
        if snap_id is not None:
            dim_scores = {}
            for dim_name, dim_data in sig.get("dimensions", {}).items():
                s = dim_data.get("score")
                if s is not None:
                    dim_scores[dim_name] = float(s)
            config_ver = sig.get("config_version", "")
            regime = sig.get("regime_at_generation", "")
            try:
                store.save_dimension_scores(snap_id, dim_scores, config_ver, regime)
            except Exception as dim_exc:
                logger.warning("  dimension scores save failed: %s", dim_exc)

        saved += 1

    if saved:
        store.save_kv("perf_snapshot", "last_run", now_ts)
        logger.info("  performance: saved %s snapshots (next in %sh)", saved, snapshot_interval)


def _calculate_gradient_score(
    direction: str, pct_change: float, accuracy_cfg: dict,
) -> float:
    """Calculate gradient accuracy score (0.0-1.0) for a directional signal.

    Instead of binary hit/miss, scores based on direction AND magnitude:
      strong_correct (1.0) — strong move in predicted direction
      correct (0.7) — moderate move in predicted direction
      weak_correct (0.4) — right direction but within noise
      weak_wrong (0.2) — wrong direction but within noise
      wrong (0.0) — clear wrong call
    """
    noise_pct = float(accuracy_cfg.get("noise_threshold_pct", 2.0))
    strong_pct = float(accuracy_cfg.get("strong_threshold_pct", 5.0))
    gradient = accuracy_cfg.get("gradient", {})

    # Normalize: for bearish signals, flip the sign so positive = correct
    effective_change = pct_change if direction == "bullish" else -pct_change

    if effective_change >= strong_pct:
        return float(gradient.get("strong_correct", 1.0))
    elif effective_change >= noise_pct:
        return float(gradient.get("correct", 0.7))
    elif effective_change >= 0:
        return float(gradient.get("weak_correct", 0.4))
    elif effective_change >= -noise_pct:
        return float(gradient.get("weak_wrong", 0.2))
    else:
        return float(gradient.get("wrong", 0.0))


def _evaluate_old_snapshots(store: Storage) -> None:
    """
    Check snapshots that are 24h/48h old and evaluate accuracy.
    Uses gradient scoring: 0.0-1.0 based on direction AND magnitude.
    Reuses prices already stored by the market agent (no extra API call).
    """
    # Get current prices from the market agent's latest run (already in storage)
    market = store.load_latest("market_agent")
    if not market:
        logger.warning("  performance eval: no market agent data in storage")
        return

    per_asset = market.get("data", {}).get("per_asset", {})
    current_prices = {}
    for asset, adata in per_asset.items():
        p = adata.get("price")
        if p is not None:
            current_prices[asset] = float(p)

    if not current_prices:
        logger.warning("  performance eval: no prices in market agent data")
        return

    # Load gradient scoring config from fusion profile
    accuracy_cfg = {}
    if _fusion and hasattr(_fusion, "profile"):
        accuracy_cfg = _fusion.profile.get("accuracy", {})

    # Evaluate each window: 24h, 48h
    windows = [(24, 24), (48, 48)]
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

            # Skip neutral signals — only evaluate directional calls.
            if direction == "neutral":
                store.save_performance_accuracy(
                    snapshot_id=snap["id"],
                    window_hours=window_hours,
                    price_at_window=price_now,
                    gradient_score=None,  # NULL = skipped (neutral)
                )
                continue

            # Calculate gradient score for directional signals
            pct_change = (price_now - price_at_signal) / price_at_signal * 100
            gradient_score = _calculate_gradient_score(direction, pct_change, accuracy_cfg)

            store.save_performance_accuracy(
                snapshot_id=snap["id"],
                window_hours=window_hours,
                price_at_window=price_now,
                gradient_score=gradient_score,
                pct_change=round(pct_change, 2),
            )
            total_evaluated += 1

    if total_evaluated:
        logger.info("  [PERF] Evaluated %s snapshots across %s windows", total_evaluated, len(windows))
    else:
        logger.info("  [PERF] No snapshots ready for evaluation yet (need 24h+ age)")


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
    logger.info("Orchestrator started (interval=%ss)", interval)

    # Start MCP Streamable HTTP session manager if available
    _mcp_session_ctx = None
    try:
        from mcp_server.server import mcp as _mcp_inst
        if hasattr(_mcp_inst, '_session_manager') and _mcp_inst._session_manager is not None:
            _mcp_session_ctx = _mcp_inst.session_manager.run()
            await _mcp_session_ctx.__aenter__()
            logger.info("MCP session manager started")
    except Exception as e:
        logger.debug("MCP session manager not started — %s", e)

    yield

    # Shutdown MCP session manager
    if _mcp_session_ctx is not None:
        try:
            await _mcp_session_ctx.__aexit__(None, None, None)
        except Exception:
            pass

    # Shutdown orchestrator
    _orchestrator_running = False
    if _orchestrator_thread:
        _orchestrator_thread.join(timeout=5)
    logger.info("Orchestrator stopped")


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
    version="0.2.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Internal API key for tagging requests as internal
# ---------------------------------------------------------------------------
_INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")


def _get_real_ip(request: Request) -> str:
    """Extract real client IP from reverse proxy headers.

    Railway sets X-Forwarded-For. Priority:
    X-Forwarded-For (first IP) > X-Real-IP > request.client.host
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("x-real-ip", "")
    if xri:
        return xri.strip()
    return request.client.host if request.client else ""


_OWN_HOSTS = {"web3-signals-api-production.up.railway.app", "localhost", "127.0.0.1"}

def _classify_request_source(request: Request) -> str:
    """Classify request as 'internal', 'external', or 'unknown'.

    Detection layers (first match wins):
    1. X-Internal-Key header matches INTERNAL_API_KEY env var  -> internal
    2. Referer from our own domain (dashboard AJAX calls)      -> internal
    3. Pure-admin paths (dashboard, analytics, health, admin)   -> internal
    4. Free /api/* mirror paths (dashboard's free endpoints)    -> internal
    5. Dev tool user-agents without payment header              -> internal
    6. Known AI agent / MCP user-agents                        -> external
    7. Has x402 payment header                                 -> external
    8. Everything else                                         -> unknown
    """
    # Layer 1: Explicit header (most reliable)
    if _INTERNAL_API_KEY:
        internal_header = request.headers.get("x-internal-key", "")
        if internal_header == _INTERNAL_API_KEY:
            return "internal"

    # Layer 2: Referer from our own domain = dashboard making API calls
    referer = (request.headers.get("referer", "") or "").lower()
    if any(host in referer for host in _OWN_HOSTS):
        return "internal"

    # Layer 3: Pure admin/ops paths — no external user would hit these
    path = request.url.path
    if (path == "/dashboard" or path.startswith("/analytics")
            or path == "/health" or path.startswith("/admin")):
        return "internal"

    # Layer 4: Free /api/* mirror paths — only dashboard calls these
    if path.startswith("/api/"):
        return "internal"

    ua = (request.headers.get("user-agent", "") or "").lower()
    has_payment = bool(
        request.headers.get("payment-signature", "")
        or request.headers.get("x-payment", "")
    )

    # Layer 5: Dev tools without payment header = testing
    if not has_payment:
        if any(t in ua for t in ("postman", "curl", "httpie", "insomnia")):
            return "internal"

    # Layer 6: Known AI agents / bots — always external
    _AI_AGENT_SIGS = (
        "claudebot", "claude-web", "anthropic",
        "gptbot", "chatgpt", "openai",
        "google-extended", "gemini",
        "mcp", "langchain", "crewai", "autogpt",
        "ccbot", "bytespider", "amazonbot",
    )
    if any(sig in ua for sig in _AI_AGENT_SIGS):
        return "external"

    # Layer 7: Has x402 payment header = real paying user
    if has_payment:
        return "external"

    # Layer 8: Discovery endpoints from non-dashboard source = external
    if (path.startswith("/.well-known") or path == "/docs"
            or path == "/openapi.json" or path == "/robots.txt"
            or path.startswith("/mcp/")):
        return "external"

    # Layer 9: Paid endpoints hit without referer/key
    # Could be us testing OR a real user. Mark unknown so we don't
    # pollute either bucket — user can audit these later.
    return "unknown"


# ---------------------------------------------------------------------------
# Referer source classification — track which directory/listing sent traffic
# ---------------------------------------------------------------------------
_REFERER_MAP = {
    "mcp.so": "mcp.so",
    "pulsemcp": "pulsemcp",
    "mcpservers.org": "mcpservers.org",
    "x402list.fun": "x402list.fun",
    "bazaar": "bazaar",
    "glama.ai": "glama.ai",
    "smithery.ai": "smithery.ai",
    "mcphub": "mcphub",
    "mcpmarket": "mcpmarket",
    "lobehub": "lobehub",
    "google.com": "google",
    "bing.com": "bing",
    "duckduckgo": "duckduckgo",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "reddit.com": "reddit",
    "linkedin.com": "linkedin",
    "github.com": "github",
}


def _classify_referer_source(referer: str) -> str:
    """Classify referer into a known source for attribution tracking."""
    if not referer:
        return "direct"
    ref_lower = referer.lower()
    if any(host in ref_lower for host in _OWN_HOSTS):
        return "self"
    for pattern, label in _REFERER_MAP.items():
        if pattern in ref_lower:
            return label
    return "other"


def _make_fingerprint(ip: str, ua: str) -> str:
    """Create a short hash fingerprint from IP + user-agent for unique client tracking."""
    import hashlib
    raw = f"{ip}|{ua}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Usage Tracking Middleware
# ---------------------------------------------------------------------------
class UsageTrackingMiddleware(BaseHTTPMiddleware):
    """Logs every API request for analytics — user-agent, endpoint, duration.

    x402 payment awareness: detects paid calls (payment-signature header
    present + 200 on a paid route) vs. 402 payment-required responses.
    """

    SKIP_PATHS = {"/favicon.ico", "/openapi.json"}
    PAID_PATHS = {"/signal", "/performance/reputation"}  # prefix-matched

    def _is_paid_path(self, path: str) -> bool:
        """Check if a path is an x402-gated route."""
        if path in self.PAID_PATHS:
            return True
        if path.startswith("/signal/") and path != "/signal/" and not path.endswith("/trace"):
            return True
        return False

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration_ms = (time.time() - start) * 1000

        path = request.url.path
        if path in self.SKIP_PATHS:
            return response

        # Detect x402 payment context
        is_paid_route = self._is_paid_path(path)
        has_payment_header = bool(
            request.headers.get("payment-signature", "")
            or request.headers.get("x-payment", "")
        )
        status = response.status_code

        # Classify the x402 payment state
        if is_paid_route and status == 402:
            payment_status = "payment_required"
        elif is_paid_route and has_payment_header and status == 200:
            payment_status = "paid"
        elif is_paid_route and has_payment_header and status != 200:
            payment_status = "payment_failed"
        elif is_paid_route and not has_payment_header and status == 200:
            payment_status = "free"  # x402 disabled or bypassed
        else:
            payment_status = None  # not a paid route

        # Fire-and-forget: don't slow down the response
        try:
            if _store:
                ua = request.headers.get("user-agent", "")
                client_ip = _get_real_ip(request)
                request_source = _classify_request_source(request)
                referer = request.headers.get("referer", "")
                origin = request.headers.get("origin", "")
                referer_source = _classify_referer_source(referer)
                fingerprint = _make_fingerprint(client_ip, ua)
                _store.save_api_request(
                    endpoint=path,
                    method=request.method,
                    user_agent=ua,
                    status_code=status,
                    duration_ms=round(duration_ms, 1),
                    client_ip=client_ip,
                    payment_status=payment_status,
                    request_source=request_source,
                    referer=referer,
                    origin=origin,
                    referer_source=referer_source,
                    client_fingerprint=fingerprint,
                )

                # Track errors for analytics
                if status >= 500:
                    _store.save_error_event(
                        error_type="api_5xx",
                        source=path,
                        message=f"HTTP {status} on {request.method} {path}",
                        context={"status_code": status, "duration_ms": round(duration_ms, 1),
                                 "user_agent": ua[:100]}
                    )
                if payment_status == "payment_failed":
                    _store.save_error_event(
                        error_type="payment_failure",
                        source=path,
                        message=f"x402 payment failed on {path}",
                        context={"user_agent": ua[:100], "duration_ms": round(duration_ms, 1)}
                    )
        except Exception:
            logger.warning("Usage tracking failed for %s %s: %s",
                           request.method, path, traceback.format_exc(limit=1))

        return response


# x402 middleware — added first so it runs INNER (handles payments)
if _X402_ENABLED:
    app.add_middleware(PaymentMiddlewareASGI, routes=_x402_routes, server=_x402_server)
    logger.info("x402 payment gate enabled (facilitator=%s)", _X402_FACILITATOR_URL)

# Usage tracking — added last so it runs OUTERMOST and sees ALL responses (incl. 402s)
app.add_middleware(UsageTrackingMiddleware)


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------
@app.get("/", tags=["info"])
async def root():
    response = {
        "name": "Web3 Signals API",
        "version": "0.2.0",
        "description": "AI-powered crypto signal intelligence for 20 assets",
        "model_version": "v0.2.0-regime-aware",
        "endpoints": {
            "/dashboard": "Live signal intelligence dashboard (open in browser)",
            "/health": "Agent status and uptime",
            "/signal": "Full fusion — portfolio + 20 signals + LLM insights",
            "/signal/{asset}": "Single asset signal (e.g. /signal/BTC)",
            "/performance/reputation": "Public reputation score — 30-day signal accuracy",
            "/performance": "Overall accuracy overview (free)",
            "/performance/{asset}": "Per-asset accuracy breakdown",
            "/analytics": "API usage analytics — who's using us, request trends",
            "/analytics/x402": "x402 payment analytics — paid calls, revenue, conversion rate",
            "/analytics/insights": "Growth insights — external vs internal, AI agent trends, revenue split",
            "/analytics/ic": "Signal quality — Information Coefficient per scoring dimension per regime",
            "/api/history": "Paginated history of all agent runs",
        },
        "discovery": {
            "/.well-known/agent.json": "A2A agent discovery card (Google A2A protocol)",
            "/.well-known/agents.md": "AGENTS.md — Agentic AI Foundation discovery",
            "/.well-known/x402": "x402scan-compatible discovery (agentcash format)",
            "/.well-known/x402.json": "x402 payment protocol discovery (detailed)",
            "/openapi.json": "OpenAPI 3.0 specification",
            "/docs": "Swagger UI — interactive API documentation",
            "/robots.txt": "Crawler guidance",
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
    if _X402_ENABLED:
        response["x402"] = {
            "enabled": True,
            "facilitator": _X402_FACILITATOR_URL,
            "network": "Base (eip155:8453)",
            "currency": "USDC",
            "pricing": {
                "/signal": "$0.001",
                "/signal/{asset}": "$0.001",
                "/performance/reputation": "$0.001",
            },
            "free_endpoints": [
                "/health", "/dashboard", "/analytics",
                "/.well-known/*", "/mcp/sse", "/docs",
            ],
        }
    return response


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

    # x402 payment gate status
    x402_status: Dict[str, Any] = {"enabled": _X402_ENABLED}
    if _PAY_TO:
        x402_status["pay_to"] = _PAY_TO[:10] + "..."
        x402_status["facilitator_url"] = _X402_FACILITATOR_URL
    if _x402_init_error:
        x402_status["init_error"] = _x402_init_error
        x402_status["note"] = "payment gate disabled — routes serving free until fixed"

    return {
        "status": "healthy",
        "boot_time": _boot_time,
        "storage_backend": _store.backend if _store else "none",
        "agents": agent_status,
        "fusion": fusion_status,
        "x402": x402_status,
    }


# ---------------------------------------------------------------------------
# GET /api/signal — Internal (free) signal endpoint for dashboard
# ---------------------------------------------------------------------------
@app.get("/api/signal", tags=["internal"], include_in_schema=False)
async def get_signal_internal():
    """Same data as /signal but free — used by the dashboard UI."""
    return await get_signal()


# ---------------------------------------------------------------------------
# GET /signal — Full fusion output
# ---------------------------------------------------------------------------
@app.get("/signal", tags=["signals"],
         openapi_extra={
             "x-payment-info": {
                 "protocols": ["x402"],
                 "price": "$0.001",
                 "network": "eip155:8453",
                 "token": "USDC",
             },
             "responses": {
                 "402": {"description": "Payment Required — x402 micropayment needed"},
             },
         })
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
@app.get("/signal/{asset}", tags=["signals"],
         openapi_extra={
             "x-payment-info": {
                 "protocols": ["x402"],
                 "price": "$0.001",
                 "network": "eip155:8453",
                 "token": "USDC",
             },
             "responses": {
                 "402": {"description": "Payment Required — x402 micropayment needed"},
             },
         })
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
# GET /signal/{asset}/trace — Pipeline trace for debugging & transparency
# ---------------------------------------------------------------------------
@app.get("/signal/{asset}/trace", tags=["signals"])
async def get_signal_trace(asset: str):
    """Full pipeline trace showing how an asset's score was computed step-by-step.

    Shows: raw agent data → dimension scores → regime → weights → composite →
    velocity dampening → abstain check → final label.
    Free endpoint — intended for debugging and transparency.
    """
    if not _store:
        raise HTTPException(status_code=503, detail="Storage not initialized")

    asset = asset.upper()

    # Load the latest fusion result
    fusion_data = _store.load("signal_fusion")
    if not fusion_data:
        raise HTTPException(status_code=503, detail="No fusion data available yet")

    signals = fusion_data.get("signals", {})
    if asset not in signals:
        valid = list(signals.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Asset '{asset}' not found. Valid: {valid}",
        )

    sig = signals[asset]
    portfolio = fusion_data.get("portfolio_summary", {})

    # Gather raw agent inputs for this asset
    agent_names = {
        "whale": "whale_agent",
        "technical": "technical_agent",
        "derivatives": "derivatives_agent",
        "narrative": "narrative_agent",
        "market": "market_agent",
    }
    raw_inputs: dict = {}
    for role, agent_name in agent_names.items():
        snapshot = _store.load_latest(agent_name)
        if snapshot and "data" in snapshot:
            data = snapshot["data"]
            # Find this asset's data in the agent output
            asset_data = data.get(asset, data.get(asset.lower()))
            if asset_data and isinstance(asset_data, dict):
                # Extract key metrics (not the full blob)
                raw_inputs[role] = {
                    k: v for k, v in asset_data.items()
                    if not isinstance(v, (list, dict)) or k in ("llm_events",)
                }
            else:
                raw_inputs[role] = None
        else:
            raw_inputs[role] = None

    # Build step-by-step trace
    dims = sig.get("dimensions", {})
    trace_steps = []

    # Step 1: Raw data summary
    trace_steps.append({
        "step": 1,
        "name": "Raw Agent Data",
        "description": "Key metrics from each data collection agent",
        "data": raw_inputs,
    })

    # Step 2: Dimension scores
    dim_scores = {}
    for dim, info in dims.items():
        dim_scores[dim] = {
            "score": info.get("score"),
            "detail": info.get("detail"),
            "data_tier": info.get("data_tier"),
        }
    trace_steps.append({
        "step": 2,
        "name": "Dimension Scores",
        "description": "Each dimension scored 0-100 based on its rules",
        "data": dim_scores,
    })

    # Step 3: Regime detection
    trace_steps.append({
        "step": 3,
        "name": "Regime Detection",
        "description": "Current market regime determines scoring behavior",
        "data": {
            "regime": sig.get("regime"),
            "regime_at_generation": sig.get("regime_at_generation"),
            "fear_greed": portfolio.get("fear_greed"),
            "btc_downtrend": sig.get("trend_dampened"),
        },
    })

    # Step 4: Weights
    dim_weights = {}
    for dim, info in dims.items():
        dim_weights[dim] = info.get("weight")
    trace_steps.append({
        "step": 4,
        "name": "Adjusted Weights",
        "description": "Weights after regime adjustments, data quality rebalancing, and IC optimization",
        "data": dim_weights,
    })

    # Step 5: Composite
    trace_steps.append({
        "step": 5,
        "name": "Composite Score",
        "description": "Weighted sum of dimension scores",
        "data": {
            "composite": sig.get("composite_score"),
            "formula": " + ".join(
                f"{dim}({dims[dim].get('score', '?')}) x {dims[dim].get('weight', '?')}"
                for dim in dims
            ),
        },
    })

    # Step 6: Velocity dampening
    vel = sig.get("velocity")
    trace_steps.append({
        "step": 6,
        "name": "Velocity Dampening",
        "description": "Dampens composite when indicators accelerate against signal",
        "data": {
            "applied": vel is not None and vel.get("dampening_factor", 1.0) < 1.0,
            "dampening_factor": vel.get("dampening_factor") if vel else 1.0,
            "velocity_details": vel if vel else "not applied",
        },
    })

    # Step 7: Abstain check
    abstain_threshold = sig.get("abstain_threshold", 0)
    composite = sig.get("composite_score", 50)
    distance = abs(composite - 50.0)
    trace_steps.append({
        "step": 7,
        "name": "Abstain Check",
        "description": f"|{composite} - 50| = {distance:.1f} {'<' if sig.get('abstain') else '>='} {abstain_threshold}",
        "data": {
            "composite": composite,
            "distance_from_center": round(distance, 1),
            "abstain_threshold": abstain_threshold,
            "abstain": sig.get("abstain"),
        },
    })

    # Step 8: Final output
    trace_steps.append({
        "step": 8,
        "name": "Final Signal",
        "description": "The output signal after all processing",
        "data": {
            "label": sig.get("label"),
            "direction": sig.get("direction"),
            "composite_score": composite,
            "momentum": sig.get("momentum"),
            "prev_score": sig.get("prev_score"),
            "config_version": sig.get("config_version"),
        },
    })

    return {
        "asset": asset,
        "timestamp": fusion_data.get("meta", {}).get("timestamp"),
        "trace": trace_steps,
    }


# ---------------------------------------------------------------------------
# GET /api/performance/reputation — Internal (free) for dashboard
# ---------------------------------------------------------------------------
@app.get("/api/performance/reputation", tags=["internal"], include_in_schema=False)
async def get_reputation_internal():
    """Same data as /performance/reputation but free — used by dashboard UI."""
    return await get_reputation()


# ---------------------------------------------------------------------------
# GET /performance/reputation — Public reputation score (agent-facing)
# ---------------------------------------------------------------------------
@app.get("/performance/reputation", tags=["performance"],
         openapi_extra={
             "x-payment-info": {
                 "protocols": ["x402"],
                 "price": "$0.001",
                 "network": "eip155:8453",
                 "token": "USDC",
             },
             "responses": {
                 "402": {"description": "Payment Required — x402 micropayment needed"},
             },
         })
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

    # Gradient accuracy: average gradient score × 100
    avg_gradient = stats.get("avg_gradient", 0.0)
    accuracy = round(avg_gradient * 100, 1)
    reputation_score = int(round(accuracy))

    return {
        "status": "active",
        "reputation_score": reputation_score,
        "accuracy_30d": accuracy,
        "avg_gradient_score": avg_gradient,
        "directional_signals_evaluated": stats["total"],
        "avg_abs_pct_change": stats.get("avg_abs_pct_change", 0),
        "neutral_signals_skipped": stats.get("neutral_skipped", 0),
        "by_timeframe": stats["by_timeframe"],
        "by_asset": stats["by_asset"],
        "snapshots_collected_30d": total_snapshots,
        "methodology": {
            "direction_extraction": (
                "from fusion engine: direction comes from YAML label thresholds. "
                "Abstain zone is DYNAMIC based on Fear & Greed index: "
                "extreme fear/greed → threshold=5 (zone 45-55), "
                "moderate fear/greed → threshold=6 (zone 44-56), "
                "neutral → threshold=10 (zone 40-60). "
                "Signals in the abstain zone are labelled INSUFFICIENT EDGE and skipped."
            ),
            "trend_override": (
                "When BTC is >5% below its 30-day MA (confirmed downtrend), "
                "contrarian boost on market and derivatives dimensions is dampened by 30%. "
                "This allows bearish signals to emerge in sustained bear markets."
            ),
            "neutral_handling": "neutral/abstain signals are NOT evaluated — only directional calls count",
            "scoring": "gradient (0.0-1.0) based on direction AND magnitude",
            "gradient_scale": {
                "1.0": "strong move (>5%) in predicted direction",
                "0.7": "moderate move (2-5%) in predicted direction",
                "0.4": "right direction but within noise (<2%)",
                "0.2": "wrong direction but within noise (<2%)",
                "0.0": "clear wrong call (>2% against prediction)",
            },
            "accuracy_formula": "AVG(gradient_score) × 100",
            "noise_threshold": "±2% — moves within this range are inconclusive",
            "window": "30-day rolling",
            "timeframes": ["24h", "48h"],
            "price_source": "market agent (CoinGecko + Binance)",
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

    # Include x402 payment summary in main analytics
    x402_stats = _store.load_x402_analytics(days=days)

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
        "by_source": stats.get("by_source", {}),
        "external_unique_clients": stats.get("external_unique_ips", 0),
        "external_requests_per_day": stats.get("external_requests_per_day", {}),
        "external_by_client_type": stats.get("external_by_client_type", {}),
        "external_top_user_agents": stats.get("external_top_user_agents", []),
        "external_by_endpoint": stats.get("external_by_endpoint", {}),
        "x402_payments": {
            "total_paid_calls": x402_stats["total_paid_calls"],
            "estimated_revenue_usdc": x402_stats["estimated_revenue_usdc"],
            "external_paid_calls": x402_stats.get("external_paid_calls", 0),
            "external_revenue_usdc": x402_stats.get("external_revenue_usdc", 0.0),
            "total_402_challenges": x402_stats["total_402_challenges"],
            "conversion_rate_pct": (
                round(x402_stats["total_paid_calls"] / x402_stats["total_402_challenges"] * 100, 1)
                if x402_stats["total_402_challenges"] > 0 else 0
            ),
            "detail_endpoint": "/analytics/x402",
        },
        "attribution": {
            "external_by_referer_source": stats.get("external_by_referer_source", {}),
        },
        "funnel": stats.get("funnel", {}),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /analytics/x402 — x402 payment analytics
# ---------------------------------------------------------------------------
@app.get("/analytics/x402", tags=["analytics"])
async def get_x402_analytics(
    days: int = Query(30, ge=1, le=90, description="Number of days to aggregate"),
):
    """
    x402 payment analytics — paid calls, revenue, conversion rate, top paying clients.
    Tracks every x402 micropayment through the system.
    """
    if not _store:
        raise HTTPException(status_code=503, detail="Storage not initialized")

    stats = _store.load_x402_analytics(days=days)
    total_challenges = stats["total_402_challenges"]
    total_paid = stats["total_paid_calls"]
    conversion = (
        round(total_paid / total_challenges * 100, 1)
        if total_challenges > 0 else 0
    )

    return {
        "status": "active",
        "window_days": days,
        "x402_enabled": _X402_ENABLED,
        "price_per_call": "$0.001 USDC",
        "network": "Base (eip155:8453)",
        "total_paid_calls": total_paid,
        "total_402_challenges": total_challenges,
        "total_payment_failures": stats["total_payment_failures"],
        "conversion_rate_pct": conversion,
        "estimated_revenue_usdc": stats["estimated_revenue_usdc"],
        "paid_by_source": stats.get("paid_by_source", {}),
        "external_paid_calls": stats.get("external_paid_calls", 0),
        "internal_paid_calls": stats.get("internal_paid_calls", 0),
        "external_revenue_usdc": stats.get("external_revenue_usdc", 0.0),
        "by_endpoint": stats["by_endpoint"],
        "by_client_type": stats["by_client_type"],
        "paid_per_day": stats["paid_per_day"],
        "avg_paid_latency_ms": stats["avg_paid_latency_ms"],
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /analytics/insights — Growth insights (are external calls increasing?)
# ---------------------------------------------------------------------------
@app.get("/analytics/insights", tags=["analytics"])
async def get_analytics_insights(
    days: int = Query(30, ge=1, le=90, description="Number of days to analyze"),
):
    """Growth insights — external AI agent call trends, source segmentation, revenue split."""
    if not _store:
        raise HTTPException(status_code=503, detail="Storage not initialized")

    stats = _store.load_api_analytics(days=days)
    x402_stats = _store.load_x402_analytics(days=days)

    total = max(stats.get("total_requests", 0), 1)
    by_source = stats.get("by_source", {})
    external_total = by_source.get("external", 0)
    internal_total = by_source.get("internal", 0)
    unknown_total = by_source.get("unknown", 0)

    # Growth: compare first half vs second half of external daily trend
    ext_daily = stats.get("external_requests_per_day", {})
    daily_sorted = sorted(ext_daily.items())
    growth_pct = None
    if len(daily_sorted) >= 4:
        mid = len(daily_sorted) // 2
        first_half_avg = sum(v for _, v in daily_sorted[:mid]) / mid
        second_half_avg = sum(v for _, v in daily_sorted[mid:]) / (len(daily_sorted) - mid)
        if first_half_avg > 0:
            growth_pct = round((second_half_avg - first_half_avg) / first_half_avg * 100, 1)

    ext_client_types = stats.get("external_by_client_type", {})
    ai_types = {k: v for k, v in ext_client_types.items()
                if k in ("claude", "openai", "gemini", "langchain", "crewai", "mcp_client", "autogpt")}

    return {
        "window_days": days,
        "summary": {
            "total_requests": stats.get("total_requests", 0),
            "external_requests": external_total,
            "internal_requests": internal_total,
            "unknown_requests": unknown_total,
            "external_pct": round(external_total / total * 100, 1),
        },
        "growth": {
            "external_daily_trend": ext_daily,
            "growth_pct": growth_pct,
            "interpretation": (
                f"External requests {'growing' if growth_pct and growth_pct > 0 else 'declining' if growth_pct and growth_pct < 0 else 'stable'}"
                + (f" at {growth_pct}% over {days}d" if growth_pct is not None else " (insufficient data)")
            ),
        },
        "external_clients": {
            "unique_ips": stats.get("external_unique_ips", 0),
            "by_type": ext_client_types,
            "ai_agent_types": ai_types,
            "ai_agent_total": sum(ai_types.values()),
        },
        "revenue": {
            "total_revenue_usdc": x402_stats.get("estimated_revenue_usdc", 0),
            "external_revenue_usdc": x402_stats.get("external_revenue_usdc", 0.0),
            "internal_paid_calls": x402_stats.get("internal_paid_calls", 0),
            "external_paid_calls": x402_stats.get("external_paid_calls", 0),
            "paid_by_source": x402_stats.get("paid_by_source", {}),
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /analytics/ic — Information Coefficient per dimension per regime
# ---------------------------------------------------------------------------

@app.get("/analytics/ic", tags=["analytics"])
async def get_ic_analytics(
    window_hours: int = Query(24, ge=24, le=168, description="Evaluation window (24, 48, 168)"),
    days: int = Query(30, ge=7, le=90, description="Lookback period in days"),
):
    """Signal quality — Information Coefficient (Spearman rank correlation) per scoring dimension.

    IC measures how well each dimension's scores predict actual future returns.
    Tracked per dimension, per regime, with ICIR (consistency) metric.

    IC interpretation:
      0.05-0.10 = useful signal
      > 0.10 = strong signal
      < 0 = anti-predictive (signal is backwards)
      ICIR > 0.5 = consistent signal quality
    """
    if not _store:
        raise HTTPException(status_code=503, detail="Storage not initialized")

    ic_data = _store.compute_ic(window_hours=window_hours, days=days)

    # Add health status per dimension
    dimensions = ic_data.get("dimensions", {})
    for dim, stats in dimensions.items():
        ic = stats.get("ic")
        if ic is None:
            stats["status"] = "no_data"
        elif ic >= 0.07:
            stats["status"] = "strong"
        elif ic >= 0.03:
            stats["status"] = "active"
        elif ic >= 0.0:
            stats["status"] = "watch"
        else:
            stats["status"] = "weak"

    return {
        "window_hours": window_hours,
        "lookback_days": days,
        "overall_ic": ic_data.get("overall_ic"),
        "total_observations": ic_data.get("total_observations", 0),
        "total_slices": ic_data.get("total_slices", 0),
        "dimensions": dimensions,
        "by_regime": ic_data.get("by_regime", {}),
        "interpretation": {
            "ic_useful": "IC > 0.03 indicates predictive value",
            "ic_strong": "IC > 0.07 indicates strong predictive signal",
            "icir": "ICIR = mean(IC)/std(IC) — measures consistency. ICIR > 0.5 is good.",
            "slices": "Each slice is a cross-asset snapshot (need 3+ assets for valid IC)",
        },
        "last_computed": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /analytics/signal-health — Tournament dashboard data
# ---------------------------------------------------------------------------

@app.get("/analytics/signal-health", tags=["analytics"])
async def get_signal_health():
    """Signal health dashboard — IC per dimension, weights, decay alerts,
    optimizer change log, regime info, and abstain rate.
    Aggregates all scoring pipeline metrics in one call for the dashboard.
    """
    if not _store:
        raise HTTPException(status_code=503, detail="Storage not initialized")

    result: dict = {}

    # 1. IC per dimension (24h and 48h windows)
    for wh in [24, 48]:
        cached = _store.load_kv_json("ic_tracking", f"ic_{wh}h_30d")
        if cached:
            dims = cached.get("dimensions", {})
            for dim, stats in dims.items():
                ic = stats.get("ic")
                if ic is None:
                    stats["status"] = "no_data"
                elif ic >= 0.07:
                    stats["status"] = "strong"
                elif ic >= 0.03:
                    stats["status"] = "active"
                elif ic >= 0.0:
                    stats["status"] = "watch"
                else:
                    stats["status"] = "weak"
            result[f"ic_{wh}h"] = cached
        else:
            result[f"ic_{wh}h"] = None

    # 2. Current weights (from optimizer)
    learned = _store.load_kv_json("weight_optimizer", "learned_weights")
    result["weights"] = learned

    # 3. Optimizer state
    opt_state = _store.load_kv_json("weight_optimizer", "optimizer_state")
    result["optimizer_state"] = opt_state

    # 4. Decay alerts
    decay = _store.load_kv_json("weight_optimizer", "decay_alerts")
    result["decay_alerts"] = decay

    # 5. Change log (last 10 entries)
    change_log = _store.load_kv_json("weight_optimizer", "change_log") or []
    result["change_log"] = change_log[-10:]

    # 6. Config version (from latest fusion result)
    latest_fusion = _store.load_latest("signal_fusion")
    if latest_fusion:
        meta = latest_fusion.get("meta", {})
        result["config_version"] = meta.get("config_version")
        result["regime"] = latest_fusion.get("regime")
        # Count abstain rate
        signals = latest_fusion.get("signals", {})
        total = len(signals)
        abstain = sum(1 for s in signals.values()
                      if isinstance(s, dict) and s.get("label") == "ABSTAIN")
        result["abstain_rate"] = round(abstain / total * 100, 1) if total else 0
        result["total_assets"] = total
        result["abstain_count"] = abstain
    else:
        result["config_version"] = None
        result["regime"] = None
        result["abstain_rate"] = 0

    # 7. Agent cadences
    result["agent_cadences"] = {
        "technical_agent": int(os.getenv("AGENT_CADENCE_TECHNICAL_MIN", "15")),
        "derivatives_agent": int(os.getenv("AGENT_CADENCE_DERIVATIVES_MIN", "15")),
        "whale_agent": int(os.getenv("AGENT_CADENCE_WHALE_MIN", "30")),
        "market_agent": int(os.getenv("AGENT_CADENCE_MARKET_MIN", "30")),
        "narrative_agent": int(os.getenv("AGENT_CADENCE_NARRATIVE_MIN", "60")),
    }

    return result


# ---------------------------------------------------------------------------
# GET /analytics/errors — Error tracking dashboard
# ---------------------------------------------------------------------------

@app.get("/analytics/errors", tags=["analytics"])
async def get_error_analytics(
    days: int = Query(7, ge=1, le=90),
):
    """Error tracking — API errors, payment failures, agent errors."""
    if not _store:
        raise HTTPException(status_code=503, detail="Storage not initialized")

    result = _store.load_error_summary(days=days)
    result["window_days"] = days
    result["last_updated"] = datetime.now(timezone.utc).isoformat()
    return result


# ---------------------------------------------------------------------------
# POST /admin/reset-accuracy — Clear tainted accuracy data (admin only)
# ---------------------------------------------------------------------------
_ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


@app.post("/admin/reset-accuracy", tags=["admin"], include_in_schema=False)
async def reset_accuracy(request: Request):
    """
    Delete all accuracy evaluations and reset snapshot evaluated flags.
    Protected by ADMIN_TOKEN env var. Use when accuracy methodology changes.
    Usage: curl -X POST /admin/reset-accuracy -H "Authorization: Bearer <token>"
    """
    if not _ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="ADMIN_TOKEN not configured")

    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != _ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")

    if not _store:
        raise HTTPException(status_code=503, detail="Storage not initialized")

    result = _store.reset_accuracy_data()
    return {
        "status": "accuracy data reset",
        "accuracy_rows_deleted": result["accuracy_rows_deleted"],
        "snapshots_reset": result["snapshots_reset"],
        "message": "All old evaluations cleared. Snapshots will be re-evaluated with new methodology.",
    }


# ---------------------------------------------------------------------------
# A2A Agent Card — /.well-known/agent.json
# ---------------------------------------------------------------------------
@app.get("/.well-known/agent.json", tags=["discovery"], include_in_schema=False)
async def agent_card():
    """Agent-to-Agent discovery card (Google A2A protocol)."""
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    card: Dict[str, Any] = {
        "name": "Web3 Signals Agent",
        "description": (
            "AI-powered crypto signal intelligence. Fuses whale tracking, "
            "derivatives positioning, technical analysis, narrative momentum, "
            "and market data into scored signals for 20 crypto assets. "
            "Includes LLM-generated cross-dimensional insights."
        ),
        "url": base_url,
        "version": "0.2.0",
        "model_version": "v0.2.0-regime-aware",
        "capabilities": [
            {
                "name": "get_all_signals",
                "description": "Get scored signals for all 20 crypto assets with portfolio summary and LLM insights",
                "endpoint": f"{base_url}/signal",
                "method": "GET",
                "response_fields": [
                    "composite_score", "direction", "label", "dimensions",
                    "regime", "momentum", "confidence", "data_tiers", "velocity",
                ],
            },
            {
                "name": "get_asset_signal",
                "description": "Get signal for a specific crypto asset (e.g. BTC, ETH, SOL)",
                "endpoint": f"{base_url}/signal/{{asset}}",
                "method": "GET",
                "parameters": {"asset": {"type": "string", "enum": [
                    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT",
                    "MATIC", "LINK", "UNI", "ATOM", "LTC", "FIL", "NEAR",
                    "APT", "ARB", "OP", "INJ", "SUI",
                ]}},
            },
            {
                "name": "get_reputation",
                "description": "Rolling 30-day signal accuracy — gradient + binary at 24h/48h windows, per-asset breakdown, full methodology",
                "endpoint": f"{base_url}/performance/reputation",
                "method": "GET",
                "response_fields": [
                    "reputation_score", "accuracy_30d", "by_timeframe",
                    "by_asset", "methodology", "abstention_rate",
                ],
            },
            {
                "name": "get_performance_free",
                "description": "Overall accuracy overview — free tier, no payment required",
                "endpoint": f"{base_url}/performance",
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
        "scoring_model": {
            "type": "multi_agent_fusion",
            "dimensions": ["whale", "technical", "derivatives", "narrative", "market", "trend"],
            "score_range": [0, 100],
            "center": 50,
            "abstain_zone": [38, 62],
            "confidence_model": "gradient_volatility_normalized",
            "regime_detection": "btc_ma30_distance",
            "accuracy_scaling": "per_dimension_per_direction",
        },
        "protocols": {
            "rest": f"{base_url}/docs",
            "openapi": f"{base_url}/openapi.json",
            "mcp_sse": f"{base_url}/mcp/sse",
            "mcp_streamable_http": f"{base_url}/mcp/stream",
            "a2a": f"{base_url}/.well-known/agent.json",
            "agents_md": f"{base_url}/.well-known/agents.md",
        },
        "assets_covered": [
            "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT",
            "MATIC", "LINK", "UNI", "ATOM", "LTC", "FIL", "NEAR", "APT",
            "ARB", "OP", "INJ", "SUI",
        ],
        "update_frequency": "Every 15 minutes",
        "response_format": "application/json",
        "schema_stable": True,
    }
    if _X402_ENABLED:
        card["protocols"]["x402"] = f"{base_url}/signal"
        card["protocols"]["x402_discovery"] = f"{base_url}/.well-known/x402.json"
        card["settlement"] = {
            "protocol": "x402",
            "network": "Base (eip155:8453)",
            "token": "USDC",
            "price_per_call": "$0.001",
            "facilitator": _X402_FACILITATOR_URL,
        }
        card["pricing"] = {
            "paid": {"/signal": "$0.001", "/signal/{asset}": "$0.001", "/performance/reputation": "$0.001"},
            "free": ["/health", "/performance", "/performance/{asset}", "/analytics", "/dashboard", "/docs", "/.well-known/*"],
        }
    else:
        card["pricing"] = "Free"
    return card


# ---------------------------------------------------------------------------
# AGENTS.md — Agentic AI Foundation discovery
# ---------------------------------------------------------------------------
@app.get("/.well-known/agents.md", tags=["discovery"], include_in_schema=False)
async def agents_md():
    """AGENTS.md — Agentic AI Foundation standard for agent discovery."""
    import pathlib
    from fastapi.responses import PlainTextResponse
    agents_path = pathlib.Path(__file__).resolve().parent.parent / "AGENTS.md"
    content = agents_path.read_text() if agents_path.exists() else "# Web3 Signals Agent"
    return PlainTextResponse(content, media_type="text/markdown")


# ---------------------------------------------------------------------------
# /.well-known/x402.json — x402 payment protocol discovery
# ---------------------------------------------------------------------------
@app.get("/.well-known/x402.json", tags=["discovery"], include_in_schema=False)
async def x402_discovery():
    """x402 payment protocol discovery — machine-readable payment config."""
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    if not _X402_ENABLED:
        return {"x402_enabled": False}
    return {
        "x402_version": 2,
        "provider": "Web3 Signals Agent",
        "network": "eip155:8453",
        "token": "USDC",
        "token_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "facilitator": _X402_FACILITATOR_URL,
        "pay_to": _PAY_TO,
        "routes": {
            "/signal": {"method": "GET", "price": "$0.001", "description": "Full 20-asset signal fusion with LLM insights"},
            "/signal/{asset}": {"method": "GET", "price": "$0.001", "description": "Single asset signal (6 dimensions)"},
            "/performance/reputation": {"method": "GET", "price": "$0.001", "description": "30-day rolling accuracy score"},
        },
        "free_routes": ["/health", "/performance", "/analytics", "/dashboard", "/docs", "/.well-known/*"],
        "discovery": {
            "agent_card": f"{base_url}/.well-known/agent.json",
            "agents_md": f"{base_url}/.well-known/agents.md",
            "openapi": f"{base_url}/openapi.json",
            "mcp_sse": f"{base_url}/mcp/sse",
            "mcp_streamable_http": f"{base_url}/mcp/stream",
        },
    }


# ---------------------------------------------------------------------------
# /.well-known/x402 — x402scan-compatible discovery (no .json extension)
# ---------------------------------------------------------------------------
@app.get("/.well-known/x402", tags=["discovery"], include_in_schema=False)
async def x402_discovery_compat():
    """x402scan-compatible discovery endpoint (no .json extension).
    Returns the minimal format expected by npx @agentcash/discovery."""
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    if not _X402_ENABLED:
        return {"version": 1, "resources": []}
    return {
        "version": 1,
        "resources": [
            f"{base_url}/signal",
            f"{base_url}/signal/{{asset}}",
            f"{base_url}/performance/reputation",
        ],
    }


# ---------------------------------------------------------------------------
# /robots.txt — Guide AI crawlers and search engines
# ---------------------------------------------------------------------------
@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    """robots.txt — Guide crawlers to machine-readable endpoints."""
    content = """User-agent: *
Allow: /
Allow: /docs
Allow: /openapi.json
Allow: /.well-known/
Allow: /health
Allow: /performance
Allow: /analytics
Disallow: /api/
Disallow: /admin/
Disallow: /mcp/messages

# AI Agent Discovery
# Agent Card: /.well-known/agent.json
# AGENTS.md: /.well-known/agents.md
# x402 Payments: /.well-known/x402.json
# OpenAPI Spec: /openapi.json
# MCP Transport: /mcp/sse

Sitemap: none
"""
    return PlainTextResponse(content.strip(), media_type="text/plain")


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
# MCP Transports — SSE (legacy) + Streamable HTTP (modern)
# ---------------------------------------------------------------------------
try:
    from mcp.server.sse import SseServerTransport
    from mcp_server.server import mcp as mcp_server_instance
    from starlette.responses import Response

    # --- Legacy SSE transport at /mcp/sse (backward compat) ---
    _mcp_sse_transport = SseServerTransport("/mcp/messages")

    @app.get("/mcp/sse", include_in_schema=False)
    async def mcp_sse_endpoint(request: Request):
        """MCP SSE endpoint — legacy transport for older clients."""
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

    from starlette.routing import Mount
    app.router.routes.append(
        Mount("/mcp/messages", app=_mcp_sse_transport.handle_post_message)
    )
    logger.info("MCP SSE transport mounted at /mcp/sse")

    # --- Streamable HTTP transport (modern, Smithery-compatible) ---
    # Separate path to avoid conflicting with SSE at /mcp/sse + /mcp/messages.
    # Smithery/modern clients POST to: https://...railway.app/mcp/stream
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        # Allow our production domain (default only allows localhost)
        _prod_host = "web3-signals-api-production.up.railway.app"
        mcp_server_instance.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "127.0.0.1:*", "localhost:*", "[::1]:*",
                f"{_prod_host}:*", _prod_host,
                "*.run.tools", "*.run.tools:*",   # Smithery gateway
            ],
            allowed_origins=[
                "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                f"https://{_prod_host}", f"https://{_prod_host}:*",
                "https://*.run.tools", "https://*.run.tools:*",
            ],
        )
        # Set internal path to / so mounted path /mcp/stream IS the endpoint
        mcp_server_instance.settings.streamable_http_path = "/"
        _streamable_app = mcp_server_instance.streamable_http_app()
        app.mount("/mcp/stream", _streamable_app)
        logger.info("MCP Streamable HTTP transport mounted at /mcp/stream")
    except Exception as e:
        logger.warning("MCP Streamable HTTP mount skipped — %s", e)

except ImportError as e:
    logger.warning("MCP transport mount skipped — %s", e)
except Exception as e:
    logger.error("MCP transport mount error — %s", e)


# ---------------------------------------------------------------------------
# Custom OpenAPI schema — inject x-agentcash-auth for x402scan discovery
# ---------------------------------------------------------------------------
_PAID_ROUTES = {"/signal", "/signal/{asset}", "/performance/reputation"}


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(
        title=app.title, version=app.version,
        description=app.description, routes=app.routes,
    )
    for path, methods in schema.get("paths", {}).items():
        if path in _PAID_ROUTES:
            for method_data in methods.values():
                if isinstance(method_data, dict):
                    method_data.setdefault("x-agentcash-auth", {
                        "mode": "x402",
                        "price": "$0.001",
                        "network": "eip155:8453",
                        "token": "USDC",
                    })
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("api.server:app", host="0.0.0.0", port=port, reload=False)
