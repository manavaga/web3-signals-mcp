# Web3 Signals — Project Context

**Last updated**: 2026-04-06
**Branch**: v2
**Status**: Regime-aware scoring implemented. 3 critical fixes applied: ADX-based 4-regime detection, correlated indicator cleanup, mean-reversion suppression in trends. Ready for re-backtest.

---

## Current State

### What's Working
- **3 active agents**: technical (Binance klines), derivatives (Binance Futures), market (F&G, VIX, volume, order book)
- **Signal fusion**: 12-phase pipeline in `signal_fusion/engine.py` (many phases disabled)
- **API + dashboard**: FastAPI with x402 micropayments, 5-tab dashboard
- **Storage**: Dual-mode Postgres (Railway) / SQLite (local)
- **Orchestrator**: 15-min scheduler running all agents

### What's Been Fixed (2026-04-06 regime-aware update)
- **ADX-based regime detection**: 4 regimes (trending_up, trending_down, ranging, volatile) replace old 2-state system
- **Correlated indicators cut**: MFI and StochRSI removed from scoring (85% correlated with RSI). Weights redistributed to RSI, OBV, Trend
- **Regime-aware technical scoring**: RSI/BB mean-reversion signals suppressed in trending regimes (the root cause of 17% accuracy)
- **189 tests passing** (was 184)

### What's Been Fixed (2026-04-05 implementation)
- **Dead agents cut**: narrative + exchange_flow removed completely (3-agent system now)
- **Technical agent**: 9 indicators (was 4). Added OBV, MFI, ROC, StochRSI, BB/Keltner squeeze, z-scores
- **Market agent**: 8 factors (was 2). Added S&P, DXY, NASDAQ, stablecoin supply, BTC dominance, VIX ROC
- **Derivatives agent**: OI change % now computed (was hardcoded 0.0), OI-weighted funding added
- **Relative features**: 3 cross-sectional features (asset vs BTC momentum, strength, funding)
- **Walk-forward backtest**: 7-day embargo, expanding window, gradient scoring, CWA metric
- **Weight optimizer**: Grid search 141 combos, IC-driven sub-weights, confidence tiers
- **Deploy gate**: Blocks changes that regress CWA (3-condition check)
- **Abstain sweep**: 405 combinations per asset
- **184 tests** passing (was 60)

### What's Still Needed
- **Full data backtest**: yfinance blocked locally — re-run on Railway with real macro data
- **Derivatives historical data**: Need to collect 90+ days of live agent snapshots for Phase 2
- **IC sub-weight optimization**: Per-indicator weights within each dimension
- **Mean-reversion vs trend**: Current default sub-weights over-weight RSI/MFI in trending markets

### Blacklisted Assets (anti-predictive)
INJ (20.7%), ATOM (24.5%), OP (41.9%) — keep blacklisted until per-asset backtest proves >50% CWA

### Enabled Assets (12)
BTC, ETH, SOL, BNB, XRP, AVAX, LINK, LTC, UNI, FIL, ARB, SUI

---

## Decisions Made (2026-04-05)

1. **Kill narrative + exchange_flow agents completely** — don't show on dashboard, don't run, don't include in config
2. **3-agent system**: technical + derivatives + market only (optimize fewer agents first)
3. **No hardcoded weights** — all weights determined by per-asset backtesting
4. **Hybrid backtest approach**: Phase 1 (6mo historical, tech+market) → Phase 2 (90d, all 3 agents)
5. **Walk-forward validation** with 7-day embargo, expanding window
6. **Grid search for weights**: ~100 valid combinations for 3 dimensions, exhaustive
7. **Deploy gate**: No config/scoring change deploys without backtest showing CWA improvement
8. **12h signal cadence** (down from 15min) — daily candles don't change every 15 min
9. **New indicators to add**: OBV, MFI, StochRSI, ROC, BB/Keltner squeeze, z-scores (technical); NASDAQ/QQQ, stablecoin supply, BTC dominance, DXY (market); OI-weighted funding, OI change tracking (derivatives)

---

## What's Next (Implementation Queue)

### Phase 1: Clean Foundation
- [ ] Cut narrative + exchange_flow from pipeline, config, orchestrator, dashboard
- [ ] Delete disabled phases from engine.py (~185 lines)
- [ ] Delete velocity.py (359 lines dead code)
- [ ] Clean default.yaml (~500 lines of disabled config)
- [ ] Switch to 12h signal cadence

### Phase 2: Data Quality
- [ ] Add new technical indicators (OBV, MFI, StochRSI, ROC, squeeze, z-scores)
- [ ] Fix market agent (fetch S&P/DXY/QQQ, add stablecoin supply, BTC dominance)
- [ ] Fix derivatives agent (OI change tracking, OI-weighted funding)
- [ ] Add relative features in scoring pipeline

### Phase 3: Backtest Engine
- [ ] Build historical data fetcher (6mo for Phase 1, 90d for Phase 2)
- [ ] Build walk-forward backtest with guardrails (embargo, no leakage)
- [ ] Build per-asset weight optimizer (grid search, IC-driven)
- [ ] Build deploy gate (backtest_baseline.json comparison)
- [ ] Build abstain calibration sweep

### Phase 4: Targets + Evaluation
- [ ] Add TP/SL to every directional signal (ATR-based + ML prediction)
- [ ] Evaluate neutrals (price within ATR band = correct)
- [ ] Implement CWA as primary metric
- [ ] Per-asset accuracy tracking

### Phase 5: Self-Learning
- [ ] Backtest-gated gradient descent for weight updates
- [ ] Concept drift detection
- [ ] A/B testing framework

---

## Key Files to Watch
| File | Why |
|------|-----|
| `signal_fusion/engine.py` | Core fusion logic — being simplified |
| `signal_fusion/profiles/default.yaml` | All scoring config — being cleaned |
| `backtest.py` | Being rewritten for walk-forward |
| `config.yaml` | Agent weights — will be replaced by backtest results |
| `assets.yaml` | Per-asset overrides, blacklist |

---

## Design Doc
Full approved design: `docs/plans/2026-04-05-hybrid-backtest-design.md`
