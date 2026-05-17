# Web3 Signals MCP

> Multi-agent crypto signal intelligence. 20 assets, 5 data dimensions, scored 0–100, refreshed every 15 min.

**Live API** — https://web3-signals-api-production.up.railway.app
**Dashboard** — https://web3-signals-api-production.up.railway.app/dashboard
**MCP endpoint** — `https://web3-signals-api-production.up.railway.app/mcp/sse`

---

## What it does

Five independent data agents (whale flows, technicals, derivatives, narrative, market microstructure) each score every asset 0–100. A fusion engine combines them into a single composite signal with a directional label, momentum tracking, and an LLM-generated rationale. The system grades its own predictions at 24h and 48h horizons against actual price moves — no self-reported accuracy.

## Why it's interesting

- **Per-asset weight learning via IC analysis.** Each asset gets its own dimension weights, fitted from Spearman/Pearson/Kendall correlations between past dimension scores and forward returns. Different assets respond to different signals.
- **Walk-forward backtesting with FDR correction.** Benjamini–Hochberg adjustment on indicator significance to avoid false discoveries when testing dozens of features.
- **Platt-scaled probability calibration.** Raw scores → calibrated probabilities so "75" means a real 75% directional likelihood, not just a higher number than 70.
- **x402 HTTP micropayments.** Paid endpoints settle $0.001 USDC on Base mainnet per call via Coinbase's CDP facilitator. Payment IS authentication — no API keys, no signup, no OAuth.
- **MCP-native.** Exposes itself to Claude Desktop, Cursor, and any MCP-compatible client over SSE. AI agents can query it with natural language.
- **Adaptive regime gating.** Abstain zone widens/narrows with the Fear & Greed index; bullish-bias contrarian boost is dampened in confirmed BTC downtrends.

## Quick start

### Hit the API directly
```bash
curl https://web3-signals-api-production.up.railway.app/signal/BTC
```
(`/signal*` and `/performance/reputation` require an x402 payment header; everything else is free.)

### Use over MCP (Claude Desktop / Cursor / Windsurf)
```json
{
  "mcpServers": {
    "web3-signals": {
      "url": "https://web3-signals-api-production.up.railway.app/mcp/sse"
    }
  }
}
```
Then prompt: *"What's the BTC signal right now?"* or *"Show me top 3 buys."*

### Run locally
```bash
git clone https://github.com/manavaga/web3-signals-mcp.git
cd web3-signals-mcp
cp .env.example .env             # fill in REDDIT_CLIENT_ID, ANTHROPIC_API_KEY, etc.
pip install -r requirements.txt
python -m api                    # API on :8000
python -m orchestrator.runner --once   # one fusion cycle
```

## Project layout

```
api/                FastAPI server, dashboard, x402 middleware
mcp_server/         MCP tool definitions (stdio + SSE)
signal_fusion/      Weighted fusion, Platt calibration, meta-learner
whale_agent/        On-chain flow tracking (Etherscan + exchange wallets)
technical_agent/    RSI, MACD, MA, Bollinger (Binance)
derivatives_agent/  Funding rate, OI, long/short ratio
narrative_agent/    Reddit, news, CoinGecko trending, LLM sentiment
market_agent/       Price, volume, Fear & Greed
shared/             Storage (Postgres / SQLite), base agent, profile loader
orchestrator/       15-minute agent scheduler + accuracy evaluator
tools/              Backtesting, IC fitting, walk-forward, weight optimizer
```

## Stack

Python 3.13 · FastAPI · PostgreSQL · pandas / numpy / scikit-learn · Anthropic Claude (LLM rationales) · Coinbase CDP x402 facilitator · Railway (deploy)

## Performance evaluation

Snapshots are saved on every fusion cycle. At 24h and 48h each directional call is graded against the actual price move (CoinGecko + Binance). Neutral signals are skipped (only directional calls count). Accuracy is `AVG(gradient_score) × 100` where gradient ∈ [0, 1] depending on whether the move was in the predicted direction and how large it was. See `/performance/reputation` for the live numbers.

## Development notes

This codebase was built in pair-programming with Anthropic's Claude. Most commits have a `Co-Authored-By: Claude` trailer — kept intentionally to document the workflow. Architectural decisions, model choices (IC-based weighting, FDR correction, Platt scaling), and the production-readiness criteria (no-deploy-without-backtest hard rule, walk-forward embargoing) were human-driven; Claude was used for implementation, refactoring, and code review.

## License

MIT
