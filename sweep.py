#!/usr/bin/env python3
"""Parameter sweep: regime shift intensity × abstain distance."""
import sys, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import backtest
from collections import defaultdict
from datetime import timedelta

def run_sweep():
    print("Loading data (one time)...")
    agent_names_cfg = backtest.PROFILE.get("agent_names", {})
    role_to_agent = {
        "exchange_flow": agent_names_cfg.get("exchange_flow", "exchange_flow_agent"),
        "technical": agent_names_cfg.get("technical", "technical_agent"),
        "derivatives": agent_names_cfg.get("derivatives", "derivatives_agent"),
        "narrative": agent_names_cfg.get("narrative", "narrative_agent"),
        "market": agent_names_cfg.get("market", "market_agent"),
    }
    agent_histories = {}
    for role, agent_name in role_to_agent.items():
        rows = backtest.load_agent_history(agent_name)
        agent_histories[role] = rows
        print(f"  {agent_name}: {len(rows)} snapshots")

    price_timeline = backtest.build_price_timeline(agent_histories.get("market", []))
    aligned = backtest.build_aligned_snapshots(agent_histories)
    print(f"  Aligned: {len(aligned)} time points")
    assets_list = [a.upper() for a in backtest.PROFILE.get("assets", [])]

    # Regime shift levels to test (trend, tech, whale, deriv, narr, market)
    regimes = {
        "mild (current)": {"trend": 1.5, "technical": 1.2, "whale": 0.7, "derivatives": 0.9, "narrative": 0.7, "market": 0.8},
        "med-low":        {"trend": 1.6, "technical": 1.0, "whale": 0.6, "derivatives": 0.85, "narrative": 0.65, "market": 0.85},
        "medium":         {"trend": 1.7, "technical": 0.9, "whale": 0.55, "derivatives": 0.8, "narrative": 0.6, "market": 0.9},
        "med-high":       {"trend": 1.8, "technical": 0.8, "whale": 0.5, "derivatives": 0.75, "narrative": 0.55, "market": 0.9},
        "high":           {"trend": 1.9, "technical": 0.7, "whale": 0.45, "derivatives": 0.7, "narrative": 0.5, "market": 0.95},
        "aggressive":     {"trend": 2.0, "technical": 0.5, "whale": 0.4, "derivatives": 0.6, "narrative": 0.5, "market": 1.0},
    }

    abstain_distances = [10, 11, 12]

    # Save originals
    orig_regime = dict(backtest.REGIME_CFG.get("trending", {}))
    orig_abstain = float(backtest.ABSTAIN_CFG.get("min_distance_from_center", 12))

    # Disable confidence and dynamic abstain
    backtest.CONFIDENCE_CFG["enabled"] = False
    backtest.ABSTAIN_CFG.get("dynamic", {})["enabled"] = False

    print(f"\n{'='*110}")
    print(f"{'Regime':>15s} {'Abs':>3s} | {'Gradient':>8s} | {'Binary':>6s} | {'Signals':>7s} | {'Evals':>5s} | {'Any/3+':>6s} | {'Bull':>4s} | {'Bear':>4s} | {'HiConv':>7s} | {'MedConv':>7s}")
    print(f"{'-'*19}-+-{'-'*8}-+-{'-'*6}-+-{'-'*7}-+-{'-'*5}-+-{'-'*6}-+-{'-'*4}-+-{'-'*4}-+-{'-'*7}-+-{'-'*7}")

    for regime_name, shifts in regimes.items():
        for abstain_dist in abstain_distances:
            # Patch regime trending config
            trending = backtest.REGIME_CFG.get("trending", {})
            for k, v in shifts.items():
                trending[k] = v

            backtest.ABSTAIN_CFG["min_distance_from_center"] = abstain_dist

            # Re-score
            all_signals = []
            prev_dims = {}
            backtest.prev_oi_by_asset.clear()
            backtest.prev_btc_dom_val.clear()

            for ts, snapshot in aligned:
                _, regime_shifts = backtest.detect_regime(snapshot)
                for asset in assets_list:
                    result = backtest.compute_composite(asset, snapshot, prev_dims.get(asset), regime_shifts)
                    prev_dims[asset] = result.get("dimensions", {})
                    all_signals.append({"timestamp": ts, "asset": asset, **result})

            # Dedup
            seen = set()
            unique = []
            for sig in all_signals:
                key = (sig["asset"], sig["timestamp"].strftime("%Y-%m-%d") +
                       ("_AM" if sig["timestamp"].hour < 12 else "_PM"))
                if key not in seen:
                    seen.add(key)
                    unique.append(sig)

            directional = [s for s in unique if s["direction"] != "neutral"]

            # Evaluate
            all_evals = []
            for wh in [24, 48]:
                for sig in directional:
                    asset = sig["asset"]
                    tl = price_timeline.get(asset, [])
                    if not tl:
                        continue
                    target_time = sig["timestamp"] + timedelta(hours=wh)
                    future_price = backtest.find_price_at_offset(tl, target_time, max_tolerance_hours=6.0)
                    if future_price is None:
                        continue
                    signal_price = backtest.find_price_at_offset(tl, sig["timestamp"], max_tolerance_hours=2.0)
                    if signal_price is None or signal_price <= 0:
                        continue
                    pct_change = (future_price - signal_price) / signal_price * 100
                    all_evals.append({
                        "asset": asset, "direction": sig["direction"],
                        "score": sig["composite_score"],
                        "gradient_score": backtest.gradient_score(sig["direction"], pct_change),
                        "binary_correct": backtest.binary_correct(sig["direction"], pct_change),
                    })

            if not all_evals:
                print(f"{regime_name:>15s} {abstain_dist:>3d} | {'N/A':>8s} |")
                continue

            avg_g = sum(e["gradient_score"] for e in all_evals) / len(all_evals)
            b_acc = sum(1 for e in all_evals if e["binary_correct"]) / len(all_evals)
            signal_assets = set(e["asset"] for e in all_evals)
            bull_count = sum(1 for e in all_evals if e["direction"] == "bullish")
            bear_count = sum(1 for e in all_evals if e["direction"] == "bearish")

            hi_conv = [e for e in all_evals if abs(e["score"] - 50) > 15]
            med_conv = [e for e in all_evals if 10 < abs(e["score"] - 50) <= 15]
            hi_g = sum(e["gradient_score"] for e in hi_conv) / len(hi_conv) * 100 if hi_conv else 0
            med_g = sum(e["gradient_score"] for e in med_conv) / len(med_conv) * 100 if med_conv else 0

            asset_eval_counts = defaultdict(int)
            for e in all_evals:
                asset_eval_counts[e["asset"]] += 1
            coins_3plus = sum(1 for c in asset_eval_counts.values() if c >= 3)

            marker = ">>>" if avg_g >= 0.60 and coins_3plus >= 6 else "   "
            label = f"{regime_name:>15s} {abstain_dist:>3d}"
            print(f"{marker}{label} | {avg_g*100:7.1f}% | {b_acc*100:5.1f}% | {len(directional):>7d} | {len(all_evals):>5d} | {len(signal_assets):>3d}/{coins_3plus:>2d} | {bull_count:>4d} | {bear_count:>4d} | {hi_g:5.1f}% | {med_g:5.1f}%")

    # Restore
    for k, v in orig_regime.items():
        backtest.REGIME_CFG.get("trending", {})[k] = v
    backtest.ABSTAIN_CFG["min_distance_from_center"] = orig_abstain

    print(f"\n>>> = gradient >= 60% AND coins_3+ >= 6")

if __name__ == "__main__":
    run_sweep()
