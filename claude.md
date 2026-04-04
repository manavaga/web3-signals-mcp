# Web3 Signals

AI-powered crypto signal intelligence — 5 data agents score 20 assets, fused via 7-step pipeline.

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
