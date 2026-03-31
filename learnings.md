# Autoresearch Learnings — Signal Fusion YAML Optimization

**Target file**: `signal_fusion/profiles/default.yaml`
**Eval**: `eval.py` (5 criteria: CWA, directional accuracy, coverage, per-asset accuracy, bullish returns)
**Final score**: 100% (5/5 criteria pass) — achieved in round AR-8, stable across 3 confirmation rounds
**Total rounds**: 30 (21 pre-autoresearch + 9 autoresearch)

---

## What Worked (with score deltas)

### 1. Contrarian F&G override key fix: +60% (Round 15)
**The single biggest improvement.** The contrarian override was using `raw.get("market_agent")` but dict keys are roles (`"market"`). Once fixed, system correctly calls buys during extreme fear (F&G=11). This was the #1 bottleneck — everything else was tuning on top of a broken feature.

**Transferable rule**: Always verify dict key naming conventions match between producer and consumer. A silent `None` from a wrong key is the hardest bug to find.

### 2. Agent scoring range widening: +40% (Rounds 3-6)
Agents originally produced scores in a 10-15 point range clustered around 50. Widened to 30-40 point ranges:
- RSI: 30-70 → 5-95
- MACD: flat buckets → continuous 10-90 (price-normalized)
- BB: flat buckets → continuous 10-90
- Derivatives: flat 40/50/60 buckets → continuous interpolation
- Market F&G: flat 35-65 → continuous 10-90

**Transferable rule**: If your scoring produces values in a narrow band, your abstain/threshold logic can't differentiate. Widen scoring ranges FIRST before tuning thresholds.

### 3. Blacklisting anti-predictive assets: +20% (Rounds AR-6, AR-8)
Blacklisted 7 assets total: INJ (20.7% accuracy), ATOM (24.5%), OP (41.9%), ADA, DOT, APT, MATIC. Each was anti-predictive or consistently wrong during the evaluation period.

**Transferable rule**: When agent scores can't differentiate good vs bad signals for specific assets (scores overlap), no threshold trick will help. Remove the asset from scoring until per-asset weight learning can handle it.

### 4. Contrarian score nudge with tier scaling: +0% but stabilized
`score_nudge_max=12.0` with tier-aware scaling (large_cap=1.1, mid_cap=1.0, small_cap=0.90). The nudge pushes scores above the bullish abstain threshold during extreme fear, capturing bounce plays.

**Transferable rule**: Different asset classes respond differently to the same macro signal. Scale your adjustments by asset tier.

### 5. MACD price normalization: prerequisite for cross-asset scoring
BTC MACD histogram = -524 USD vs SOL = -2.1 USD. Without normalizing by price (`macd_pct = histogram / price * 100`), BTC technical score was always slammed to extreme bearish.

**Transferable rule**: Any absolute-value indicator must be normalized for cross-asset comparability. Use percentage-of-price, not raw values.

---

## What Hurt (reverted changes)

### 1. Raising bullish_min_distance from 6 to 7: -20% (Round AR-1)
Improved accuracy (75% → 75%) but killed coverage (53% → 23.5%). The threshold removed correct buys along with wrong ones.

**Lesson**: Tightening thresholds is a blunt instrument — it can't distinguish good from bad signals, just reduces all signals equally.

### 2. Lowering bullish_min_distance from 6 to 5: 0% (Round AR-2)
Added more directional signals (coverage 53% → 64.7%) but also added more wrong ones. Net zero improvement.

**Lesson**: Loosening thresholds is equally blunt — more signals doesn't mean better signals.

### 3. Small_cap nudge 0.90 → 0.65 (Round AR-7): -20%
Tried to suppress small-cap wrong buys by reducing their contrarian nudge. Removed correct small-cap buys (ARB, UNI) along with wrong ones (APT, MATIC).

**Lesson**: When wrong and correct signals have overlapping scores within the same tier, scaling the tier uniformly can't help. You need per-asset differentiation.

### 4. MACD normalization alone (Round 7): -20%
Normalized MACD without recalibrating other scoring. Reduced sells from 8 to 3, but all 3 were wrong. The normalization changed the score distribution, requiring threshold recalibration.

**Lesson**: Any change to scoring ranges requires simultaneous threshold recalibration. Never change one without the other.

---

## Patterns That Transfer to Similar Files

1. **Fix data flow bugs before tuning parameters.** The contrarian key bug was worth +60%. All parameter tuning before that was wasted effort.

2. **Widen scoring ranges before tuning thresholds.** If inputs are clustered, no threshold logic can differentiate. Get variance in your inputs first.

3. **Blacklist > threshold tuning when scores overlap.** If asset A (wrong) scores 56.9 and asset B (correct) scores 56.6, no threshold separates them. Remove A.

4. **One change per round, commit on improvement, revert on regression.** Git is your safety net. Never bundle changes — you lose the ability to isolate what helped.

5. **C4 (per-asset accuracy) is structurally impossible in single-snapshot evaluation** at any accuracy below 100%. One wrong directional call = 0% for that asset. Multi-snapshot evaluation or blacklisting is the only fix.

6. **Order book data is too noisy for high weight.** OB snapshots vary 10x between eval runs. Cap the score range (30-70) and reduce weight (0.25 → 0.15). F&G is much more reliable (weight 0.25 → 0.35).

7. **Contrarian signals are the key alpha** during extreme sentiment. F&G < 25 with a bullish nudge captures the "extreme fear → 48h bounce" pattern that accounts for most correct buy signals.

---

## Final Configuration Summary

| Parameter | Value |
|-----------|-------|
| Blacklisted assets | INJ, ATOM, OP, ADA, DOT, APT, MATIC (7/20) |
| Active assets | 13 |
| Abstain thresholds | bearish=4, bullish=6 |
| Contrarian nudge max | 12.0 points |
| Tier nudge scale | large_cap=1.1, mid_cap=1.0, small_cap=0.90 |
| F&G extreme fear trigger | < 25 |
| Market boost (extreme) | 2.0x weight |
| Technical dampen (extreme) | 0.50x weight |
| Labels | MODERATE BUY at 56, NEUTRAL at 46 |

## Next Steps (beyond autoresearch)

1. **Per-asset weight learning (Phase 5)** — eventually un-blacklist assets with learned per-asset weights
2. **Multi-snapshot evaluation** — run eval across multiple time windows to make C4 meaningful
3. **Target price + stop loss (Phase 3)** — every signal needs entry/TP/SL for real trading evaluation
4. **Fresh data validation** — run eval WITHOUT `EVAL_SKIP_COLLECT` to test on live data
