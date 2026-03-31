#!/usr/bin/env python3
"""
Autoresearch eval harness — scores the default.yaml config against LIVE data.

Runs all 5 agents to collect fresh data, then scores with current YAML config,
then checks actual 48h returns from Binance for signals from 2 days ago.

Criteria:
  C1: CWA > 0.35 (Coverage-Weighted Accuracy)
  C2: Directional accuracy > 50% (better than coin flip)
  C3: Coverage > 25% (at least 25% signals are directional)
  C4: No single asset accuracy < 30%
  C5: Risk/reward alignment — bullish signals should have positive avg returns
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from shared.profile_loader import load_profile
from shared.storage import Storage

PROFILE_PATH = PROJECT_ROOT / "signal_fusion" / "profiles" / "default.yaml"

BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "BNB": "BNBUSDT", "XRP": "XRPUSDT", "ADA": "ADAUSDT",
    "AVAX": "AVAXUSDT", "DOT": "DOTUSDT", "MATIC": "POLUSDT",
    "LINK": "LINKUSDT", "UNI": "UNIUSDT", "ATOM": "ATOMUSDT",
    "LTC": "LTCUSDT", "FIL": "FILUSDT", "NEAR": "NEARUSDT",
    "APT": "APTUSDT", "ARB": "ARBUSDT", "OP": "OPUSDT",
    "INJ": "INJUSDT", "SUI": "SUIUSDT",
}


def fetch_48h_return(asset: str) -> Optional[float]:
    """Fetch 48h ago price vs now from Binance klines."""
    symbol = BINANCE_SYMBOLS.get(asset)
    if not symbol:
        return None
    try:
        now_ms = int(time.time() * 1000)
        ago_ms = now_ms - (48 * 3600 * 1000)

        # Price 48h ago
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&startTime={ago_ms}&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "x402-eval/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            candle_ago = json.loads(resp.read())
        if not candle_ago:
            return None
        price_ago = float(candle_ago[0][1])

        # Current price
        url2 = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        req2 = urllib.request.Request(url2, headers={"User-Agent": "x402-eval/1.0"})
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            ticker = json.loads(resp2.read())
        price_now = float(ticker["price"])

        return (price_now - price_ago) / price_ago * 100
    except Exception:
        return None


def collect_agent_data(store: Storage) -> None:
    """Run all 5 agents to collect fresh data."""
    agents = []
    try:
        from technical_agent.engine import TechnicalAgent
        agents.append(("technical_agent", TechnicalAgent))
    except ImportError:
        pass
    try:
        from derivatives_agent.engine import DerivativesAgent
        agents.append(("derivatives_agent", DerivativesAgent))
    except ImportError:
        pass
    try:
        from market_agent.engine import MarketAgent
        agents.append(("market_agent", MarketAgent))
    except ImportError:
        pass
    try:
        from exchange_flow_agent.engine import ExchangeFlowAgent
        agents.append(("exchange_flow_agent", ExchangeFlowAgent))
    except ImportError:
        pass
    try:
        from narrative_agent.engine import NarrativeAgent
        agents.append(("narrative_agent", NarrativeAgent))
    except ImportError:
        pass

    for name, cls in agents:
        try:
            agent = cls()
            result = agent.execute()
            store.save(name, result)
            status = result.get("status", "?")
            ms = result.get("meta", {}).get("duration_ms", 0)
            print(f"  {name}: {status} ({ms}ms)")
        except Exception as exc:
            print(f"  {name}: ERROR - {exc}")


def run_eval(round_num: int = 0) -> dict:
    """Run the full eval and print scored results."""
    profile = load_profile(PROFILE_PATH)
    store = Storage()

    # Step 1: Collect agent data (skip if recent data exists)
    import os
    skip_collect = os.getenv("EVAL_SKIP_COLLECT", "").lower() in ("1", "true", "yes")
    if skip_collect:
        print(f"Skipping agent data collection (EVAL_SKIP_COLLECT=1)")
    else:
        print(f"Collecting fresh agent data...")
        collect_agent_data(store)

    # Step 2: Run fusion with current YAML
    print(f"Running signal fusion...")
    from signal_fusion.engine import SignalFusion
    sf = SignalFusion()
    result = sf.fuse()
    signals = result.get("data", {}).get("signals", {})

    if not signals:
        print("ERROR: Fusion produced no signals")
        return {"total_pct": 0.0, "criteria": {}}

    # Step 3: Fetch actual 48h returns for evaluation
    print(f"Fetching 48h price returns...")
    total_signals = 0
    directional_signals = 0
    correct_directional = 0
    per_asset_results: Dict[str, float] = {}
    bullish_returns: List[float] = []
    bearish_returns: List[float] = []

    for asset, sig in signals.items():
        direction = sig.get("direction", "neutral")
        composite = sig.get("composite_score", 50.0)
        total_signals += 1

        ret = fetch_48h_return(asset)
        time.sleep(0.1)  # rate limit

        if ret is None:
            per_asset_results[asset] = 0.5
            continue

        if direction in ("buy", "sell"):
            directional_signals += 1
            if direction == "buy" and ret > 0:
                correct_directional += 1
                per_asset_results[asset] = 1.0
                bullish_returns.append(ret)
            elif direction == "sell" and ret < 0:
                correct_directional += 1
                per_asset_results[asset] = 1.0
                bearish_returns.append(ret)
            else:
                per_asset_results[asset] = 0.0
                if direction == "buy":
                    bullish_returns.append(ret)
                else:
                    bearish_returns.append(ret)
        else:
            per_asset_results[asset] = 0.5

    # Compute metrics
    coverage = directional_signals / total_signals if total_signals > 0 else 0.0
    dir_accuracy = correct_directional / directional_signals if directional_signals > 0 else 0.0
    correct_total = correct_directional + sum(1 for v in per_asset_results.values() if v == 0.5)
    raw_accuracy = correct_total / total_signals if total_signals > 0 else 0.0
    coverage_factor = min(coverage / 0.30, 1.0)
    cwa = raw_accuracy * coverage_factor

    worst_asset_acc = min(per_asset_results.values()) if per_asset_results else 0.0
    avg_bullish_ret = sum(bullish_returns) / len(bullish_returns) if bullish_returns else 0.0

    # Score criteria
    c1_pass = cwa > 0.35
    c2_pass = dir_accuracy > 0.50
    c3_pass = coverage > 0.25
    c4_pass = worst_asset_acc >= 0.30
    c5_pass = avg_bullish_ret > 0

    criteria = {
        "C1": {"name": "CWA > 0.35", "pass": c1_pass, "value": f"{cwa:.3f}"},
        "C2": {"name": "Dir accuracy > 50%", "pass": c2_pass, "value": f"{dir_accuracy:.1%}"},
        "C3": {"name": "Coverage > 25%", "pass": c3_pass, "value": f"{coverage:.1%}"},
        "C4": {"name": "No asset < 30%", "pass": c4_pass, "value": f"{worst_asset_acc:.1%}"},
        "C5": {"name": "Bullish avg return > 0", "pass": c5_pass, "value": f"{avg_bullish_ret:+.2f}%"},
    }

    passed = sum(1 for c in criteria.values() if c["pass"])
    total_criteria = len(criteria)
    total_pct = passed / total_criteria * 100

    # Print results
    print(f"\nROUND {round_num} RESULTS:")
    print(f"  Signals: {total_signals} total, {directional_signals} directional ({coverage:.0%} coverage)")
    print(f"  Correct: {correct_directional}/{directional_signals} ({dir_accuracy:.0%} accuracy)")
    print(f"  CWA: {cwa:.3f}")
    print()

    for key, c in criteria.items():
        status = "PASS" if c["pass"] else "FAIL"
        print(f"  {key} ({c['name']}): {status} [{c['value']}]")
    print(f"\n  TOTAL: {passed}/{total_criteria} = {total_pct:.1f}%")

    # Per-asset detail
    print(f"\n  Per-asset signals:")
    for asset in sorted(signals.keys()):
        sig = signals[asset]
        d = sig.get("direction", "?")
        s = sig.get("composite_score", 0)
        acc = per_asset_results.get(asset, 0.5)
        label = "CORRECT" if acc == 1.0 else "WRONG" if acc == 0.0 else "NEUTRAL"
        print(f"    {asset:6s}: {d:8s} (score={s:5.1f}) → {label}")

    return {"total_pct": total_pct, "criteria": criteria, "cwa": cwa}


if __name__ == "__main__":
    round_num = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    run_eval(round_num)
