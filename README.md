# Web3 Signals MCP

> Crypto signal intelligence for AI agents. 5 data dimensions, 20 assets, refreshed every 15 minutes.

**Version**: 0.1.0
**Live API**: `https://web3-signals-api-production.up.railway.app`
**MCP Endpoint**: `https://web3-signals-api-production.up.railway.app/mcp/sse`
**Dashboard**: [web3-signals-api-production.up.railway.app/dashboard](https://web3-signals-api-production.up.railway.app/dashboard)

---

## What It Is

A signal fusion engine that scores 20 crypto assets from 0-100 by combining 5 independent data agents:

| Agent | Weight | Sources |
|-------|--------|---------|
| Whale | 30% | On-chain flows, exchange movements, large transactions |
| Technical | 25% | RSI, MACD, Moving Averages (Binance) |
| Derivatives | 20% | Funding rate, open interest, long/short ratio |
| Narrative | 15% | Reddit, Google News, CoinGecko trending, LLM sentiment |
| Market | 10% | Price, volume, Fear & Greed Index |

Each agent runs every 15 minutes. Scores are fused into a composite signal with directional labels (`STRONG BUY` to `STRONG SELL`), momentum tracking, and LLM-generated cross-dimensional insights.

## What Problem It Solves

AI agents and trading systems need structured, multi-dimensional crypto intelligence — not raw price feeds. This API delivers scored, opinionated signals that combine what whales are doing, what derivatives markets are pricing, what the crowd is saying, and what technicals show — fused into a single actionable score with an LLM explanation of why.

## Target Horizon

- **Signal refresh**: Every 15 minutes
- **Accuracy evaluation**: 24h, 48h, 7d windows
- **Best for**: Swing trades (hours to days), portfolio risk monitoring, market regime detection
- **Not designed for**: Sub-minute scalping or HFT

## Assets Covered

`BTC` `ETH` `SOL` `BNB` `XRP` `ADA` `AVAX` `DOT` `MATIC` `LINK` `UNI` `ATOM` `LTC` `FIL` `NEAR` `APT` `ARB` `OP` `INJ` `SUI`

---

## Connect via MCP

Add to your MCP config (Claude Desktop, Cursor, Windsurf, etc.):

```json
{
  "mcpServers": {
    "web3-signals": {
      "url": "https://web3-signals-api-production.up.railway.app/mcp/sse"
    }
  }
}
```

Then ask your AI: *"What are the current crypto signals?"* or *"Get me the BTC signal"*

### MCP Tools

| Tool | Description |
|------|-------------|
| `get_all_signals` | Full portfolio: 20 scored signals + portfolio summary + LLM insights |
| `get_asset_signal` | Single asset signal with market context |
| `get_health` | Agent status, last run times, error counts |
| `get_performance` | Rolling 30-day accuracy across 24h/48h/7d timeframes |
| `get_asset_performance` | Per-asset accuracy breakdown |

---

## REST API

### Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /signal` | All 20 asset signals with portfolio summary |
| `GET /signal/{asset}` | Single asset signal (e.g. `/signal/BTC`) |
| `GET /performance/reputation` | 30-day rolling accuracy score |
| `GET /performance/{asset}` | Per-asset accuracy breakdown |
| `GET /health` | Agent status and uptime |
| `GET /analytics` | API usage analytics |
| `GET /api/history` | Historical signal runs (paginated) |
| `GET /docs` | OpenAPI documentation |
| `GET /dashboard` | Live signal intelligence dashboard |

### Example: Single Asset Signal

```bash
curl https://web3-signals-api-production.up.railway.app/signal/BTC
```

```json
{
  "asset": "BTC",
  "timestamp": "2026-02-24T21:49:42.513414+00:00",
  "signal": {
    "composite_score": 31.7,
    "label": "MODERATE SELL",
    "direction": "sell",
    "dimensions": {
      "whale": {
        "score": 7.9,
        "label": "STRONG SELL",
        "detail": "25 accumulate, 33 sell (ratio 43%); exchange inflow",
        "weight": 0.3
      },
      "technical": {
        "score": 35.2,
        "label": "MODERATE SELL",
        "detail": "RSI 30; MACD bullish; trend bearish",
        "weight": 0.25
      },
      "derivatives": {
        "score": 25.0,
        "label": "STRONG SELL",
        "detail": "L/S 0.69",
        "weight": 0.2
      },
      "narrative": {
        "score": 63.5,
        "label": "MODERATE BUY",
        "detail": "vol 0.97 (106 mentions); LLM neutral; trending; 3 sources",
        "weight": 0.15
      },
      "market": {
        "score": 60.0,
        "label": "MODERATE BUY",
        "detail": "-0.8%; F&G 8 extreme fear",
        "weight": 0.1
      }
    },
    "momentum": "degrading",
    "prev_score": 42.1,
    "llm_insight": "Whale capitulation intensifying — 33 sellers dominating with exchange inflow. Derivatives flipped to strong sell. Divergence: narrative and market fear remain bullish, suggesting classic capitulation setup..."
  },
  "market_context": {
    "regime": "extreme_fear",
    "risk_level": "high",
    "signal_momentum": "degrading"
  }
}
```

### Example: Portfolio Summary

```bash
curl https://web3-signals-api-production.up.railway.app/signal
```

```json
{
  "status": "success",
  "timestamp": "2026-02-24T21:49:42+00:00",
  "data": {
    "portfolio_summary": {
      "top_buys": [
        {"asset": "ETH", "score": 53.2, "label": "NEUTRAL", "conviction": "moderate"},
        {"asset": "SUI", "score": 50.7, "label": "NEUTRAL", "conviction": "moderate"},
        {"asset": "DOT", "score": 49.4, "label": "NEUTRAL", "conviction": "moderate"}
      ],
      "top_sells": [
        {"asset": "SOL", "score": 36.9, "label": "MODERATE SELL"},
        {"asset": "XRP", "score": 34.0, "label": "MODERATE SELL"},
        {"asset": "BTC", "score": 31.7, "label": "MODERATE SELL"}
      ],
      "market_regime": "extreme_fear",
      "risk_level": "high",
      "signal_momentum": "degrading",
      "assets_improving": 0,
      "assets_degrading": 6
    },
    "signals": {
      "BTC": { "composite_score": 31.7, "label": "MODERATE SELL", "..." : "..." },
      "ETH": { "composite_score": 53.2, "label": "NEUTRAL", "..." : "..." }
    }
  }
}
```

### Example: Performance / Reputation

```bash
curl https://web3-signals-api-production.up.railway.app/performance/reputation
```

```json
{
  "status": "active",
  "reputation_score": 72,
  "accuracy_30d": 72.3,
  "signals_evaluated": 840,
  "signals_correct": 607,
  "by_timeframe": {
    "24h": {"total": 280, "hits": 196, "accuracy": 70.0},
    "48h": {"total": 280, "hits": 201, "accuracy": 71.8},
    "7d":  {"total": 280, "hits": 210, "accuracy": 75.0}
  },
  "by_asset": {
    "BTC": 75.0,
    "ETH": 70.0,
    "SOL": 68.5
  },
  "methodology": {
    "direction_extraction": "score >60 = bullish, <40 = bearish, 40-60 = neutral",
    "neutral_threshold": "price move <=2% = correct for neutral signals",
    "scoring": "binary (hit/miss)",
    "window": "30-day rolling",
    "timeframes": ["24h", "48h", "7d"],
    "price_source": "CoinGecko"
  }
}
```

### Signal Labels

| Score Range | Label | Direction |
|-------------|-------|-----------|
| 80-100 | STRONG BUY | bullish |
| 60-79 | MODERATE BUY | bullish |
| 40-59 | NEUTRAL | neutral |
| 20-39 | MODERATE SELL | bearish |
| 0-19 | STRONG SELL | bearish |

---

## Performance Tracking

The system tracks its own signal accuracy — no self-reported claims:

- **Snapshots** captured every 12 hours (1 per asset, max 40/day)
- **Evaluation** at 24h, 48h, and 7d windows against actual price movement
- **Direction match**: Did the predicted direction (bullish/bearish/neutral) match the actual price move?
- **Neutral threshold**: Price move <=2% counts as correct for neutral signals
- **Price source**: CoinGecko (independent, no API key needed)
- **Window**: 30-day rolling, recalculated every evaluation cycle

---

## Discovery Protocols

| Protocol | Endpoint | Standard |
|----------|----------|----------|
| **x402** | `/signal`, `/signal/{asset}` | [HTTP 402 Micropayments](https://www.x402.org/) (Coinbase) |
| **MCP SSE** | `/mcp/sse` | [Model Context Protocol](https://modelcontextprotocol.io) (Anthropic) |
| **A2A** | `/.well-known/agent.json` | [Agent-to-Agent](https://google.github.io/A2A/) (Google) |
| **AGENTS.md** | `/.well-known/agents.md` | [Agentic AI Foundation](https://agenticaialliance.org/) |
| **OpenAPI** | `/docs` | OpenAPI 3.0 |

---

## x402 Micropayments

Payment IS authentication. No API keys, no signup, no OAuth.

AI agents pay $0.001 USDC per call on Base mainnet. The x402 protocol handles discovery, payment, and settlement automatically via the [Coinbase CDP Facilitator](https://x402.org/facilitator).

### Paid Endpoints ($0.001/call)
| Endpoint | What you get |
|----------|-------------|
| `GET /signal` | All 20 signals + portfolio summary + LLM insights |
| `GET /signal/{asset}` | Single asset signal with 5 dimensions |
| `GET /performance/reputation` | 30-day rolling accuracy score |

### Free Endpoints
`/health`, `/dashboard`, `/analytics`, `/docs`, `/.well-known/*`, `/mcp/sse`

### How it works
1. Agent calls `GET /signal` → gets `402 Payment Required` with payment instructions
2. Agent signs USDC payment on Base → retries with `PAYMENT-SIGNATURE` header
3. Facilitator verifies payment → endpoint returns data
4. Settlement happens on-chain in <2 seconds

Agents using x402-compatible clients (Otto, Questflow, Fluora, Oops!402) handle this automatically.

---

## Project Structure

```
/api                  FastAPI server, dashboard, middleware
/mcp_server           MCP tool definitions (stdio + SSE)
/signal_fusion        Weighted score fusion engine
/whale_agent          On-chain flow tracking
/technical_agent      RSI, MACD, MA analysis
/derivatives_agent    Funding rate, OI, L/S ratio
/narrative_agent      Reddit, News, Trending, LLM sentiment
/market_agent         Price, volume, Fear & Greed
/shared               Storage layer, base agent, profile loader
/orchestrator         15-minute agent runner
README.md
AGENTS.md
```

---

## Self-Hosting

```bash
git clone https://github.com/manavaga/web3-signals-mcp.git
cd web3-signals-mcp

cp .env.example .env
# Edit .env with your API keys

pip install -r requirements.txt
python -m api.server
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `REDDIT_CLIENT_ID` | Yes | Reddit API credentials |
| `REDDIT_CLIENT_SECRET` | Yes | Reddit API secret |
| `ANTHROPIC_API_KEY` | No | Enables LLM insights (Claude Haiku) |
| `DATABASE_URL` | No | Postgres URL (falls back to SQLite) |
| `PORT` | No | Server port (default: 8000) |

---

## Roadmap

### Near-term (building now)
- **Calibration buckets** — Group signals by score range (e.g. 70-80) and track accuracy per bucket. Answers: "When we say 75, how often is that actually bullish?" *(needs 24h+ of accuracy data)*
- **Magnitude scoring** — Move beyond binary hit/miss to measure how much the predicted move captured vs actual move. *(needs 1 week of data)*

### Medium-term
- **Confidence-weighted penalties** — Penalize high-conviction misses more than low-conviction ones. A "STRONG BUY" that dumps should hurt reputation more than a "MODERATE BUY" that goes flat. *(needs calibration data)*
- **Correlation vs BTC baseline** — Compare signal accuracy against a naive "just follow BTC" strategy. If we can't beat that, the signal isn't adding value. *(needs 30 days of data)*

### Future
- **x402 micropayments** — Pay-per-signal via HTTP 402
- **Additional assets** — Expand beyond 20
- **More data sources** — Twitter/X, Farcaster, CryptoPanic (currently disabled, pending API access)

---

## License

MIT
