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
