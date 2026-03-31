"""
Extract training data for Platt calibration and LightGBM from backtest infrastructure.

Reuses backtest.py's data loading + alignment to produce paired
(dimension_scores, actual_return) samples for model training.

Run: python3 -m signal_fusion.extract_training_data
Output: /tmp/calibration_data.json
"""

from __future__ import annotations

import json
import sys
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

# Import backtest utilities
from backtest import (
    load_agent_history,
    build_aligned_snapshots,
    build_price_timeline,
    parse_timestamp,
    score_whale,
    score_technical,
    score_derivatives,
    score_narrative,
    score_market,
    score_trend,
    PROFILE,
    SCORING_CFG,
    ACCURACY_CFG,
)


def find_price_at_time(
    timeline: List[Tuple[datetime, float]], target: datetime, max_tolerance_h: float = 6.0
) -> Optional[float]:
    """Find price closest to target time within tolerance."""
    best_price = None
    best_delta = timedelta(hours=max_tolerance_h)
    for ts, price in timeline:
        delta = abs(ts - target)
        if delta < best_delta:
            best_delta = delta
            best_price = price
    return best_price


def extract_training_data(
    window_hours: int = 24,
    output_path: str = "/tmp/calibration_data.json",
) -> List[Dict[str, Any]]:
    """
    Extract (dimension_scores, actual_return) pairs for all assets at all time points.

    Returns list of dicts with:
        - asset: ticker
        - timestamp: ISO string
        - dimension_scores: {whale: float, technical: float, ...}
        - composite_score: float (weighted average)
        - direction: "bullish" or "bearish"
        - pct_change: actual price change %
        - correct: bool (direction was right)
    """
    print("=" * 60)
    print("EXTRACTING TRAINING DATA FOR CALIBRATION")
    print("=" * 60)

    # Load agent histories (same as backtest)
    agent_names = ["exchange_flow_agent", "technical_agent", "derivatives_agent",
                    "narrative_agent", "market_agent"]
    agent_map = {
        "exchange_flow_agent": "exchange_flow",
        "technical_agent": "technical",
        "derivatives_agent": "derivatives",
        "narrative_agent": "narrative",
        "market_agent": "market",
    }

    histories = {}
    for name in agent_names:
        print(f"  Loading {name}...", end=" ")
        rows = load_agent_history(name)
        histories[agent_map[name]] = rows
        print(f"{len(rows)} snapshots")

    # Build aligned snapshots and price timeline
    aligned = build_aligned_snapshots(histories)
    print(f"\n  Aligned time points: {len(aligned)}")

    market_rows = histories.get("market", [])
    price_timeline = build_price_timeline(market_rows)
    print(f"  Assets with price data: {len(price_timeline)}")

    # Asset blacklist
    blacklist = set(PROFILE.get("asset_blacklist", {}).get("assets", []))
    assets = [a for a in PROFILE.get("assets", []) if a not in blacklist]
    print(f"  Assets (after blacklist): {len(assets)}")

    # Score each time point for each asset
    training_samples: List[Dict[str, Any]] = []
    now_ts = datetime.now(timezone.utc).timestamp()

    for ts, snapshot in aligned:
        # Get agent data (handle None snapshots from alignment gaps)
        whale_raw = snapshot.get("whale") or {}
        tech_raw = snapshot.get("technical") or {}
        deriv_raw = snapshot.get("derivatives") or {}
        narr_raw = snapshot.get("narrative") or {}
        market_raw = snapshot.get("market") or {}

        whale_data = whale_raw.get("data", {}) or {}
        tech_data = tech_raw.get("data", {}) or {}
        deriv_data = deriv_raw.get("data", {}) or {}
        narr_data = narr_raw.get("data", {}) or {}
        market_data = market_raw.get("data", {}) or {}

        for asset in assets:
            # Skip if no price data
            if asset not in price_timeline or len(price_timeline[asset]) < 2:
                continue

            # Find signal price and future price
            signal_price = find_price_at_time(price_timeline[asset], ts, max_tolerance_h=2)
            target_time = ts + timedelta(hours=window_hours)

            # Skip future targets
            if target_time.timestamp() > now_ts:
                continue

            future_price = find_price_at_time(price_timeline[asset], target_time, max_tolerance_h=6)

            if signal_price is None or future_price is None or signal_price <= 0:
                continue

            pct_change = ((future_price - signal_price) / signal_price) * 100

            # Score all dimensions
            try:
                whale_score, whale_detail = score_whale(asset, whale_data)
                tech_score, tech_detail = score_technical(asset, tech_data)
                deriv_score, deriv_detail = score_derivatives(asset, deriv_data)
                narr_score, narr_detail = score_narrative(asset, narr_data)
                market_score, market_detail = score_market(asset, market_data)
                trend_score, trend_detail = score_trend(asset, tech_data)
            except Exception:
                continue

            dim_scores = {
                "whale": round(whale_score, 2),
                "technical": round(tech_score, 2),
                "derivatives": round(deriv_score, 2),
                "narrative": round(narr_score, 2),
                "market": round(market_score, 2),
                "trend": round(trend_score, 2),
            }

            # Compute raw average to determine direction
            avg = sum(dim_scores.values()) / len(dim_scores)
            direction = "bullish" if avg > 50 else "bearish" if avg < 50 else "neutral"

            if direction == "neutral":
                continue  # Can't evaluate neutral signals

            correct = (
                (direction == "bullish" and pct_change > 0)
                or (direction == "bearish" and pct_change < 0)
            )

            # Fear & Greed from market data
            fg = None
            market_summary = market_data.get("data", {}).get("by_asset", {}).get(asset, {})
            if not market_summary:
                market_summary = market_data.get("by_asset", {}).get(asset, {})
            fear_greed_data = market_data.get("data", {}).get("summary", {})
            if not fear_greed_data:
                fear_greed_data = market_data.get("summary", {})
            fg = fear_greed_data.get("fear_greed_value")

            sample = {
                "asset": asset,
                "timestamp": ts.isoformat(),
                "dimension_scores": dim_scores,
                "avg_score": round(avg, 2),
                "direction": direction,
                "pct_change": round(pct_change, 4),
                "correct": correct,
                "fear_greed": fg,
            }
            training_samples.append(sample)

    print(f"\n  Total training samples: {len(training_samples)}")

    # Stats
    if training_samples:
        correct_count = sum(1 for s in training_samples if s["correct"])
        print(f"  Correct direction: {correct_count}/{len(training_samples)} ({100*correct_count/len(training_samples):.1f}%)")

        bullish = [s for s in training_samples if s["direction"] == "bullish"]
        bearish = [s for s in training_samples if s["direction"] == "bearish"]
        print(f"  Bullish: {len(bullish)}, Bearish: {len(bearish)}")

        per_dim_counts = defaultdict(int)
        for s in training_samples:
            for dim in s["dimension_scores"]:
                per_dim_counts[dim] += 1
        print(f"  Per-dimension sample counts: {dict(per_dim_counts)}")

    # Save
    with open(output_path, "w") as f:
        json.dump(training_samples, f, indent=2)
    print(f"\n  Saved to: {output_path}")

    return training_samples


if __name__ == "__main__":
    extract_training_data()
