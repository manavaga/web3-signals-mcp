# Hybrid Backtest-Driven Signal Optimization

**Date:** 2026-04-05
**Status:** Approved
**Author:** Web3 Signals Team

## Objective

Replace hardcoded weights with per-asset, data-driven weights determined by backtesting. Add missing leading indicators identified through competitive research. Implement a deploy gate that blocks any scoring or config change that cannot demonstrate backtest improvement over the current baseline.

This document covers four major workstreams:

1. Agent data overhaul -- add proven indicators, fix broken fields, cut dead weight
2. Hybrid two-phase backtest architecture with walk-forward validation
3. Per-asset weight optimization with confidence tiers
4. Deploy gate that enforces backtest approval before any change ships

---

## Table of Contents

1. [Agent Data Overhaul](#1-agent-data-overhaul)
2. [Hybrid Backtest Architecture](#2-hybrid-backtest-architecture)
3. [Data Leakage Guardrails](#3-data-leakage-guardrails)
4. [Per-Asset Weight Optimizer](#4-per-asset-weight-optimizer)
5. [Deploy Gate](#5-deploy-gate)
6. [Indicators NOT Added](#6-indicators-not-added-with-reasons)
7. [Key Research Findings Driving This Design](#7-key-research-findings-driving-this-design)

---

## 1. Agent Data Overhaul

### 1.1 Technical Agent -- Add 6 Indicator Groups

All indicators below are computed from existing Binance kline data. No new API calls are required.

| Indicator | What It Does | Evidence |
|---|---|---|
| OBV (On-Balance Volume) | Cumulative volume flow, divergence detection | #1 ranked leading indicator by crypto TA experts, top-5 ML feature |
| MFI (Money Flow Index) | Volume-weighted RSI | Strictly superior to RSI per all experts surveyed |
| ROC(1d), ROC(7d), ROC(30d) | Rate of change at 3 periods | 1-week momentum = strongest standalone factor in crypto |
| Stochastic RSI | RSI of RSI, more sensitive to fast moves | Better than RSI for crypto's rapid regime changes |
| BB/Keltner Squeeze | Bollinger Bands contracting inside Keltner Channels | #2 ranked leading signal, detects volatility compression before breakout |
| Z-scores of RSI, MACD, BB | Rolling z-score normalization (50-period window) | ML research: +30-80% IC improvement over raw indicator values |

**Implementation note:** Each indicator is computed per asset using a rolling window. Z-score normalization uses a 50-period lookback to avoid forward contamination.

### 1.2 Market Agent -- Fix Broken Fields + Add 4 New Sources

**Fixes** (config exists in `market/profiles/default.yaml` but data is never actually fetched):

| Field | Fix | Rationale |
|---|---|---|
| S&P 500 | Fetch via `yfinance ^GSPC` | Coincident risk signal, currently returning null |
| DXY | Fetch via `yfinance DX-Y.NYB` | -0.65 correlation with BTC, currently returning null |
| `breadth_status` | Remove hardcoded `"neutral"`, replace with BTC dominance calculation | Current value is always `"neutral"`, contributing zero information |

**New additions:**

| Source | Data Provider | Rationale |
|---|---|---|
| NASDAQ/QQQ | `yfinance QQQ` | 0.87 correlation with BTC post-ETF approval |
| Stablecoin supply | DefiLlama API (free, no key required) | 1-4 week leading indicator, ranked top signal by 3/5 experts |
| BTC dominance | CoinGecko `/global` endpoint | Alt rotation signal -- rising BTC dominance is bearish for alts |
| VIX rate of change | Computed from existing VIX data | VIX transitions (direction of change) matter more than absolute VIX level |

### 1.3 Derivatives Agent -- 2 Fixes

| Issue | Current Behavior | Fix |
|---|---|---|
| OI change % | Hardcoded `0.0` -- never computed | Track previous OI values in storage, compute actual `(current_oi - prev_oi) / prev_oi * 100`. This field currently wastes ~15% of the derivatives dimension score. |
| OI-weighted funding | Not implemented | Add `funding_rate * open_interest` as a new feature. ML scientist's #1 ranked feature with IC of -0.15 to -0.20 (contrarian: extreme funding predicts reversal). |

### 1.4 Relative Features (Cross-Sectional)

Computed in the scoring pipeline (not in individual agents), these capture how an asset behaves relative to BTC:

| Feature | Formula | Expected IC |
|---|---|---|
| Relative momentum | `asset_RSI - BTC_RSI` | +0.09 to +0.13 |
| Relative strength | `asset_24h_return - BTC_24h_return` | +0.08 to +0.12 |
| Relative positioning | `asset_funding - BTC_funding` | -0.10 (contrarian) |

These are injected between Phase 1 (independent agent scoring) and Phase 2 (composite calculation) in the existing 12-phase pipeline.

### 1.5 Cut Completely

| Component | Current State | Reason for Removal |
|---|---|---|
| Narrative agent | Placeholder returning hardcoded `50` for all assets | Zero information content. Remove from pipeline, config, orchestrator, and dashboard. |
| Exchange flow agent | Placeholder returning hardcoded `50` for all assets | Zero information content. Remove completely. |
| Gold/XAU | Config placeholder | Spurious correlation -- disappears after controlling for DXY. |
| Social sentiment | Not implemented | Fastest alpha decay (~4-8h half-life), mostly lagging by the time signals arrive. |

**After this overhaul, the system reduces from 5 agents to 3 active agents:** Technical, Market, Derivatives.

---

## 2. Hybrid Backtest Architecture

The backtest runs in two phases because data availability differs across agent types. Technical and market data has deep history (6+ months). Derivatives data (funding rates, OI) has shallow history (60-90 days at best from most exchanges).

### 2.1 Phase 1: Deep Technical + Market (6 Months)

**Data window:** 180 days of daily candles + macro data.

**Purpose:** Determine which technical and market indicators are leading vs. lagging for each specific asset.

**NOT used:** Derivatives data (history too short for statistically significant IC measurement).

**Process -- for each asset, for each day N:**

```
1. Compute all technical indicators using ONLY candles up to day N
2. Compute all market indicators using ONLY data available on day N
3. Record actual price change at N+24h and N+48h
4. Compute gradient score per indicator vs actual outcome
5. Compute IC (Spearman rank correlation) per indicator per asset
```

**Output:** Per-asset IC rankings for ~20 technical + market indicators. This tells us which indicators actually predict price direction for each specific asset.

### 2.2 Phase 2: Full 3-Agent Backtest (60-90 Days)

**Data window:** 60-90 days of all agent data (technical + market + derivatives).

**Purpose:** Find optimal dimension weights per asset across all 3 agents, using Phase 1's indicator rankings to inform which sub-indicators get weight within each dimension.

**Process -- for each asset, for each day N:**

```
1. Score technical dimension (using Phase 1's best indicators for this asset)
2. Score market dimension (using Phase 1's best indicators for this asset)
3. Score derivatives dimension
4. Grid search over weight combinations (see Section 4.1)
5. Evaluate each combination against actual 24h/48h price outcomes
6. Select weights that maximize CWA (Coverage-Weighted Accuracy)
```

**Output:** Per-asset optimal weights for `technical / market / derivatives`.

### 2.3 Evaluation Windows

The system produces predictions for both 24h and 48h price moves. The backtest evaluates BOTH windows for every signal generated.

**For each signal emitted on day N:**

```python
price_at_signal = close_price[day_N]
price_24h       = close_price[day_N + 1]
price_48h       = close_price[day_N + 2]

pct_change_24h  = (price_24h - price_at_signal) / price_at_signal * 100
pct_change_48h  = (price_48h - price_at_signal) / price_at_signal * 100

gradient_24h    = gradient_score(direction, pct_change_24h, noise_pct, strong_pct)
gradient_48h    = gradient_score(direction, pct_change_48h, noise_pct, strong_pct)
```

**For ABSTAIN signals:**

```python
if abs(pct_change) > 1 * ATR:
    result = "abstain_miss"    # We missed a tradeable move
else:
    result = "abstain_correct" # Correctly avoided noise
```

The `abstain_miss_rate` is a key metric: it measures how often the system sits out when it should have been in. A high abstain miss rate means the abstain thresholds are too aggressive.

### 2.4 Walk-Forward Folds (Expanding Window)

All validation uses expanding-window walk-forward to prevent data leakage. Each fold trains on all available history up to that point, never on future data. A 7-day embargo gap between training and test prevents autocorrelation leakage.

**Phase 1 folds (180 days):**

| Fold | Train Window | Embargo | Test Window |
|---|---|---|---|
| 1 | Day 1 -- 90 | Day 91 -- 97 | Day 98 -- 120 |
| 2 | Day 1 -- 112 | Day 113 -- 119 | Day 120 -- 142 |
| 3 | Day 1 -- 135 | Day 136 -- 142 | Day 143 -- 165 |
| 4 | Day 1 -- 158 | Day 159 -- 165 | Day 166 -- 180 |

**Phase 2 folds (90 days):**

| Fold | Train Window | Embargo | Test Window |
|---|---|---|---|
| 1 | Day 1 -- 45 | Day 46 -- 52 | Day 53 -- 67 |
| 2 | Day 1 -- 60 | Day 61 -- 67 | Day 68 -- 82 |
| 3 | Day 1 -- 75 | Day 76 -- 82 | Day 83 -- 90 |

The expanding window (as opposed to rolling) ensures that later folds have strictly more training data, which better matches production behavior where the system always has access to its full history.

---

## 3. Data Leakage Guardrails

Data leakage is the single biggest risk in any backtest system. A leaked backtest produces artificially inflated metrics that collapse in production. Every guardrail below is enforced programmatically, not by convention.

| Guardrail | What It Prevents | Enforcement |
|---|---|---|
| Temporal cutoff | Using future data for indicator computation | All indicators computed with `candles[:day_N]`. Assert that no candle timestamp exceeds day N. |
| Walk-forward only | Training on test data | Train and test windows never overlap. Expanding window with 7-day embargo between them. |
| 7-day embargo | Autocorrelation leakage (test outcomes influenced by training tail) | Configurable gap between last training day and first test day. Default: 7 days. |
| No future MA/ATR | Moving averages or ATR using future candles | All rolling computations use `data[:i]`. Unit test validates by comparing indicator values at each step. |
| Survivorship bias | Testing assets before they existed or had meaningful liquidity | Each asset has a `first_available_date`. Skip if `day_N < first_available_date`. |
| No indicator snooping | Optimizing indicator parameters (e.g., RSI period) on the test set | Indicator params are fixed from `config.yaml` before backtest starts. Never tuned during backtest runs. |
| Minimum sample size | Drawing statistical conclusions from too few data points | Require 50+ scored signals per asset for IC computation. Flag assets with insufficient data. |

**Testing:** A dedicated `test_leakage_guardrails.py` test suite will inject known future data and verify that every guardrail raises an exception or produces a warning.

---

## 4. Per-Asset Weight Optimizer

### 4.1 Dimension Weight Optimization (Level 1)

Grid search over weight combinations for the three remaining dimensions:

```
technical:    [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]
market:       [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]
derivatives:  remainder (1.0 - technical - market)
```

**Constraints:**
- All weights >= 0.05
- All weights <= 0.70
- Sum must equal 1.0

This produces approximately 66 valid combinations per asset (11 x 11 grid minus constraint violations).

**Scoring function for each weight combination:**

```python
combined_score = (
    0.4 * CWA_24h +
    0.4 * CWA_48h +
    0.2 * (1.0 - abstain_miss_rate)
)
```

The 40/40/20 split prioritizes prediction accuracy equally across both time horizons while penalizing excessive abstention.

**Selection:** The weight combination that maximizes `combined_score` averaged across all test folds is selected for that asset.

### 4.2 Indicator Sub-Weight Optimization (Level 2)

Within each dimension, individual indicator sub-weights are set by normalized IC values from Phase 1:

```python
# For each indicator i within a dimension, for a given asset:
ic_values = [compute_ic(indicator_i, asset) for indicator_i in dimension_indicators]

# Clip anti-predictive indicators to zero
ic_clipped = [max(0, ic) for ic in ic_values]

# Normalize to sum to 1.0
total = sum(ic_clipped)
sub_weights = [ic / total for ic in ic_clipped] if total > 0 else equal_weights
```

This means:
- Indicators with negative IC (anti-predictive) get zero weight for that asset
- Indicators with higher IC get proportionally more influence
- Sub-weights are asset-specific -- RSI might matter for BTC but not for SOL

### 4.3 Confidence Levels

Not all assets have enough backtest history for reliable per-asset optimization. The confidence tier determines how much the system trusts per-asset weights:

| Signals Per Asset | Confidence | Action |
|---|---|---|
| >= 80 | High | Use per-asset optimized weights directly |
| 50 -- 79 | Medium | Use per-asset weights, flag for manual review |
| 30 -- 49 | Low | Fall back to tier-average weights (average across large-cap / mid-cap / small-cap grouping) |
| < 30 | Insufficient | Fall back to equal weights (0.33 / 0.33 / 0.33) |

**Tier groupings:**
- Large-cap: BTC, ETH, SOL, BNB, XRP
- Mid-cap: ADA, AVAX, DOT, MATIC, LINK, LTC
- Small-cap: UNI, ATOM, FIL, NEAR, APT, ARB, OP, INJ, SUI

### 4.4 Output Format

The optimizer writes results to `backtest_baseline.json` in the repository root. This file is committed to version control and serves as both the production weight source and the deploy gate baseline.

```json
{
  "version": "2026-04-05T14:30:00Z",
  "overall_cwa": 0.342,
  "overall_accuracy_24h": 0.581,
  "overall_accuracy_48h": 0.563,
  "overall_abstain_miss_rate": 0.187,
  "assets": {
    "BTC": {
      "weights": {
        "technical": 0.40,
        "market": 0.35,
        "derivatives": 0.25
      },
      "confidence": "high",
      "signal_count": 142,
      "metrics": {
        "cwa_24h": 0.38,
        "cwa_48h": 0.35,
        "accuracy_24h": 0.62,
        "accuracy_48h": 0.59,
        "abstain_miss_rate": 0.15,
        "coverage": 0.45
      },
      "ic_rankings": {
        "technical": {
          "obv_divergence": 0.14,
          "mfi": 0.11,
          "roc_7d": 0.10,
          "bb_keltner_squeeze": 0.09,
          "rsi_zscore": 0.08,
          "stochastic_rsi": 0.06,
          "roc_1d": 0.04,
          "roc_30d": 0.03
        },
        "market": {
          "dxy_roc": 0.12,
          "stablecoin_supply_growth": 0.10,
          "nasdaq_correlation": 0.08,
          "vix_roc": 0.05,
          "btc_dominance": 0.03
        },
        "derivatives": {
          "oi_weighted_funding": 0.18,
          "taker_ratio_change": 0.11,
          "oi_change_pct": 0.07
        }
      },
      "abstain_thresholds": {
        "bearish_min_distance": 5,
        "bullish_min_distance": 4,
        "regime_multiplier_ranging": 1.2
      }
    }
  }
}
```

---

## 5. Deploy Gate

### 5.1 Gate Logic

Every change to scoring config or pipeline code must pass the deploy gate before it can ship. The gate runs a full backtest with the proposed changes and compares results against the committed baseline.

**Gate conditions (ALL must pass):**

```python
# Condition 1: Overall CWA must not regress
assert proposed_overall_cwa >= baseline_overall_cwa

# Condition 2: No individual asset drops by more than 15%
for asset in all_assets:
    if asset not in blacklist:
        assert proposed_cwa[asset] >= baseline_cwa[asset] * 0.85

# Condition 3: Abstain miss rate stays controlled
for asset in all_assets:
    if asset not in blacklist:
        assert proposed_abstain_miss_rate[asset] <= 0.30
```

**On failure:** The gate prints a comparison table showing exactly which conditions failed and by how much:

```
DEPLOY GATE FAILED

Overall CWA:  0.342 (baseline) -> 0.328 (proposed)  FAIL (-4.1%)

Per-asset regressions:
  Asset  | Baseline CWA | Proposed CWA | Change
  -------|--------------|--------------|--------
  ETH    | 0.35         | 0.28         | -20.0%  FAIL (> 15% drop)
  SOL    | 0.31         | 0.30         | -3.2%   OK
  ...

Abstain miss rate violations:
  Asset  | Rate   | Threshold
  -------|--------|----------
  DOT    | 0.35   | 0.30       FAIL
```

### 5.2 Baseline Management

- `backtest_baseline.json` is committed to the repository root
- Updated ONLY when a backtest produces strictly better overall CWA
- Git history tracks every baseline evolution, providing a full audit trail
- The baseline is never manually edited -- only the backtest can write it

**Workflow:**

```bash
# Run full backtest with deploy gate
python3 -m tools.backtest --full --gate

# If gate passes and CWA improved, update baseline
python3 -m tools.backtest --full --gate --update-baseline

# CI integration (runs on every PR that touches scoring files)
python3 -m tools.backtest --full --gate --ci
```

### 5.3 Abstain Calibration

Per-asset abstain thresholds are found by grid sweep over these parameters:

```yaml
bearish_min_distance:       [2, 3, 4, 5, 6, 7, 8, 10, 12]
bullish_min_distance:       [2, 3, 4, 5, 6, 7, 8, 10, 12]
regime_multiplier_ranging:  [0.8, 1.0, 1.2, 1.5, 2.0]
```

This produces 9 x 9 x 5 = 405 combinations per asset. Each combination is scored:

```python
abstain_score = (
    0.4 * CWA +
    0.3 * accuracy +
    0.3 * (1.0 - abstain_miss_rate)
)
```

The combination that maximizes `abstain_score` across test folds becomes that asset's production abstain thresholds.

### 5.4 Target Metrics

These thresholds define what "good" looks like and determine whether an asset is worth keeping in the active signal set:

| Metric | Minimum Acceptable | Good | Excellent |
|---|---|---|---|
| Accuracy (directional 24h) | > 55% | > 60% | > 65% |
| Accuracy (directional 48h) | > 53% | > 58% | > 63% |
| Coverage (non-abstain rate) | > 30% | > 40% | > 50% |
| Abstain miss rate | < 30% | < 20% | < 15% |
| CWA (Coverage-Weighted Accuracy) | > 0.20 | > 0.30 | > 0.40 |

**Blacklisting:** Assets that cannot hit "Minimum Acceptable" on CWA (> 0.20) after full optimization get blacklisted. Blacklisted assets are excluded from:
- API signal output
- Dashboard display
- Deploy gate per-asset checks
- Overall CWA calculation

Currently blacklisted based on prior analysis: INJ (20.7% accuracy), ATOM (24.5%).

---

## 6. Indicators NOT Added (With Reasons)

Every indicator below was considered during the competitive research phase and explicitly rejected. This section exists to prevent re-investigating the same dead ends.

| Indicator | Reason to Skip |
|---|---|
| Gold / XAU | Spurious correlation -- disappears after controlling for DXY. Confirmed by banker consensus across multiple expert sources. |
| Social media sentiment | Fastest alpha decay (~4-8h half-life). By the time it is aggregated and scored, the move has already happened. Mostly lagging. High overfit risk in backtest. |
| Whale wallet tracking | 30-40% false positive rate (influencer-grade analysis). Already cut from the pipeline in a prior phase. |
| Fibonacci retracement levels | Self-fulfilling prophecy gives it a razor-thin edge, but that edge disappears in high-volatility regimes (which is when signals matter most). |
| Elliott Wave | Core fractal assumption does not hold in narrative-driven crypto markets. Expert TA consensus: not applicable to crypto. |
| Google Trends | Update frequency too slow for 24/48h signal windows. Potentially useful for monthly regime detection, but out of scope for this system's time horizons. |

---

## 7. Key Research Findings Driving This Design

### 7.1 Indicator Rankings by Evidence Strength

Ranked by IC (Information Coefficient = Spearman rank correlation) for 24h forward returns:

| Rank | Indicator | IC Range | Category |
|---|---|---|---|
| 1 | OI-weighted funding rate z-score | -0.15 to -0.20 | Derivatives (contrarian) |
| 2 | Volume anomaly score (z-score) | +0.12 to +0.15 | Technical |
| 3 | Taker buy/sell ratio change | +0.10 to +0.13 | Derivatives |
| 4 | RSI of OBV | +0.09 to +0.12 | Technical (engineered) |
| 5 | BTC-relative momentum | +0.10 to +0.13 | Cross-sectional |
| 6 | DXY rate of change | ~-0.65 correlation | Market (Granger-causes BTC) |
| 7 | Stablecoin supply growth | +0.08 to +0.15 | Market (1-4 week lead) |
| 8 | NASDAQ correlation | 0.87 with BTC | Market (post-ETF regime) |

### 7.2 Optimal Prediction Horizons by Data Type

Not all data types predict the same time horizons. This informs how we weight time-decayed signals:

| Data Type | Optimal Prediction Horizon | Implication |
|---|---|---|
| Derivatives (funding, OI, liquidations) | 4 -- 24h | Most relevant for our 24h window |
| Volume / microstructure | 8 -- 24h | Strong for 24h, moderate for 48h |
| Technical (transformed/z-scored) | 12 -- 48h | Covers both windows well |
| Macro (VIX, DXY, S&P 500) | 24h -- 7d | Better for 48h window and regime context |
| On-chain (stablecoin flows, TVL) | 3 -- 30d | Too slow for our 24/48h windows; use only for regime detection |

### 7.3 Key Asymmetries

These asymmetries explain why the system uses direction-aware weighting (different weights for bullish vs. bearish leans):

| Asymmetry | Bullish Edge | Bearish Edge | Implication |
|---|---|---|---|
| RSI extremes | Oversold (< 22-25) works well | Overbought (> 82-85) works poorly | RSI oversold is a stronger signal than overbought in crypto |
| Bollinger Band mean reversion | Buying dips: 61-65% hit rate | Fading rallies: 48-52% hit rate | BB mean reversion is a bullish-only signal |
| Funding rate extremes | Positive funding less predictive (59% hit rate) | Negative funding more predictive (62% hit rate) | Negative funding (short squeeze setup) is the stronger contrarian signal |

These asymmetries are baked into the direction-aware weighting in Phase 3 of the scoring pipeline. The backtest optimizer will discover and calibrate these per-asset rather than relying on global heuristics.

---

## Appendix: Implementation Sequence

The recommended implementation order, designed to deliver value incrementally:

1. **Cut dead agents** (Narrative, Exchange Flow) -- immediate complexity reduction
2. **Fix broken market fields** (S&P, DXY, breadth) -- free accuracy gain from data already configured
3. **Fix OI change %** in derivatives -- recover 15% of derivatives score from hardcoded zero
4. **Add OBV and MFI** to technical -- highest-IC new indicators
5. **Implement Phase 1 backtest** (technical + market, 180 days)
6. **Add remaining technical indicators** (ROC, Stochastic RSI, squeeze, z-scores)
7. **Add market sources** (NASDAQ, stablecoin supply, BTC dominance, VIX ROC)
8. **Add OI-weighted funding** and relative features
9. **Implement Phase 2 backtest** (full 3-agent, 90 days)
10. **Implement per-asset weight optimizer** with confidence tiers
11. **Implement deploy gate** with baseline management
12. **Run full optimization** and commit first `backtest_baseline.json`
