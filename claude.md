# Web3 Signals

AI-powered crypto signal intelligence — data agents score assets, fused via 7-step pipeline.

## HARD RULES (non-negotiable, override everything else)

### 1. No deploy without backtest
NO scoring, weight, threshold, agent, or pipeline change can be deployed without passing a backtest gate.
- Run backtest on historical data BEFORE any deploy
- Compare proposed accuracy vs current accuracy per asset
- Only deploy if proposed >= current for overall CWA
- Present backtest results to the user before deploying
- This applies to ALL changes: config, weights, thresholds, new agents, removed agents, new indicators

### 2. No hardcoded weights
NEVER propose or hardcode dimension weights. All weights must be determined by backtesting at the per-asset level.
- Different assets have different leading indicators
- Only IC analysis and backtest optimization can determine weights
- Start new dimensions at equal weight (1/N), let the optimizer find the right values
- Per-asset weight profiles come from data, not human intuition

### 3. No placeholder agents
If an agent doesn't produce real data, cut it completely. Don't include it with weight=0, don't show it on dashboard, don't run it in orchestrator.
- Only add an agent back when it has a real implementation AND backtest shows positive IC
- Don't half-implement agents

### 4. Research before code
Stop making reactive code changes. Research and validate first, then implement.
- When an issue is raised, RESEARCH first (read code, understand the problem fully)
- Present findings and proposed approach to the user
- Get explicit approval before making changes
- Never chain multiple untested changes in one push

### 5. Key research learnings (from 5-expert analysis, 2026-04-05)

**Leading indicators by consensus (add these):**
- OBV (On-Balance Volume) — volume-confirmed trend, leads price by 1-3 candles
- MFI (Money Flow Index) — volume-weighted RSI, catches distribution before price drops
- Bollinger/Keltner squeeze — volatility compression precedes breakouts
- Funding rate extremes — >0.05% or <-0.03% mean crowded trades about to unwind
- Stablecoin supply ratio — money entering/leaving crypto ecosystem
- NASDAQ/QQQ correlation — crypto follows risk-on/risk-off macro moves
- DXY (Dollar Index) — inverse correlation with crypto
- Z-score transformations — normalize all indicators for cross-asset comparison

**What NOT to add (low IC or noisy):**
- Twitter/social sentiment — too noisy for quantitative scoring, save for qualitative LLM overlay
- On-chain whale tracking — fundamentally broken architecture (monitors transfers, not flows)
- Elliott Wave / harmonic patterns — subjective, not backtestable
- Google Trends — too lagging for 24h/48h predictions

**Asymmetries discovered:**
- Whale/exchange_flow bullish signals are toxic (27% accuracy) but bearish signals are strong (61%)
- Market dimension is strongest bullish predictor (IC=+0.31)
- Technical dimension is strongest bearish predictor (IC=+0.64)
- Different assets have different leading indicators — BTC is macro-driven, small caps are technical-driven

**Optimal parameters (from research, to be validated by backtest):**
- RSI: 14-period (standard), but add StochRSI for momentum divergence
- MACD: 12/26/9 (standard), add histogram slope for momentum direction
- Bollinger Bands: 20/2 (standard), add bandwidth for squeeze detection
- ATR: 14-period for stop-loss calibration
- Walk-forward embargo: 7+ days minimum between train/test sets
- Minimum 20 evaluated signals per asset before learning kicks in

### 6. Persistent context
See `docs/CONTEXT.md` for current project state, decisions, and what's in progress. Updated each session.

---

## File Map (decision tree)

**Changing scoring logic?**
→ `scoring/pipeline.py` (orchestrator), `scoring/dimensions.py` (formulas), `scoring/modifiers.py` (regime/abstain/targets)

**Changing weights or thresholds?**
→ `config.yaml` (all scoring config), `assets.yaml` (per-asset overrides)

**Adding/modifying data sources?**
→ `agents/technical.py`, `agents/derivatives.py`, `agents/market.py`, `agents/narrative.py`, `agents/exchange_flow.py`

**Changing API routes?**
→ `api/server.py` (routes), `api/middleware.py` (x402/CORS/cache)

**Changing database schema?**
→ `storage/db.py`

**Changing scheduling?**
→ `orchestrator/runner.py`, agent cadences in `config.yaml`

**Running backtests?**
→ `python3 -m tools.backtest --quick` or `--full`

## Architecture Rules

1. **types.py is the contract** — all modules communicate through frozen dataclasses
2. **Config is Pydantic-validated** — crash at startup if weights don't sum to 1.0
3. **Scoring functions are pure** — inputs in, outputs out, no shared state
4. **Shadow mode learning** — optimizer computes weights but doesn't apply until 90 days
5. **Circuit breakers on agents** — 3 failures → 30min cooldown

## Connectivity Status (what's wired vs dead code)

### Fully Connected (used in production)
- `scoring/types.py` — contracts used by all modules
- `scoring/config.py` — loaded at startup by API + orchestrator
- `scoring/dimensions.py` — called by `scoring/pipeline.py`
- `scoring/modifiers.py` — called by `scoring/pipeline.py`
- `scoring/pipeline.py` — called by API (`/signal`) + orchestrator (fusion cycle)
- `agents/technical.py` — run by orchestrator on cadence
- `agents/derivatives.py` — run by orchestrator on cadence
- `agents/market.py` — run by orchestrator on cadence
- `storage/db.py` — all core methods connected (save, load, perf snapshots, analytics)
- `api/server.py` — all routes active
- `api/middleware.py` — x402, CORS, caching, UsageTrackingMiddleware all wired
- `orchestrator/runner.py` — agent scheduling + fusion + learning evaluation cycle

### Connected but in Shadow Mode
- `learning/evaluation.py` — called by orchestrator after fusion; `gradient_score()` evaluates old signals, results saved to `performance_accuracy`
- `learning/optimizer.py` — called by orchestrator in shadow mode; computes IC + proposes weights, saves to KV store but **never auto-applies weights** (requires 90 days minimum)

### Placeholder Agents (weight=0.0 in config)
- `agents/narrative.py` — skeleton agent, weight 0.0, runs but produces no signal impact. **Connect when**: LunarCrush/Reddit data source is implemented
- `agents/exchange_flow.py` — skeleton agent, weight 0.0, runs but produces no signal impact. **Connect when**: Binance order book + taker volume integration is built

### Storage Methods — Usage Status
| Method | Status | Called By |
|--------|--------|-----------|
| `save()` / `load_latest()` / `load_all_latest()` | ACTIVE | orchestrator, API |
| `save_performance_snapshot()` | ACTIVE | orchestrator (fusion) |
| `save_performance_accuracy()` | ACTIVE | orchestrator (evaluation) |
| `save_dimension_scores()` | ACTIVE | orchestrator (evaluation) |
| `load_unevaluated_snapshots()` | ACTIVE | orchestrator (evaluation) |
| `load_accuracy_stats()` | ACTIVE | API `/performance` |
| `save_api_request()` | ACTIVE | UsageTrackingMiddleware |
| `load_api_analytics()` | ACTIVE | API `/analytics` |
| `load_x402_analytics()` | ACTIVE | API `/analytics/x402` |
| `save_error_event()` | ACTIVE | UsageTrackingMiddleware |
| `load_error_summary()` | ACTIVE | API `/analytics/errors` |
| `save_kv()` / `load_kv()` | AVAILABLE | not currently called (available for future use) |
| `save_kv_json()` / `load_kv_json()` | ACTIVE | shadow optimizer saves proposed weights |
| `load_recent()` | AVAILABLE | only used by `tools/backtest.py` |
| `load_history()` | AVAILABLE | only used by `tools/backtest.py` |
| `count_rows()` | AVAILABLE | not currently called |

## Running

```bash
# Start API
python3 -m api

# Run orchestrator (once)
python3 -m orchestrator.runner --once

# Run orchestrator (continuous)
python3 -m orchestrator.runner --interval 3600

# Quick backtest
python3 -m tools.backtest --quick
```

## Testing

```bash
python3 -m pytest tests/ -v
```
