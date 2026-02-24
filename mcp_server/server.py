"""
Web3 Signals — MCP Server

Exposes crypto signal intelligence as MCP tools for Claude Desktop, Cursor,
and other MCP-compatible AI assistants.

Tools:
    get_all_signals     Full fusion: portfolio + 20 scored signals + LLM insights
    get_asset_signal    Single asset signal (e.g. BTC, ETH, SOL)
    get_health          Agent status, last run, uptime
    get_performance     Signal accuracy tracking
    get_asset_perf      Per-asset accuracy

Run:
    python -m mcp_server.server          # stdio mode (default)
    python -m mcp_server.server --sse    # SSE mode for remote connections
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

# Add project root to path so we can import shared modules
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from shared.storage import Storage
from signal_fusion.engine import SignalFusion

# ---------------------------------------------------------------------------
# MCP Server setup
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "Web3 Signals",
    instructions=(
        "AI-powered crypto signal intelligence for 20 assets. "
        "Fuses whale activity, derivatives positioning, technical analysis, "
        "narrative momentum, and market data into scored signals with LLM insights. "
        "Use get_all_signals for a full portfolio view, or get_asset_signal for a "
        "specific asset like BTC or ETH. Use get_health to check agent status, "
        "and get_performance to see signal accuracy tracking."
    ),
)

# Globals (lazy-initialized on first tool call)
_store: Storage | None = None
_fusion: SignalFusion | None = None


def _get_store() -> Storage:
    global _store
    if _store is None:
        _store = Storage()
    return _store


def _get_fusion() -> SignalFusion:
    global _fusion
    if _fusion is None:
        _fusion = SignalFusion()
    return _fusion


# ---------------------------------------------------------------------------
# Tool: get_all_signals
# ---------------------------------------------------------------------------
@mcp.tool()
def get_all_signals() -> str:
    """
    Get scored signals for all 20 crypto assets.

    Returns portfolio summary (top buys, top sells, market regime, risk level),
    composite scores for each asset (0-100), dimension breakdowns (whale, technical,
    derivatives, narrative, market), momentum tracking, and LLM-generated insights.

    Assets covered: BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, MATIC, LINK,
    UNI, ATOM, LTC, FIL, NEAR, APT, ARB, OP, INJ, SUI.

    Data refreshes every 15 minutes from 5 AI data collection agents.
    """
    fusion = _get_fusion()
    result = fusion.fuse()
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_asset_signal
# ---------------------------------------------------------------------------
VALID_ASSETS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT",
    "MATIC", "LINK", "UNI", "ATOM", "LTC", "FIL", "NEAR", "APT",
    "ARB", "OP", "INJ", "SUI",
]


@mcp.tool()
def get_asset_signal(asset: str) -> str:
    """
    Get the signal for a specific crypto asset.

    Args:
        asset: The crypto asset ticker (e.g. BTC, ETH, SOL). Case-insensitive.

    Returns the composite score (0-100), label (STRONG BUY to STRONG SELL),
    dimension scores (whale, technical, derivatives, narrative, market),
    momentum vs previous run, and LLM insight if available.

    Valid assets: BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, MATIC, LINK,
    UNI, ATOM, LTC, FIL, NEAR, APT, ARB, OP, INJ, SUI.
    """
    asset = asset.upper().strip()
    if asset not in VALID_ASSETS:
        return json.dumps({
            "error": f"Invalid asset '{asset}'. Valid: {VALID_ASSETS}"
        })

    fusion = _get_fusion()
    result = fusion.fuse()

    signals = result.get("data", {}).get("signals", {})
    sig = signals.get(asset, {})
    portfolio = result.get("data", {}).get("portfolio_summary", {})

    return json.dumps({
        "asset": asset,
        "timestamp": result.get("timestamp"),
        "signal": sig,
        "market_context": {
            "regime": portfolio.get("market_regime"),
            "risk_level": portfolio.get("risk_level"),
            "signal_momentum": portfolio.get("signal_momentum"),
        },
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_health
# ---------------------------------------------------------------------------
@mcp.tool()
def get_health() -> str:
    """
    Check the health and status of all signal agents.

    Returns the status of each agent (technical, derivatives, market,
    narrative, whale), when they last ran, duration, error counts,
    fusion status, and storage backend info.
    """
    store = _get_store()
    agent_names = [
        "technical_agent", "derivatives_agent", "market_agent",
        "narrative_agent", "whale_agent",
    ]
    agent_status: dict[str, Any] = {}

    for name in agent_names:
        latest = store.load_latest(name)
        if latest:
            agent_status[name] = {
                "status": latest.get("status", "unknown"),
                "last_run": latest.get("timestamp"),
                "duration_ms": latest.get("meta", {}).get("duration_ms"),
                "errors": len(latest.get("meta", {}).get("errors", [])),
            }
        else:
            agent_status[name] = {"status": "no_data", "last_run": None}

    fusion_latest = store.load_latest("signal_fusion")
    fusion_status = {
        "status": fusion_latest.get("status") if fusion_latest else "no_data",
        "last_run": fusion_latest.get("timestamp") if fusion_latest else None,
    }

    return json.dumps({
        "status": "healthy",
        "storage_backend": store.backend,
        "agents": agent_status,
        "fusion": fusion_status,
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_performance
# ---------------------------------------------------------------------------
@mcp.tool()
def get_performance() -> str:
    """
    Get signal reputation and accuracy tracking — rolling 30-day performance.

    Shows overall accuracy percentage across 24h/48h/7d timeframes,
    per-asset accuracy breakdown, and reputation score.
    Needs at least 24 hours of data to produce meaningful results.
    """
    store = _get_store()

    stats = store.load_accuracy_stats(days=30)
    total_snapshots = store.count_snapshots(days=30)

    if stats["total"] == 0:
        return json.dumps({
            "status": "collecting_data",
            "message": "Performance tracking is active. Accuracy data will appear after 24h of signal history.",
            "snapshots_collected": total_snapshots,
        })

    accuracy = round(stats["hits"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0

    return json.dumps({
        "status": "active",
        "reputation_score": int(round(accuracy)),
        "accuracy_30d": accuracy,
        "signals_evaluated": stats["total"],
        "signals_correct": stats["hits"],
        "by_timeframe": stats["by_timeframe"],
        "by_asset": stats["by_asset"],
        "snapshots_collected_30d": total_snapshots,
        "methodology": {
            "direction_extraction": "score >60 = bullish, <40 = bearish, 40-60 = neutral",
            "neutral_threshold": "price move <=2% = correct for neutral signals",
            "scoring": "binary (hit/miss)",
            "window": "30-day rolling",
            "timeframes": ["24h", "48h", "7d"],
            "price_source": "CoinGecko",
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_asset_performance
# ---------------------------------------------------------------------------
@mcp.tool()
def get_asset_performance(asset: str) -> str:
    """
    Get performance tracking for a specific crypto asset.

    Args:
        asset: The crypto asset ticker (e.g. BTC, ETH, SOL). Case-insensitive.

    Shows how accurately our signals predicted this asset's price movement,
    including per-asset accuracy in the rolling 30-day window.
    """
    asset = asset.upper().strip()
    if asset not in VALID_ASSETS:
        return json.dumps({
            "error": f"Invalid asset '{asset}'. Valid: {VALID_ASSETS}"
        })

    store = _get_store()
    stats = store.load_accuracy_stats(days=30)

    if stats["total"] == 0:
        return json.dumps({
            "status": "collecting_data",
            "message": "Performance tracking is active. Check back after 24h.",
        })

    asset_accuracy = stats["by_asset"].get(asset)
    if asset_accuracy is None:
        return json.dumps({"error": f"No accuracy data for '{asset}'"})

    overall = round(stats["hits"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0

    return json.dumps({
        "asset": asset,
        "accuracy_30d": asset_accuracy,
        "overall_accuracy_30d": overall,
        "reputation_score": int(round(overall)),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    """Run the MCP server."""
    transport = "stdio"
    if "--sse" in sys.argv:
        transport = "sse"

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
