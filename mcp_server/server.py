"""
Web3 Signals — MCP Server (Intelligent Edition)

Exposes crypto signal intelligence as MCP tools for Claude Desktop, Cursor,
and other MCP-compatible AI assistants.

Tools:
    get_market_briefing   Executive summary — regime, risk, top movers, actionable calls
    get_crypto_price      Live crypto price from market_agent data
    get_all_signals       Teaser signals for top/bottom assets (free tier)
    get_asset_signal      Single asset score + direction (free tier)
    compare_assets        Side-by-side comparison of 2-5 assets (free tier)
    get_health            Agent status, last run, uptime
    get_performance       Signal accuracy tracking (30-day rolling)
    get_asset_performance Per-asset accuracy breakdown
    get_analytics         API usage analytics — requests, clients, trends
    get_x402_stats        x402 micropayment analytics — revenue, paid calls, conversion

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
    "Web3 Signals — AgentMarketSignal",
    instructions=(
        "AgentMarketSignal: AI-powered crypto signal intelligence for 20 assets.\n\n"
        "WHAT YOU CAN DO:\n"
        "• Check crypto prices → get_crypto_price('BTC')\n"
        "• Get buy/sell recommendation → get_asset_signal('BTC')\n"
        "• See what's hot right now → get_market_briefing()\n"
        "• Compare assets → compare_assets('BTC,ETH,SOL')\n"
        "• Check signal accuracy → get_performance()\n\n"
        "Signals scored 0-100: above 62 = buy, below 38 = sell. "
        "Based on whale tracking, technical analysis, derivatives data, "
        "social sentiment, and market trends. Updated every 15 minutes."
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
# Tool: get_market_briefing — Executive summary with actionable intelligence
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
def get_market_briefing() -> str:
    """What should I buy or sell in crypto right now? Returns the top 3 buy and top 3 sell recommendations from 20 cryptocurrencies, plus market regime (trending/ranging), risk level, and momentum. Best starting point for portfolio decisions. Scores range 0-100: above 62 is a buy signal, below 38 is a sell signal."""
    fusion = _get_fusion()
    result = fusion.fuse()

    portfolio = result.get("data", {}).get("portfolio_summary", {})
    signals = result.get("data", {}).get("signals", {})
    timestamp = result.get("timestamp", "unknown")

    # Sort assets by score
    scored = []
    for asset, sig in signals.items():
        score = sig.get("composite_score", 50)
        scored.append((asset, score, sig))
    scored.sort(key=lambda x: x[1], reverse=True)

    # Top buys (highest scores) and sells (lowest scores)
    top_buys = scored[:3]
    top_sells = scored[-3:]
    top_sells.reverse()  # lowest first

    # High conviction signals
    high_conviction_bullish = [
        (a, s, sig) for a, s, sig in scored if s >= 62
    ]
    high_conviction_bearish = [
        (a, s, sig) for a, s, sig in scored if s <= 38
    ]
    neutral_count = len([1 for _, s, _ in scored if 38 < s < 62])

    def _signal_summary(asset, score, sig):
        """Build a concise summary for one asset."""
        direction = sig.get("direction", "?")
        label = sig.get("label", "?")
        dims = sig.get("dimensions", {})
        # Find strongest dimension
        dim_scores = {}
        for dim_name, dim_data in dims.items():
            if isinstance(dim_data, dict):
                dim_scores[dim_name] = dim_data.get("score", 50)
        if dim_scores:
            strongest_dim = max(dim_scores, key=dim_scores.get)
            weakest_dim = min(dim_scores, key=dim_scores.get)
        else:
            strongest_dim = weakest_dim = "unknown"
        return {
            "asset": asset,
            "score": score,
            "direction": direction,
            "label": label,
            "strongest_dimension": strongest_dim,
            "weakest_dimension": weakest_dim,
        }

    regime = portfolio.get("market_regime", "unknown")
    risk = portfolio.get("risk_level", "unknown")

    briefing = {
        "briefing_type": "market_intelligence",
        "timestamp": timestamp,
        "market_regime": regime,
        "risk_level": risk,
        "signal_momentum": portfolio.get("signal_momentum", "unknown"),
        "regime_context": (
            f"Market is in {regime} regime with {risk} risk. "
            + ("Trend-following signals are amplified. " if regime == "TRENDING" else "")
            + ("Mean-reversion plays are favored. " if regime == "RANGING" else "")
            + f"{len(high_conviction_bullish)} bullish and "
            f"{len(high_conviction_bearish)} bearish high-conviction signals. "
            f"{neutral_count} assets in neutral/abstain zone."
        ),
        "top_buys": [_signal_summary(*x) for x in top_buys],
        "top_sells": [_signal_summary(*x) for x in top_sells],
        "high_conviction_count": {
            "bullish": len(high_conviction_bullish),
            "bearish": len(high_conviction_bearish),
            "neutral": neutral_count,
        },
        "total_assets_tracked": len(signals),
        "data_freshness": timestamp,
    }

    return json.dumps(briefing, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_crypto_price — Live price from market_agent data
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
def get_crypto_price(asset: str) -> str:
    """What is the current price of Bitcoin, Ethereum, or any major crypto? Returns the latest USD price, 24-hour price change percentage, trading volume, and market cap. Updated every 15 minutes from CoinGecko and Binance. Supports 20 assets: BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, MATIC, LINK, UNI, ATOM, LTC, FIL, NEAR, APT, ARB, OP, INJ, SUI. Example: get_crypto_price('BTC')"""
    asset = asset.upper().strip()
    valid = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT",
             "MATIC", "LINK", "UNI", "ATOM", "LTC", "FIL", "NEAR",
             "APT", "ARB", "OP", "INJ", "SUI"]
    if asset not in valid:
        return json.dumps({"error": f"Unknown asset '{asset}'. Valid: {', '.join(valid)}"})

    store = _get_store()
    market = store.load_latest("market_agent")
    if not market:
        return json.dumps({"error": "Market data not yet available. The pipeline runs every 15 minutes — try again shortly."})

    per_asset = market.get("data", {}).get("per_asset", {})
    data = per_asset.get(asset, {})
    if not data:
        return json.dumps({"error": f"No price data for {asset}. Data pipeline may still be initializing."})

    return json.dumps({
        "asset": asset,
        "price_usd": data.get("price"),
        "change_24h_pct": data.get("change_24h_pct"),
        "volume_24h_usd": data.get("volume_24h"),
        "market_cap_usd": data.get("market_cap"),
        "volume_status": data.get("volume_status", "unknown"),
        "volume_spike_ratio": data.get("volume_spike_ratio"),
        "timestamp": market.get("timestamp"),
        "_tip": f"For a buy/sell signal with AI analysis, try get_asset_signal('{asset}')",
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_all_signals
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
def get_all_signals() -> str:
    """Get buy/sell signals for all 20 major cryptocurrencies including Bitcoin, Ethereum, Solana, and more. Returns a 0-100 composite score and direction (bullish/bearish/neutral) for each asset, plus portfolio summary with market regime and risk level. Updated every 15 minutes. For full dimension breakdown and AI insights, use the paid REST API."""
    fusion = _get_fusion()
    result = fusion.fuse()

    portfolio = result.get("data", {}).get("portfolio_summary", {})
    signals = result.get("data", {}).get("signals", {})

    # Sort assets by composite_score
    scored = []
    for asset, sig in signals.items():
        score = sig.get("composite_score", 50)
        scored.append((asset, score, sig))
    scored.sort(key=lambda x: x[1], reverse=True)

    # Top 3 bullish (highest) + bottom 3 bearish (lowest)
    top_bullish = scored[:3]
    top_bearish = scored[-3:]
    top_bearish.reverse()  # lowest first

    def _teaser(asset, score, sig):
        return {
            "asset": asset,
            "composite_score": score,
            "direction": sig.get("direction", "?"),
            "label": sig.get("label", "?"),
        }

    return json.dumps({
        "timestamp": result.get("timestamp"),
        "portfolio_summary": {
            "market_regime": portfolio.get("market_regime"),
            "risk_level": portfolio.get("risk_level"),
            "signal_momentum": portfolio.get("signal_momentum"),
        },
        "top_bullish": [_teaser(*x) for x in top_bullish],
        "top_bearish": [_teaser(*x) for x in top_bearish],
        "total_assets_tracked": 20,
        "_upgrade": (
            "Full 20-asset data with 6-dimension breakdown "
            "(whale, technical, derivatives, narrative, market, trend) "
            "and LLM insights available via paid REST API at "
            "https://web3-signals-api-production.up.railway.app/signal "
            "($0.001 USDC on Base via x402 protocol)"
        ),
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_asset_signal
# ---------------------------------------------------------------------------
VALID_ASSETS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOT",
    "MATIC", "LINK", "UNI", "ATOM", "LTC", "FIL", "NEAR", "APT",
    "ARB", "OP", "INJ", "SUI",
]


@mcp.tool(annotations={"readOnlyHint": True})
def get_asset_signal(asset: str) -> str:
    """Is BTC bullish or bearish right now? Get a 0-100 buy/sell score for any cryptocurrency. Returns composite score, direction (bullish/bearish/neutral), signal label (STRONG BUY to STRONG SELL), and momentum. Supports: BTC, ETH, SOL, BNB, XRP, ADA, AVAX, DOT, MATIC, LINK, UNI, ATOM, LTC, FIL, NEAR, APT, ARB, OP, INJ, SUI. For the full 6-dimension breakdown with whale, technical, and derivatives analysis, use the paid REST API."""
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

    # Extract momentum direction only (not full dict)
    momentum = sig.get("momentum", {})
    momentum_direction = momentum.get("direction", "unknown") if isinstance(momentum, dict) else "unknown"

    return json.dumps({
        "asset": asset,
        "timestamp": result.get("timestamp"),
        "composite_score": sig.get("composite_score"),
        "direction": sig.get("direction"),
        "label": sig.get("label"),
        "momentum": momentum_direction,
        "market_context": {
            "regime": portfolio.get("market_regime"),
            "risk_level": portfolio.get("risk_level"),
        },
        "_upgrade": (
            f"Full 6-dimension breakdown for {asset} with LLM analysis: "
            f"GET https://web3-signals-api-production.up.railway.app/signal/{asset} "
            f"($0.001 USDC on Base via x402)"
        ),
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: compare_assets — Side-by-side comparison
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
def compare_assets(assets: str) -> str:
    """Which crypto should I buy — BTC, ETH, or SOL? Compare 2-5 cryptocurrencies ranked by signal strength. Input: comma-separated tickers (e.g. 'BTC,ETH,SOL'). Returns ranked comparison with scores, direction, and verdict. Use for portfolio allocation decisions."""
    asset_list = [a.strip().upper() for a in assets.split(",") if a.strip()]
    if len(asset_list) < 2:
        return json.dumps({"error": "Need at least 2 assets to compare. Example: 'BTC,ETH,SOL'"})
    if len(asset_list) > 5:
        return json.dumps({"error": "Maximum 5 assets per comparison."})

    invalid = [a for a in asset_list if a not in VALID_ASSETS]
    if invalid:
        return json.dumps({"error": f"Invalid assets: {invalid}. Valid: {VALID_ASSETS}"})

    fusion = _get_fusion()
    result = fusion.fuse()
    signals = result.get("data", {}).get("signals", {})
    portfolio = result.get("data", {}).get("portfolio_summary", {})

    comparison = []
    for asset in asset_list:
        sig = signals.get(asset, {})
        score = sig.get("composite_score", 50)
        momentum = sig.get("momentum", {})
        momentum_direction = momentum.get("direction", "unknown") if isinstance(momentum, dict) else "unknown"
        comparison.append({
            "asset": asset,
            "composite_score": score,
            "direction": sig.get("direction", "?"),
            "label": sig.get("label", "?"),
            "momentum": momentum_direction,
        })

    # Rank by score
    comparison.sort(key=lambda x: x["composite_score"], reverse=True)
    for i, c in enumerate(comparison):
        c["rank"] = i + 1

    return json.dumps({
        "comparison": comparison,
        "market_context": {
            "regime": portfolio.get("market_regime"),
            "risk_level": portfolio.get("risk_level"),
        },
        "timestamp": result.get("timestamp"),
        "verdict": (
            f"Strongest: {comparison[0]['asset']} ({comparison[0]['composite_score']}/100 — "
            f"{comparison[0]['direction']}). "
            f"Weakest: {comparison[-1]['asset']} ({comparison[-1]['composite_score']}/100 — "
            f"{comparison[-1]['direction']})."
        ),
        "_upgrade": (
            "Full 6-dimension breakdown (whale, technical, derivatives, narrative, "
            "market, trend) and LLM insights available via paid REST API at "
            "https://web3-signals-api-production.up.railway.app/signal "
            "($0.001 USDC on Base via x402 protocol)"
        ),
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_health
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
def get_health() -> str:
    """Is AgentMarketSignal working? Check the real-time status of all 5 AI data pipelines (whale tracking, technical analysis, derivatives, narrative sentiment, market data) and the signal fusion engine. Returns last run times, durations, and any errors."""
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
@mcp.tool(annotations={"readOnlyHint": True})
def get_performance() -> str:
    """How accurate are these crypto signals? Returns 30-day rolling accuracy metrics showing how often buy/sell predictions were correct. Includes overall accuracy percentage, reputation score (0-100), and breakdowns by asset and timeframe (24h/48h)."""
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
            "timeframes": ["24h", "48h"],
            "price_source": "CoinGecko",
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_asset_performance
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
def get_asset_performance(asset: str) -> str:
    """How accurate are the signals for BTC specifically? Get per-asset accuracy metrics for any cryptocurrency. Returns 30-day rolling accuracy, total signals evaluated, and comparison to overall accuracy."""
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
# Tool: get_analytics — API usage analytics
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
def get_analytics(days: int = 7) -> str:
    """Who is using AgentMarketSignal? See API usage statistics including total requests, unique clients, response times, breakdowns by endpoint and client type (AI agents, browsers, scripts). Useful for understanding adoption."""
    if days < 1:
        days = 1
    if days > 90:
        days = 90

    store = _get_store()
    stats = store.load_api_analytics(days=days)

    return json.dumps({
        "window_days": days,
        "total_requests": stats["total_requests"],
        "unique_clients": stats["unique_ips"],
        "avg_response_ms": stats["avg_duration_ms"],
        "by_endpoint": stats["by_endpoint"],
        "by_client_type": stats["by_user_agent_type"],
        "requests_per_day": stats["requests_per_day"],
        "top_user_agents": stats["top_user_agents"][:10],
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool: get_x402_stats — x402 micropayment analytics
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
def get_x402_stats(days: int = 30) -> str:
    """How much revenue has AgentMarketSignal generated? View x402 micropayment analytics including total paid calls, revenue in USDC, payment conversion rate, and daily payment timeline."""
    if days < 1:
        days = 1
    if days > 90:
        days = 90

    store = _get_store()
    stats = store.load_x402_analytics(days=days)
    total_challenges = stats["total_402_challenges"]
    total_paid = stats["total_paid_calls"]
    conversion = (
        round(total_paid / total_challenges * 100, 1)
        if total_challenges > 0 else 0
    )

    return json.dumps({
        "window_days": days,
        "price_per_call": "$0.001 USDC",
        "network": "Base (eip155:8453)",
        "total_paid_calls": total_paid,
        "estimated_revenue_usdc": stats["estimated_revenue_usdc"],
        "total_402_challenges": total_challenges,
        "total_payment_failures": stats["total_payment_failures"],
        "conversion_rate_pct": conversion,
        "by_endpoint": stats["by_endpoint"],
        "by_client_type": stats["by_client_type"],
        "paid_per_day": stats["paid_per_day"],
        "avg_paid_latency_ms": stats["avg_paid_latency_ms"],
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
