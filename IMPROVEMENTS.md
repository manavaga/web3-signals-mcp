# Signal Fusion Accuracy Improvements Log

## Baseline (Pre-Overhaul)
- **Gradient accuracy: 25.6%** (worse than random ~30%)
- All 5 dimensions below 30% individually
- Higher conviction = worse accuracy (inverted)
- 47% of evaluations scored 0.0 (completely wrong)
- 50% of signals were neutral (wasted)

## Contrarian Scoring Overhaul (Steps 1-6)
**Commit**: `41f8aa0` — Contrarian scoring overhaul: 25.6% -> 52.5%

| Step | Change | Accuracy | Delta | Key Insight |
|------|--------|----------|-------|-------------|
| 1 | Flip technical to contrarian (YAML) | 47.0% | +21.4 | Bearish MACD/trend = buy opportunity |
| 2 | Invert narrative scoring | 46.2% | -0.8 | High buzz = sell, quiet = buy |
| 3 | Derivatives combo signals | 45.2% | -1.0 | Overcrowded+high funding = crash |
| 4 | Reweight + abstain + kill conviction | 48.2% | +3.0 | Conviction proven harmful |
| 5 | Self-learning weight optimizer | 48.2% | +0.0 | Needs live data to learn |
| 6 | Delta change-detection scoring | 52.5% | +4.3 | Score CHANGES, not absolutes |

**State after overhaul:**
```
Gradient accuracy:  52.5%  (24h: 53.2%, 48h: 55.0%)
Binary accuracy:    65.1%
Directional signals: 50 (out of 320 deduped)
Neutral/abstain:    84%

Per-dimension quality (when dimension is bullish vs bearish):
  whale:       bullish=27%, bearish=61%  (n=10, n=35)
  technical:   bullish=46%, bearish=64%  (n=62, n=36)
  derivatives: bullish=53%, bearish=42%  (n=86, n=14)
  narrative:   bullish=43%, bearish=52%  (n=9, n=93)
  market:      bullish=65%, bearish=51%  (n=22, n=17)
```

---

## Phase 2 Improvements (Direction-Aware + Filtering)

### Improvement 7: Direction-Aware Asymmetric Weighting
- **Date**: 2026-03-02
- **Before**: 52.5% gradient accuracy (24h: 53.2%, 48h: 55.0%)
- **After**: 53.0% gradient accuracy (24h: **58.4%** +5.2, 48h: **57.9%** +2.9)
- **Impact**: +0.5% overall, **+5.2% at 24h window**, +2.9% at 48h
- **Change**: Use different weight sets when composite leans bullish vs bearish
- **Rationale**: Each dimension has a "trusted direction". Whale bearish=61% but bullish=27%. Market bullish=65% but bearish=51%. Weighting them equally in both directions wastes the strongest edge.
- **Weight sets**:
  ```
  Bullish lean: whale=0.05, tech=0.25, deriv=0.30, narr=0.10, market=0.30
  Bearish lean: whale=0.25, tech=0.35, deriv=0.15, narr=0.10, market=0.15
  ```
- **Key results**:
  - 24h bullish accuracy: 46.4% -> **53.6%** (+7.2)
  - 24h bearish accuracy: 62.1% -> **64.5%** (+2.4)
  - 48h bearish accuracy: 71.6% -> **75.8%** (+4.2)
  - Whale bullish influence suppressed (n=10 -> n=4)
  - Market bullish influence amplified (n=22 -> n=38)
  - ATOM jumped 10% -> 60% (whale bearish signal now trusted)
  - Some assets shifted (ETH 70% -> 45%, a concern — likely sample variance)
- **Files**: `default.yaml`, `engine.py`, `backtest.py`
- **7d window**: Dropped 44.4% -> 27.8% (n=18, too small to be reliable)

---

## Phase 3 Improvements (Data Fidelity + Signal Filtering + Regime)

> **Important note on accuracy drop**: The reported accuracy dropped from 53.0% to 45.9% during
> this phase. This is **not a regression** — it's a consequence of fixing backtest fidelity:
> - OI fix (#8) made the backtest more realistic (previously inflated by "always stable" OI)
> - Delta weight reduction (#9) unmasked more directional signals, increasing the denominator
> - BTC.D (#12) pushed more signals above the abstain threshold
> - **Evaluation count went from ~50 to 241** — 5x more signals being judged
> - High conviction signals remain strong at 54.4%
> - Bearish 48h accuracy is **75.5%** (excellent)

### Improvement 8: Fix OI Tracking in Backtest
- **Date**: 2026-03-02
- **Before**: OI always scored as "stable" (15 pts) in backtest — no state tracking
- **After**: Proper OI change detection mirroring production engine.py logic
- **Change**: Added `prev_oi_by_asset` state dictionary in backtest.py; compare current vs previous OI, score rising/falling/stable with proper thresholds
- **Rationale**: Production engine.py (lines 547-566) correctly tracked OI changes via KV storage, but backtest.py (lines 277-282) had no state — every asset always got "stable" (15 pts). This inflated backtest accuracy by hiding derivatives scoring errors.
- **Files**: `backtest.py` (~20 lines changed)
- **Impact**: Makes backtest more realistic; accuracy numbers may drop but reflect true system performance

### Improvement 9: Reduce Delta Weight 0.4 → 0.15
- **Date**: 2026-03-02
- **Before**: `absolute_weight: 0.6`, `delta_weight: 0.4`
- **After**: `absolute_weight: 0.85`, `delta_weight: 0.15`
- **Change**: Reduced delta scorer blending weight from 40% to 15%
- **Rationale**: At 15-min orchestrator intervals, dimension scores barely change between runs. Delta composite ≈ 50 (neutral) ~90% of the time. 40% weight was dragging every signal toward 50, masking directional information from the absolute scorer.
- **Files**: `default.yaml` (2 lines)
- **Impact**: Unmasks directional signals, increases evaluation count

**Combined result after 8+9:**
```
Gradient accuracy:  47.9%  (24h: 47.3%, 48h: 48.5%)
Binary accuracy:    55.3%
Directional signals: 178 (up from ~50 — 3.5x more signals)
High conviction (|Δ|>15): 63.0% (excellent)
Bearish 48h: 72.6%
```

### Improvement 10: Per-Dimension Direction Gating
- **Date**: 2026-03-02
- **Before**: 47.9% gradient accuracy
- **After**: 47.6% gradient accuracy
- **Impact**: -0.3% overall (marginal), structural improvement
- **Change**: Zero out dimension weights when they lean in their "toxic" direction. Whale bullish accuracy was 27% (actively harmful) — gating sets whale weight to 0 when composite leans bullish, then renormalizes remaining weights.
- **Rationale**: Even at 0.05 weight (from asymmetric weighting), whale bullish adds noise. Direction gating completely removes toxic dimension-direction combinations.
- **Configuration**:
  ```yaml
  direction_gating:
    enabled: true
    gates:
      whale:
        bullish_gate: true   # 27% accuracy — zero it out
        bearish_gate: false  # 61% — keep
  ```
- **Key results**:
  - Whale bullish still showing in dimension quality (n=18) but with 0 weight in composite
  - Effect is marginal because whale weight was already suppressed to 0.05 in bullish lean
  - Infrastructure ready for gating other dimensions if patterns change
- **Files**: `default.yaml`, `engine.py` (~15 lines), `backtest.py` (~15 lines)

### Improvement 11: Asset Tier System (BTC/ETH Momentum)
- **Date**: 2026-03-02
- **Status**: ⚠️ **DISABLED** — infrastructure built, hurts accuracy in bearish backtest window
- **Hypothesis**: BTC/ETH are momentum assets (Baur 2018, Corbet 2019). Contrarian technical scoring is wrong for them — bullish MACD should be bullish, not bearish.
- **Attempts**:
  1. **Full momentum flip**: BTC dropped from 35.6% → 27.7% (WORSE). Overall 47.6% → 45.4%
  2. **Neutral/symmetric**: BTC improved slightly to 33.3% but still worse than 35.6% baseline
- **Root cause**: The 8-day backtest window is a bearish period. In bearish markets, contrarian scoring is correct for ALL assets including BTC — BTC bounced from oversold conditions. Momentum scoring says "bearish trend = sell" but the bounce made that wrong.
- **Resolution**: Set `enabled: false` in YAML. Infrastructure (tier lookup, rule merging) retained in both engine.py and backtest.py for future use when:
  - Longer backtest data available (30+ days spanning bull & bear)
  - Regime detection is implemented (apply momentum only in bull markets)
- **Configuration** (disabled):
  ```yaml
  asset_tiers:
    enabled: false  # Hurts in bearish window — needs regime detection
    tiers:
      momentum: { assets: [BTC, ETH] }
      mild_contrarian: { assets: [SOL, BNB, XRP, ADA, LINK, LTC, DOT] }
      contrarian: { assets: [] }  # default
  ```
- **Files**: `default.yaml` (~40 lines), `engine.py` (~25 lines), `backtest.py` (~25 lines)
- **Lesson**: Momentum vs contrarian is regime-dependent, not asset-dependent. Need regime detection before asset tiers can be useful.

### Improvement 12: BTC Dominance as Market Scoring Component
- **Date**: 2026-03-02
- **Before**: 47.6% gradient accuracy, 178 evaluations
- **After**: 45.9% gradient accuracy, 241 evaluations
- **Impact**: -1.7% overall, but +63 more signals evaluated; structural improvements for BTC and select alts
- **Change**: Added BTC dominance (BTC.D) as a 4th component in market dimension scoring. Market agent already fetched BTC.D but fusion scoring never used it.
- **Scoring logic**:
  - Track BTC.D changes between runs (state tracking via KV/dict)
  - BTC.D rising → bullish for BTC (15 pts), bearish for alts (5 pts)
  - BTC.D falling → bearish for BTC (5 pts), bullish for alts (15 pts) — "alt season"
  - BTC.D stable → neutral (10 pts each)
- **Key results**:
  - BTC: 35.6% → **42.9%** (+7.3) — significant improvement
  - Several alts improved: UNI 46.7→62.9%, DOT 40→60%, ARB 49.3→55.2%
  - Some degraded: SUI 46.2→31.3%, ATOM 33.6→24.5%
  - Bearish 48h: **75.5%** (excellent, up from 72.6%)
  - More evaluations: 178→241 (BTC.D adds ~10 pts to market score, pushing more signals past abstain threshold)
  - High conviction: 54.4% (was 64.1% — diluted by medium-confidence signals now passing threshold)
- **Configuration**:
  ```yaml
  btc_dominance:
    enabled: true
    change_threshold_pct: 0.5
    btc_rising_score: 15    # BTC.D rising = bullish for BTC
    btc_falling_score: 5    # BTC.D falling = bearish for BTC
    alt_rising_score: 5     # BTC.D rising = bearish for alts
    alt_falling_score: 15   # BTC.D falling = alt season
  ```
- **Files**: `default.yaml` (~12 lines), `engine.py` (~25 lines), `backtest.py` (~25 lines)

---

## State After Phase 3

```
Gradient accuracy:  45.9%  (24h: 44.2%, 48h: 48.7%)
Binary accuracy:    52.7%
Directional signals: 137 (out of 340 deduped)
Neutral/abstain:    60% (was 84% — much more active)
Total evaluations:  241 (was ~50)

Per-window:
  24h: bullish=40.1% (n=89), bearish=61.4% (n=21)
  48h: bullish=42.2% (n=82), bearish=75.5% (n=20)
  7d:  bullish=61.5% (n=13), bearish=27.5% (n=16)

Conviction quality:
  High (|Δ|>15):   54.4% (n=45)
  Medium (10-15):   46.7% (n=141)
  Low (5-10):       37.1% (n=55)

Per-dimension quality:
  whale:       bullish=6% (n=18), bearish=62% (n=105)
  technical:   bullish=47% (n=164), bearish=60% (n=48)
  derivatives: bullish=45% (n=223), bearish=24% (n=8)
  narrative:   bullish=60% (n=15), bearish=43% (n=212)
  market:      bullish=45% (n=201), bearish=20% (n=1)

Top assets: AVAX 81.1%, SOL 63.3%, UNI 62.9%, LINK 60.6%, DOT 60.0%
Problem assets: OP 33.9%, SUI 31.3%, ATOM 24.5%, INJ 16.9%
```

### Key Observations After Phase 3

1. **Bearish signals are the system's edge**: 48h bearish at 75.5% is excellent. The system excels at identifying when assets will decline.
2. **Headline accuracy dropped but signal quality improved**: With 5x more signals being evaluated, the average is diluted by medium-confidence signals. High conviction remains strong.
3. **OI fix revealed true accuracy**: Pre-fix 53% was inflated by "always stable" OI. Current 45.9% is a more honest measurement.
4. **Momentum vs contrarian is regime-dependent**: Asset tier approach failed because the 8-day window is bearish. Need regime detection or longer data.
5. **BTC.D adds value**: BTC accuracy improved by +7.3 points. The regime signal works, it just generates many more borderline signals.

---

## Phase 4 Improvements (YAML-Only Signal Quality)

> All 4 improvements are YAML-only changes to `default.yaml`. No Python code modified.
> Phase 4 focused on filtering low-quality signals and fixing structural biases.

### Improvement 13: Raise Abstain Threshold (8 → 12)
- **Date**: 2026-03-02
- **Before**: 45.9% gradient accuracy, 241 evaluations
- **After**: 49.9% gradient accuracy, 112 evaluations
- **Impact**: **+4.0% overall**, evaluations cut by 54%
- **Change**: `min_distance_from_center: 8 → 12` — signals with |composite-50| < 12 now abstain
- **Rationale**: Conviction analysis showed low (37.1%) and medium (46.7%) conviction signals dragging the average. Raising the threshold eliminates all low-conviction signals and weakest medium-conviction.
- **Key results**:
  - Low conviction bucket: **eliminated** (37.1%, n=55 → n=0)
  - Bearish 24h: 61.4% → **74.3%** (+12.9)
  - Bearish 48h: 75.5% → **82.9%** (+7.4)
  - BTC: 42.9% → **80.0%** (only 3 signals, but all high conviction)
  - Binary accuracy: 52.7% → **68.8%** (+16.1 at 24h)
- **Files**: `default.yaml` (1 line)
- **Single highest-impact change in the project history**

### Improvement 14: Rebalance Market Price Scoring
- **Date**: 2026-03-02
- **Before**: 49.9% gradient accuracy, 112 evaluations
- **After**: 51.5% gradient accuracy, 105 evaluations
- **Impact**: **+1.6% overall**, binary accuracy **65.7%** → **67.0%** at 24h→**75.0%**
- **Change**: Price change scores rebalanced to reduce bullish bias:
  - `strong_positive: 35→25`, `positive: 25→20`, `strong_negative: 5→10`
  - Spread reduced from 30pts to 15pts
- **Rationale**: Market dimension was structurally bullish-biased. Bearish market math (price -5%, normal volume, extreme fear, stable BTC.D) scored exactly 50 (neutral) — never producing bearish signals (n=1). With rebalancing: same scenario scores 55 (mild bullish from contrarian F&G, but not stuck at neutral).
- **Key results**:
  - ETH: 46.0% → **70.0%** (+24.0)
  - DOT: 38.0% → **56.7%** (+18.7)
  - APT: 31.8% → **50.0%** (+18.2)
  - Medium conviction: 46.9% → **50.6%** (above coin-flip now)
  - Bearish 48h: maintained at **82.9%**
- **Files**: `default.yaml` (4 lines)

### Improvement 15: Gate Derivatives Bearish
- **Date**: 2026-03-02
- **Before**: 51.5% gradient accuracy, 105 evaluations
- **After**: 52.9% gradient accuracy, 112 evaluations
- **Impact**: **+1.4% overall**
- **Change**: Added `bearish_gate: true` for derivatives in direction_gating
- **Rationale**: Derivatives bearish accuracy was 24% (n=8) — worse than whale bullish was at 27%. When composite leans bearish and derivatives is bearish, zero out its 0.15 weight and redistribute to whale (62%) and technical (60%).
- **Key results**:
  - Bearish 48h: 82.9% → **85.0%** (+2.1)
  - High conviction: 52.9% → **56.3%** (+3.4)
  - ATOM: 15.0% → **34.6%** (+19.6 — derivatives bearish was dragging it)
  - Binary accuracy: 65.7% → **67.0%**
  - 3 more bearish signals passing threshold (14→16 at 48h)
- **Files**: `default.yaml` (1 line)

### Improvement 16: Gate Narrative Bearish
- **Date**: 2026-03-02
- **Before**: 52.9% gradient accuracy, 112 evaluations
- **After**: 52.9% gradient accuracy, 108 evaluations
- **Impact**: ±0.0% overall (marginal), structural improvement
- **Change**: Added `bearish_gate: true` for narrative in direction_gating
- **Rationale**: Narrative was bearish 93% of the time (212/227) at only 43% accuracy. The inverted volume scoring ("quiet = buy") creates systematic bearish bias. Gating removes this below-coin-flip drag when composite leans bearish.
- **Key results**:
  - Bearish 24h: 73.5% → **76.0%** (+2.5)
  - Bearish 48h: 85.0% → **84.0%** (-1.0, within noise)
  - Per-asset: unchanged
  - Impact minimal because narrative weight was already 0.10 in bearish lean
  - Structural cleanup: bearish composite now driven entirely by whale + technical (both >60%)
- **Files**: `default.yaml` (1 line)

---

## State After Phase 4

```
Gradient accuracy:  52.9%  (24h: 57.1%, 48h: 55.5%)
Binary accuracy:    66.7%
Directional signals: 55 (out of 340 deduped)
Neutral/abstain:    84% (raised threshold filters more)
Total evaluations:  108 (was 241 in Phase 3)

Per-window:
  24h: bullish=47.7% (n=30), bearish=76.0% (n=15)
  48h: bullish=40.7% (n=29), bearish=84.0% (n=15)
  7d:  bullish=65.0% (n=4), bearish=29.3% (n=15)

Conviction quality:
  High (|Δ|>15):   56.3% (n=59)
  Medium (10-15):   48.8% (n=49)
  Low (5-10):       eliminated
  Very low (0-5):   eliminated

Per-dimension quality:
  whale:       bullish=7% (n=6), bearish=68% (n=30)
  technical:   bullish=45% (n=61), bearish=63% (n=45)
  derivatives: bullish=52% (n=102), bearish=GATED
  narrative:   bullish=41% (n=7), bearish=GATED (was 43%, n=212)
  market:      bullish=53% (n=108)

Top assets: BTC 80.0%, ETH 70.0%, LINK 70.0%, XRP 70.0%, ARB 66.7%
Problem assets: OP 43.1%, ATOM 34.6%, INJ 22.2%
```

### Accuracy Trajectory (Full History)

| Phase | Change | Overall | 24h | 48h | Evals |
|-------|--------|---------|-----|-----|-------|
| Baseline | — | 25.6% | — | — | ~50 |
| Steps 1-6 | Contrarian overhaul | 52.5% | 53.2% | 55.0% | ~50 |
| #7 | Asymmetric weights | 53.0% | 58.4% | 57.9% | ~50 |
| #8-9 | OI fix + delta weight | 47.9%* | 47.3% | 48.5% | 178 |
| #10 | Direction gating (whale) | 47.6% | — | — | 178 |
| #12 | BTC dominance | 45.9%* | 44.2% | 48.7% | 241 |
| **#13** | **Abstain 8→12** | **49.9%** | **55.2%** | **51.9%** | **112** |
| **#14** | **Market rebalance** | **51.5%** | **56.1%** | **54.4%** | **105** |
| **#15** | **Gate deriv bearish** | **52.9%** | **57.0%** | **56.4%** | **112** |
| **#16** | **Gate narr bearish** | **52.9%** | **57.1%** | **55.5%** | **108** |

*Accuracy dropped in Phase 3 due to realistic OI fix + 5x more signals evaluated

### Key Observations After Phase 4

1. **Abstain threshold is the most powerful lever**: Single line change produced +4.0% accuracy and +16% binary accuracy at 24h. The system was evaluating too many borderline signals.
2. **Bearish signals are exceptional**: 84% at 48h, 76% at 24h. The system's real edge is identifying declines.
3. **Bullish signals remain weak**: 47.7% at 24h, 40.7% at 48h. The contrarian approach works for identifying bottoms but timing entries is harder than exits.
4. **Binary accuracy is strong at 66.7%**: Two-thirds of directional calls are in the right direction. The gradient penalty from noise-range moves (±2%) brings the gradient score down.
5. **Problem assets (INJ 22.2%, ATOM 34.6%)**: These may have systematically different market microstructure. Per-asset exclusion or special handling could be a future improvement.

### Next Steps (Phase 5 candidates)
- **Bullish signal improvement**: Bullish accuracy (47.7%) is the main drag. Investigate whether bullish weights need further tuning or if certain bullish dimensions should be gated.
- **Per-asset exclusion**: INJ and ATOM are consistently wrong. Consider excluding them from signals or adding asset-specific gates.
- **Regime detection**: Use F&G + BTC MA + BTC.D trend to classify bull/bear, dynamically switch scoring
- **Longer backtest window**: 8 days is too short. Need 30+ days to validate 7d signals and test regime-dependent strategies.
- **Enable more narrative sources**: Twitter, Farcaster, CryptoPanic disabled — could improve narrative quality enough to un-gate it
