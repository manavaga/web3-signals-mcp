# Backtest v2 + Dashboard Target Display — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add 5 new analysis parts to backtest.py (signal timeline, target evaluation, prediction quality, data quality audit, regime analysis) with train/test split, plus display entry/target/SL in the dashboard modal.

**Architecture:** Extend `backtest.py` with a temporal train/test split (60/40), add Parts 8-12 after existing Part 7, and add a `renderTradingLevels()` JS function to `api/dashboard.py`. The backtest replays 48h price paths tick-by-tick for target evaluation using the existing `price_timeline` data.

**Tech Stack:** Python 3.13, existing YAML profile system, existing API data pipeline, vanilla JS dashboard

---

### Task 1: Add Train/Test Temporal Split to backtest.py

**Files:**
- Modify: `/Users/admin/Documents/web3 Signals x402/backtest.py:1386-1416`

**Step 1: Add split logic after aligned snapshots are built**

After line 1398 (`assets_list = all_assets`), and before the re-scoring loop (line 1400), insert the train/test split. The split point is computed but ALL data gets re-scored — the split only determines which evals are reported.

Add after the `assets_list` assignment (around line 1399):

```python
    # ================================================================
    # TRAIN/TEST TEMPORAL SPLIT
    # ================================================================
    split_ratio = 0.60
    split_idx = int(len(aligned) * split_ratio)
    split_timestamp = aligned[split_idx][0] if split_idx < len(aligned) else aligned[-1][0]
    print(f"\n  Train/Test split: {split_ratio:.0%}/{1-split_ratio:.0%}")
    print(f"    Train: {aligned[0][0].strftime('%Y-%m-%d %H:%M')} → {split_timestamp.strftime('%Y-%m-%d %H:%M')} ({split_idx} points)")
    print(f"    Test:  {split_timestamp.strftime('%Y-%m-%d %H:%M')} → {aligned[-1][0].strftime('%Y-%m-%d %H:%M')} ({len(aligned) - split_idx} points)")
```

**Step 2: Tag each signal with train/test**

In the re-scoring loop (around line 1411-1415), add the split tag:

```python
            all_signals.append({
                "timestamp": ts,
                "asset": asset,
                "split": "train" if idx < split_idx else "test",
                **result,
            })
```

Update the loop to use `enumerate`:
```python
    for idx, (ts, snapshot) in enumerate(aligned):
```

**Step 3: Filter evals to test-only for accuracy Parts 2-7**

Before the accuracy section (around line 1467, PART 2), add:

```python
    # Filter unique_signals to test set for accuracy evaluation
    test_signals = [s for s in all_signals if s["split"] == "test"]
    test_unique = [s for s in unique_signals if s["split"] == "test"]
    test_directional = [s for s in test_unique if s["direction"] != "neutral"]
    print(f"\n  Test-only: {len(test_signals)} signals, {len(test_unique)} unique, {len(test_directional)} directional")
```

Then replace `directional` with `test_directional` and `unique_signals` with `test_unique` in the PART 2 loop (line 1498 `for sig in directional:` → `for sig in test_directional:`).

**Step 4: Commit**

```bash
git add backtest.py
git commit -m "backtest: add 60/40 temporal train/test split"
```

---

### Task 2: PART 8 — Signal Flow Timeline

**Files:**
- Modify: `/Users/admin/Documents/web3 Signals x402/backtest.py` (add after PART 7, before SUMMARY)

**Step 1: Add Part 8 before the SUMMARY section**

Insert before `# SUMMARY` (around line 1834). This section uses ALL data (train+test) to show signal distribution over time:

```python
    # ================================================================
    # PART 8: SIGNAL FLOW TIMELINE (per 12h bucket)
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 8: SIGNAL FLOW TIMELINE (per 12h bucket)")
    print(f"{'='*80}")
    print("Shows buy/sell/neutral signal counts per 12-hour window.\n")

    # Group all signals by 12h bucket
    timeline_buckets: Dict[str, Dict[str, Any]] = {}
    for sig in all_signals:
        t = sig["timestamp"]
        bucket = t.strftime("%Y-%m-%d") + (" AM" if t.hour < 12 else " PM")
        if bucket not in timeline_buckets:
            timeline_buckets[bucket] = {
                "buy": 0, "sell": 0, "neutral": 0,
                "buy_assets": [], "sell_assets": [],
                "scores": [], "split": sig["split"],
            }
        b = timeline_buckets[bucket]
        d = sig["direction"]
        if d == "bullish":
            b["buy"] += 1
            if sig["asset"] not in b["buy_assets"]:
                b["buy_assets"].append(sig["asset"])
        elif d == "bearish":
            b["sell"] += 1
            if sig["asset"] not in b["sell_assets"]:
                b["sell_assets"].append(sig["asset"])
        else:
            b["neutral"] += 1
        b["scores"].append(sig["composite_score"])

    print(f"  {'Bucket':>16s}  {'Split':>5s}  {'Buy':>4s}  {'Sell':>4s}  {'Neut':>4s}  {'AvgSc':>5s}  Buy Assets")
    print(f"  {'─'*16}  {'─'*5}  {'─'*4}  {'─'*4}  {'─'*4}  {'─'*5}  {'─'*30}")
    for bucket in sorted(timeline_buckets.keys()):
        b = timeline_buckets[bucket]
        avg_sc = sum(b["scores"]) / len(b["scores"]) if b["scores"] else 0
        split_tag = "TRAIN" if b["split"] == "train" else "TEST"
        buy_list = ", ".join(b["buy_assets"][:6])
        if len(b["buy_assets"]) > 6:
            buy_list += f" +{len(b['buy_assets'])-6}"
        sell_note = f" | Sell: {', '.join(b['sell_assets'])}" if b["sell_assets"] else ""
        print(f"  {bucket:>16s}  {split_tag:>5s}  {b['buy']:4d}  {b['sell']:4d}  {b['neutral']:4d}  {avg_sc:5.1f}  {buy_list}{sell_note}")

    # Summary stats
    total_buy = sum(b["buy"] for b in timeline_buckets.values())
    total_sell = sum(b["sell"] for b in timeline_buckets.values())
    total_neutral = sum(b["neutral"] for b in timeline_buckets.values())
    total_all = total_buy + total_sell + total_neutral
    print(f"\n  Totals: {total_buy} buy ({total_buy/total_all*100:.0f}%), "
          f"{total_sell} sell ({total_sell/total_all*100:.0f}%), "
          f"{total_neutral} neutral ({total_neutral/total_all*100:.0f}%)")
```

**Step 2: Commit**

```bash
git add backtest.py
git commit -m "backtest: add Part 8 signal flow timeline"
```

---

### Task 3: PART 9 — Target Price Evaluation

**Files:**
- Modify: `/Users/admin/Documents/web3 Signals x402/backtest.py`

**Step 1: Add target calculation to the re-scoring loop**

In `compute_composite()` (around line 860), the function currently returns composite_score, label, direction, dimensions, data_tiers, conviction_boost, abstain. We need to also calculate target/SL for directional signals.

Add target calculation inside the re-scoring loop (around line 1408-1415), after `compute_composite()`:

```python
            # Calculate target/SL for directional signals
            target_data = {}
            if result["direction"] in ("bullish", "bearish") and not result.get("abstain"):
                # Get entry price from market agent
                market_snap = snapshot.get("market")
                tech_snap = snapshot.get("technical")
                entry_price = None
                atr_14 = None

                if market_snap:
                    mkt_data = market_snap.get("data", {})
                    pa = mkt_data.get("per_asset", {}).get(asset, {})
                    entry_price = pa.get("price")
                if entry_price is None and tech_snap:
                    tech_data = tech_snap.get("data", {})
                    entry_price = tech_data.get("by_asset", {}).get(asset, {}).get("price")
                if tech_snap:
                    tech_data = tech_snap.get("data", {})
                    atr_14 = tech_data.get("by_asset", {}).get(asset, {}).get("atr_14")

                if entry_price and float(entry_price) > 0:
                    entry_price = float(entry_price)
                    if atr_14 is None or float(atr_14) <= 0:
                        atr_14 = entry_price * 0.03  # fallback
                    else:
                        atr_14 = float(atr_14)

                    from signal_fusion.target_calculator import ATR_SL_MULTIPLIERS
                    sl_mult = ATR_SL_MULTIPLIERS.get(asset, 2.0)
                    direction_map = "buy" if result["direction"] == "bullish" else "sell"

                    if direction_map == "buy":
                        stop_loss = entry_price - (atr_14 * sl_mult)
                        # Heuristic target: score distance maps to ATR fraction
                        distance = result["composite_score"] - 50.0
                        atr_pct = (atr_14 / entry_price) * 100
                        move_fraction = distance / 35.0
                        predicted_pct = move_fraction * atr_pct * 0.5
                        predicted_pct = max(0.1, min(atr_pct * 2.0, predicted_pct))
                        target_price = entry_price * (1 + predicted_pct / 100)
                        # Enforce min R:R
                        risk = entry_price - stop_loss
                        if target_price - entry_price < risk * 1.5:
                            target_price = entry_price + risk * 1.5
                    else:
                        stop_loss = entry_price + (atr_14 * sl_mult)
                        distance = 50.0 - result["composite_score"]
                        atr_pct = (atr_14 / entry_price) * 100
                        move_fraction = distance / 35.0
                        predicted_pct = move_fraction * atr_pct * 0.5
                        predicted_pct = max(0.1, min(atr_pct * 2.0, predicted_pct))
                        target_price = entry_price * (1 - predicted_pct / 100)
                        risk = stop_loss - entry_price
                        if entry_price - target_price < risk * 1.5:
                            target_price = entry_price - risk * 1.5

                    target_data = {
                        "entry_price": round(entry_price, 2),
                        "target_price": round(target_price, 2),
                        "stop_loss": round(stop_loss, 2),
                        "predicted_move_pct": round(predicted_pct, 2),
                    }
```

Then include `target_data` in the signal dict:
```python
            all_signals.append({
                "timestamp": ts,
                "asset": asset,
                "split": "train" if idx < split_idx else "test",
                **result,
                **target_data,
            })
```

**Step 2: Add Part 9 — target evaluation using price path replay**

Insert after Part 8:

```python
    # ================================================================
    # PART 9: TARGET PRICE EVALUATION (test set only)
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 9: TARGET PRICE EVALUATION")
    print(f"{'='*80}")
    print("For each directional signal, replay 48h price path: TP hit / SL hit / expired.\n")

    target_signals = [s for s in all_signals
                      if s["split"] == "test"
                      and s.get("entry_price") is not None
                      and s["direction"] != "neutral"]

    # Deduplicate to 1 per asset per 12h
    seen_target = set()
    unique_target = []
    for sig in target_signals:
        bk = (sig["asset"], sig["timestamp"].strftime("%Y-%m-%d") +
              ("_AM" if sig["timestamp"].hour < 12 else "_PM"))
        if bk not in seen_target:
            seen_target.add(bk)
            unique_target.append(sig)

    outcomes = []  # list of {asset, outcome, pnl_pct, time_to_outcome_hours, ...}

    for sig in unique_target:
        asset = sig["asset"]
        tl = price_timeline.get(asset, [])
        if not tl:
            continue

        entry = sig["entry_price"]
        tp = sig["target_price"]
        sl = sig["stop_loss"]
        direction = sig["direction"]
        sig_time = sig["timestamp"]
        end_time = sig_time + timedelta(hours=48)

        # Walk price path
        outcome = "EXPIRED"
        final_price = entry
        time_to_outcome = 48.0

        for pt_time, pt_price in tl:
            if pt_time <= sig_time:
                continue
            if pt_time > end_time:
                break

            final_price = pt_price

            if direction == "bullish":
                if pt_price >= tp:
                    outcome = "TP_HIT"
                    time_to_outcome = (pt_time - sig_time).total_seconds() / 3600
                    final_price = tp
                    break
                elif pt_price <= sl:
                    outcome = "SL_HIT"
                    time_to_outcome = (pt_time - sig_time).total_seconds() / 3600
                    final_price = sl
                    break
            else:  # bearish
                if pt_price <= tp:
                    outcome = "TP_HIT"
                    time_to_outcome = (pt_time - sig_time).total_seconds() / 3600
                    final_price = tp
                    break
                elif pt_price >= sl:
                    outcome = "SL_HIT"
                    time_to_outcome = (pt_time - sig_time).total_seconds() / 3600
                    final_price = sl
                    break

        if direction == "bullish":
            pnl_pct = (final_price - entry) / entry * 100
        else:
            pnl_pct = (entry - final_price) / entry * 100

        outcomes.append({
            "asset": asset,
            "direction": direction,
            "outcome": outcome,
            "pnl_pct": round(pnl_pct, 2),
            "time_to_outcome_hours": round(time_to_outcome, 1),
            "entry": entry,
            "target": tp,
            "stop_loss": sl,
            "predicted_move_pct": sig.get("predicted_move_pct", 0),
            "composite_score": sig["composite_score"],
            "timestamp": sig_time,
        })

    if outcomes:
        tp_hits = [o for o in outcomes if o["outcome"] == "TP_HIT"]
        sl_hits = [o for o in outcomes if o["outcome"] == "SL_HIT"]
        expired = [o for o in outcomes if o["outcome"] == "EXPIRED"]

        win_rate = len(tp_hits) / len(outcomes) * 100
        loss_rate = len(sl_hits) / len(outcomes) * 100
        expire_rate = len(expired) / len(outcomes) * 100

        avg_win = sum(o["pnl_pct"] for o in tp_hits) / len(tp_hits) if tp_hits else 0
        avg_loss = sum(o["pnl_pct"] for o in sl_hits) / len(sl_hits) if sl_hits else 0
        avg_expired_pnl = sum(o["pnl_pct"] for o in expired) / len(expired) if expired else 0

        ev_per_trade = (len(tp_hits) * avg_win + len(sl_hits) * avg_loss +
                        len(expired) * avg_expired_pnl) / len(outcomes)

        avg_time_tp = sum(o["time_to_outcome_hours"] for o in tp_hits) / len(tp_hits) if tp_hits else 0
        avg_time_sl = sum(o["time_to_outcome_hours"] for o in sl_hits) / len(sl_hits) if sl_hits else 0

        print(f"  Total target evaluations: {len(outcomes)}")
        print(f"  TP Hit:   {len(tp_hits):3d} ({win_rate:5.1f}%)  avg P&L: {avg_win:+.2f}%  avg time: {avg_time_tp:.1f}h")
        print(f"  SL Hit:   {len(sl_hits):3d} ({loss_rate:5.1f}%)  avg P&L: {avg_loss:+.2f}%  avg time: {avg_time_sl:.1f}h")
        print(f"  Expired:  {len(expired):3d} ({expire_rate:5.1f}%)  avg P&L: {avg_expired_pnl:+.2f}%")
        print(f"\n  Expected Value per trade: {ev_per_trade:+.3f}%")
        print(f"  Effective win rate (TP + profitable expires): "
              f"{(len(tp_hits) + sum(1 for o in expired if o['pnl_pct'] > 0)) / len(outcomes) * 100:.1f}%")

        # Per-asset breakdown
        print(f"\n  Per-asset target accuracy:")
        print(f"  {'Asset':>6s}  {'Trades':>6s}  {'WinR':>5s}  {'AvgW':>6s}  {'AvgL':>6s}  {'EV':>7s}")
        print(f"  {'─'*6}  {'─'*6}  {'─'*5}  {'─'*6}  {'─'*6}  {'─'*7}")
        asset_outcomes = defaultdict(list)
        for o in outcomes:
            asset_outcomes[o["asset"]].append(o)
        for asset in sorted(asset_outcomes.keys()):
            ao = asset_outcomes[asset]
            a_tp = [o for o in ao if o["outcome"] == "TP_HIT"]
            a_sl = [o for o in ao if o["outcome"] == "SL_HIT"]
            a_wr = len(a_tp) / len(ao) * 100 if ao else 0
            a_avgw = sum(o["pnl_pct"] for o in a_tp) / len(a_tp) if a_tp else 0
            a_avgl = sum(o["pnl_pct"] for o in a_sl) / len(a_sl) if a_sl else 0
            a_ev = sum(o["pnl_pct"] for o in ao) / len(ao) if ao else 0
            marker = "🟢" if a_ev > 0 else "🔴"
            print(f"  {marker}{asset:>5s}  {len(ao):6d}  {a_wr:4.0f}%  {a_avgw:+5.2f}  {a_avgl:+5.2f}  {a_ev:+6.3f}%")

        # SL tightness analysis
        print(f"\n  Stop-loss tightness (SL hit rate by asset — high = SL too tight):")
        for asset in sorted(asset_outcomes.keys()):
            ao = asset_outcomes[asset]
            sl_rate = sum(1 for o in ao if o["outcome"] == "SL_HIT") / len(ao) * 100
            marker = "⚠️ " if sl_rate > 50 else "  "
            print(f"  {marker}{asset:>5s}: SL hit {sl_rate:.0f}% of {len(ao)} trades")
    else:
        print("  No target evaluations possible (no directional signals with price data)")
```

**Step 3: Commit**

```bash
git add backtest.py
git commit -m "backtest: add Part 9 target price evaluation with price path replay"
```

---

### Task 4: PART 10 — Prediction Quality

**Files:**
- Modify: `/Users/admin/Documents/web3 Signals x402/backtest.py`

**Step 1: Add Part 10 after Part 9**

```python
    # ================================================================
    # PART 10: PREDICTION QUALITY (test set only)
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 10: PREDICTION QUALITY")
    print(f"{'='*80}")
    print("How accurate are predicted move percentages vs actual outcomes?\n")

    pred_evals = []
    for sig in test_directional:
        asset = sig["asset"]
        tl = price_timeline.get(asset, [])
        if not tl:
            continue
        target_time = sig["timestamp"] + timedelta(hours=48)
        if target_time.timestamp() > now_ts:
            continue
        future_price = find_price_at_offset(tl, target_time, max_tolerance_hours=6.0)
        signal_price = find_price_at_offset(tl, sig["timestamp"], max_tolerance_hours=2.0)
        if future_price is None or signal_price is None or signal_price <= 0:
            continue
        actual_pct = (future_price - signal_price) / signal_price * 100
        # For bearish signals, predicted_move_pct is positive but represents downward move
        predicted = sig.get("predicted_move_pct", 0)
        if sig["direction"] == "bearish":
            actual_for_comparison = -actual_pct  # positive = correct direction
        else:
            actual_for_comparison = actual_pct

        pred_evals.append({
            "asset": asset,
            "direction": sig["direction"],
            "predicted_pct": predicted,
            "actual_pct": round(actual_pct, 2),
            "actual_directional": round(actual_for_comparison, 2),
            "error": round(abs(predicted - actual_for_comparison), 2),
            "direction_correct": actual_for_comparison > 0,
        })

    if pred_evals:
        mae = sum(e["error"] for e in pred_evals) / len(pred_evals)
        dir_correct = sum(1 for e in pred_evals if e["direction_correct"])
        dir_acc = dir_correct / len(pred_evals) * 100

        print(f"  Prediction evaluations: {len(pred_evals)}")
        print(f"  Mean Absolute Error:    {mae:.2f}%")
        print(f"  Directional accuracy:   {dir_acc:.1f}% ({dir_correct}/{len(pred_evals)})")

        # Calibration buckets
        buckets = {"0-2%": [], "2-5%": [], "5%+": []}
        for e in pred_evals:
            p = abs(e["predicted_pct"])
            if p < 2:
                buckets["0-2%"].append(e)
            elif p < 5:
                buckets["2-5%"].append(e)
            else:
                buckets["5%+"].append(e)

        print(f"\n  Calibration (predicted bucket vs actual):")
        print(f"  {'Predicted':>10s}  {'n':>4s}  {'Avg Pred':>8s}  {'Avg Actual':>10s}  {'MAE':>6s}  {'DirAcc':>6s}")
        print(f"  {'─'*10}  {'─'*4}  {'─'*8}  {'─'*10}  {'─'*6}  {'─'*6}")
        for label, entries in buckets.items():
            if entries:
                ap = sum(abs(e["predicted_pct"]) for e in entries) / len(entries)
                aa = sum(abs(e["actual_directional"]) for e in entries) / len(entries)
                am = sum(e["error"] for e in entries) / len(entries)
                da = sum(1 for e in entries if e["direction_correct"]) / len(entries) * 100
                print(f"  {label:>10s}  {len(entries):4d}  {ap:7.2f}%  {aa:9.2f}%  {am:5.2f}%  {da:5.1f}%")
            else:
                print(f"  {label:>10s}  {0:4d}  {'—':>8s}  {'—':>10s}  {'—':>6s}  {'—':>6s}")
    else:
        print("  No prediction evaluations possible")
```

**Step 2: Move `now_ts` definition earlier**

The variable `now_ts` is used in Part 2 (line 1510) and now also in Part 10. Ensure it's defined before both. It should already exist around line 1510:
```python
            now_ts = datetime.now(timezone.utc).timestamp()
```
Move this to just before PART 2's loop, making it available to Part 10 too. Actually define it once at the top of the accuracy section:
```python
    now_ts = datetime.now(timezone.utc).timestamp()
```

**Step 3: Commit**

```bash
git add backtest.py
git commit -m "backtest: add Part 10 prediction quality analysis"
```

---

### Task 5: PART 11 — Input Data Quality Audit

**Files:**
- Modify: `/Users/admin/Documents/web3 Signals x402/backtest.py`

**Step 1: Add Part 11 after Part 10**

```python
    # ================================================================
    # PART 11: INPUT DATA QUALITY AUDIT
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 11: INPUT DATA QUALITY AUDIT")
    print(f"{'='*80}")
    print("Per-dimension score statistics and data completeness.\n")

    # Analyze all signals (full dataset for data quality visibility)
    dim_stats: Dict[str, Dict[str, Any]] = {}
    for dim_name in ALL_ROLES:
        scores_list = []
        no_data_count = 0
        full_count = 0
        partial_count = 0

        for sig in all_signals:
            dim = sig.get("dimensions", {}).get(dim_name, {})
            ds = dim.get("score")
            tier = dim.get("data_tier", "unknown")
            if ds is not None:
                scores_list.append(ds)
            if tier == "none":
                no_data_count += 1
            elif tier == "full":
                full_count += 1
            elif tier == "partial":
                partial_count += 1

        if scores_list:
            mean_s = sum(scores_list) / len(scores_list)
            sorted_s = sorted(scores_list)
            median_s = sorted_s[len(sorted_s) // 2]
            min_s = min(scores_list)
            max_s = max(scores_list)
            std_s = (sum((x - mean_s) ** 2 for x in scores_list) / len(scores_list)) ** 0.5
            cluster_pct = sum(1 for s in scores_list if 45 <= s <= 55) / len(scores_list) * 100
            spread = max_s - min_s

            dim_stats[dim_name] = {
                "mean": mean_s, "median": median_s, "min": min_s, "max": max_s,
                "std": std_s, "spread": spread, "cluster_pct": cluster_pct,
                "full": full_count, "partial": partial_count, "none": no_data_count,
                "total": len(scores_list),
            }

    print(f"  {'Dimension':>12s}  {'Mean':>5s}  {'Std':>5s}  {'Min':>5s}  {'Max':>5s}  {'Spread':>6s}  {'45-55%':>6s}  {'Full%':>5s}  {'None%':>5s}")
    print(f"  {'─'*12}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*6}  {'─'*6}  {'─'*5}  {'─'*5}")
    for dim_name in ALL_ROLES:
        ds = dim_stats.get(dim_name)
        if ds:
            full_pct = ds["full"] / ds["total"] * 100 if ds["total"] > 0 else 0
            none_pct = ds["none"] / ds["total"] * 100 if ds["total"] > 0 else 0
            cluster_warn = " ⚠️" if ds["cluster_pct"] > 70 else ""
            spread_warn = " ⚠️" if ds["spread"] < 20 else ""
            print(f"  {dim_name:>12s}  {ds['mean']:5.1f}  {ds['std']:5.1f}  {ds['min']:5.1f}  {ds['max']:5.1f}  {ds['spread']:5.0f}{spread_warn}  {ds['cluster_pct']:5.1f}{cluster_warn}  {full_pct:4.0f}%  {none_pct:4.0f}%")
        else:
            print(f"  {dim_name:>12s}  no data")

    # Cross-dimension correlation check
    print(f"\n  Cross-dimension independence (low correlation = good, dimensions add independent info):")
    dim_arrays: Dict[str, List[float]] = {}
    for dim_name in ALL_ROLES:
        dim_arrays[dim_name] = [
            sig.get("dimensions", {}).get(dim_name, {}).get("score", 50.0)
            for sig in all_signals
        ]

    pairs_checked = []
    for i, d1 in enumerate(ALL_ROLES):
        for d2 in ALL_ROLES[i+1:]:
            a1, a2 = dim_arrays[d1], dim_arrays[d2]
            n = len(a1)
            m1, m2 = sum(a1)/n, sum(a2)/n
            cov = sum((x-m1)*(y-m2) for x, y in zip(a1, a2)) / n
            s1 = (sum((x-m1)**2 for x in a1) / n) ** 0.5
            s2 = (sum((y-m2)**2 for y in a2) / n) ** 0.5
            corr = cov / (s1 * s2) if s1 > 0 and s2 > 0 else 0
            flag = " ⚠️ REDUNDANT" if abs(corr) > 0.7 else ""
            pairs_checked.append((d1, d2, corr))
            print(f"    {d1:>12s} × {d2:<12s}: r={corr:+.3f}{flag}")
```

**Step 2: Commit**

```bash
git add backtest.py
git commit -m "backtest: add Part 11 input data quality audit"
```

---

### Task 6: PART 12 — Regime Analysis

**Files:**
- Modify: `/Users/admin/Documents/web3 Signals x402/backtest.py`

**Step 1: Add regime tagging to the re-scoring loop**

In the re-scoring loop, we already call `detect_regime()` and get `regime_shifts`. We need to also tag each signal with the detected regime AND the F&G level. Add to the signal dict around line 1411:

```python
            # Get F&G value for regime analysis
            fg_for_tag = None
            market_snap_data = snapshot.get("market")
            if market_snap_data:
                fg_for_tag = market_snap_data.get("data", {}).get("sentiment", {}).get("fear_greed_index")

            all_signals.append({
                "timestamp": ts,
                "asset": asset,
                "split": "train" if idx < split_idx else "test",
                "regime": detected_regime,
                "fg_value": float(fg_for_tag) if fg_for_tag is not None else None,
                **result,
                **target_data,
            })
```

Note: `detected_regime` comes from `detect_regime()` which is already called per time point. The variable is `_` currently — change:
```python
        detected_regime, regime_shifts = detect_regime(snapshot)
```

**Step 2: Add Part 12 after Part 11**

```python
    # ================================================================
    # PART 12: REGIME ANALYSIS (test set only)
    # ================================================================
    print(f"\n{'='*80}")
    print("PART 12: REGIME ANALYSIS")
    print(f"{'='*80}")
    print("Accuracy split by market regime and Fear & Greed level.\n")

    # Use test-set evals with regime data
    regime_evals: Dict[str, List] = defaultdict(list)
    fg_bucket_evals: Dict[str, List] = defaultdict(list)

    for ev in all_evals:
        if ev.get("split", ev.get("_split")) == "train":
            continue
        # Find matching signal for regime/fg data
        # all_evals already have regime info if we tagged the signals
        regime = ev.get("regime", "unknown")
        regime_evals[regime].append(ev)

        fg = ev.get("fg_value")
        if fg is not None:
            if fg < 25:
                fg_bucket_evals["extreme_fear (<25)"].append(ev)
            elif fg < 45:
                fg_bucket_evals["fear (25-45)"].append(ev)
            elif fg < 55:
                fg_bucket_evals["neutral (45-55)"].append(ev)
            elif fg < 75:
                fg_bucket_evals["greed (55-75)"].append(ev)
            else:
                fg_bucket_evals["extreme_greed (75+)"].append(ev)

    print(f"  Accuracy by market regime:")
    print(f"  {'Regime':>12s}  {'n':>4s}  {'Gradient':>8s}  {'Binary':>6s}  {'Avg Move':>8s}")
    print(f"  {'─'*12}  {'─'*4}  {'─'*8}  {'─'*6}  {'─'*8}")
    for regime in ["trending", "ranging", "unknown"]:
        evals = regime_evals.get(regime, [])
        if evals:
            g = sum(e["gradient_score"] for e in evals) / len(evals)
            b = sum(1 for e in evals if e["binary_correct"]) / len(evals)
            m = sum(abs(e["pct_change"]) for e in evals) / len(evals)
            print(f"  {regime:>12s}  {len(evals):4d}  {g*100:7.1f}%  {b*100:5.1f}%  {m:7.2f}%")
        else:
            print(f"  {regime:>12s}  {0:4d}  {'—':>8s}  {'—':>6s}  {'—':>8s}")

    print(f"\n  Accuracy by Fear & Greed bucket:")
    print(f"  {'F&G Bucket':>22s}  {'n':>4s}  {'Gradient':>8s}  {'Binary':>6s}  {'Avg Move':>8s}")
    print(f"  {'─'*22}  {'─'*4}  {'─'*8}  {'─'*6}  {'─'*8}")
    for bucket in ["extreme_fear (<25)", "fear (25-45)", "neutral (45-55)",
                    "greed (55-75)", "extreme_greed (75+)"]:
        evals = fg_bucket_evals.get(bucket, [])
        if evals:
            g = sum(e["gradient_score"] for e in evals) / len(evals)
            b = sum(1 for e in evals if e["binary_correct"]) / len(evals)
            m = sum(abs(e["pct_change"]) for e in evals) / len(evals)
            print(f"  {bucket:>22s}  {len(evals):4d}  {g*100:7.1f}%  {b*100:5.1f}%  {m:7.2f}%")
        else:
            print(f"  {bucket:>22s}  {0:4d}  {'—':>8s}  {'—':>6s}  {'—':>8s}")
```

**Step 3: Propagate regime/fg data to eval dicts**

In the Part 2 accuracy loop where `ev` dicts are built (around line 1526), add regime and fg data from the source signal:

```python
            ev = {
                ...existing fields...
                "regime": sig.get("regime", "unknown"),
                "fg_value": sig.get("fg_value"),
                "split": sig.get("split", "test"),
            }
```

**Step 4: Commit**

```bash
git add backtest.py
git commit -m "backtest: add Part 12 regime analysis"
```

---

### Task 7: Dashboard — Add Trading Levels to Modal

**Files:**
- Modify: `/Users/admin/Documents/web3 Signals x402/api/dashboard.py:1602-1650`

**Step 1: Add `renderTradingLevels()` function**

Insert before the `openModal()` function (around line 1601):

```javascript
function renderTradingLevels(s) {
  if (!s.entry_price || s.direction === 'neutral') return '';

  const entry = s.entry_price;
  const target = s.target_price;
  const sl = s.stop_loss;
  const rr = s.risk_reward_ratio || 0;
  const conf = s.confidence || 'low';
  const predMove = s.predicted_move_pct || 0;
  const isBuy = s.direction === 'buy';

  const fmt = (v) => v >= 1000 ? v.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})
    : v >= 1 ? v.toFixed(4) : v.toFixed(6);

  const confColor = conf === 'high' ? 'green' : conf === 'medium' ? 'blue' : 'yellow';
  const moveColor = predMove > 0 ? 'green' : predMove < 0 ? 'red' : 'dim';
  const moveStr = predMove !== 0 ? `${predMove > 0 ? '+' : ''}${predMove.toFixed(2)}%` : '—';

  const riskPct = isBuy
    ? ((entry - sl) / entry * 100).toFixed(2)
    : ((sl - entry) / entry * 100).toFixed(2);
  const rewardPct = isBuy
    ? ((target - entry) / entry * 100).toFixed(2)
    : ((entry - target) / entry * 100).toFixed(2);

  return `
    <div class="modal-prediction">
      <div class="section-title">Trading Levels (48h)</div>
      <div class="pred-grid">
        <div class="pred-item">
          <span class="pred-label">Entry Price</span>
          <span class="pred-value dim">$${fmt(entry)}</span>
        </div>
        <div class="pred-item">
          <span class="pred-label">Target Price</span>
          <span class="pred-value green">$${fmt(target)} (+${rewardPct}%)</span>
        </div>
        <div class="pred-item">
          <span class="pred-label">Stop Loss</span>
          <span class="pred-value red">$${fmt(sl)} (-${riskPct}%)</span>
        </div>
        <div class="pred-item">
          <span class="pred-label">Risk:Reward</span>
          <span class="pred-value ${rr >= 2 ? 'green' : rr >= 1.5 ? 'blue' : 'yellow'}">1:${rr.toFixed(1)}</span>
        </div>
        <div class="pred-item">
          <span class="pred-label">Predicted Move</span>
          <span class="pred-value ${moveColor}">${moveStr}</span>
        </div>
        <div class="pred-item">
          <span class="pred-label">Confidence</span>
          <span class="pred-value ${confColor}" style="text-transform:capitalize">${conf}</span>
        </div>
      </div>
    </div>`;
}
```

**Step 2: Add the call in `openModal()`**

In the `openModal()` function, add `${renderTradingLevels(s)}` after `${renderModalPrediction(s)}` (around line 1627):

```javascript
    ${renderModalPrediction(s)}
    ${renderTradingLevels(s)}
    <div class="modal-dim-detail">
```

**Step 3: Commit**

```bash
git add api/dashboard.py
git commit -m "dashboard: add trading levels (entry/target/SL) to signal modal"
```

---

### Task 8: Run Backtest and Capture Output

**Step 1: Run the backtest**

```bash
cd "/Users/admin/Documents/web3 Signals x402"
python3 backtest.py 2>&1 | tee backtest_output.txt
```

Expected: Full 12-part output with train/test split, signal timeline, target evaluation, prediction quality, data quality audit, and regime analysis.

**Step 2: Commit output for analysis**

```bash
git add backtest_output.txt
git commit -m "backtest: capture v2 output for analysis"
```

---

### Task 9: Critical Analysis with Parallel Agents

**Step 1: Spin 3 analysis agents in parallel**

1. **Signal Quality Agent**: Analyze Parts 1-3, 8 — signal distribution, accuracy trends, timeline patterns
2. **Target & Prediction Agent**: Analyze Parts 9-10 — target hit rates, prediction calibration, EV per trade
3. **Data & Regime Agent**: Analyze Parts 7, 11-12 — IC values, data quality issues, regime-dependent performance

Each agent reads `backtest_output.txt` and produces a findings summary with actionable recommendations.

**Step 2: Synthesize findings and decide on autoresearch**

If findings suggest YAML parameter changes can improve metrics → run autoresearch.
If findings suggest code/algorithm changes needed → create new plan.
