#!/usr/bin/env python3
"""
Retroactive Accuracy Analysis for Web3 Signal Fusion System
Computes gradient accuracy scores for BUY/SELL signals over the last 7 days.
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ─── Configuration ───────────────────────────────────────────────────────────

SIGNAL_API = "https://web3-signals-api-production.up.railway.app/api/history"
COINGECKO_API = "https://api.coingecko.com/api/v3/coins"

# Symbol -> CoinGecko ID mapping
SYMBOL_TO_GECKO = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "ADA": "cardano",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "MATIC": "matic-network",
    "LINK": "chainlink",
    "UNI": "uniswap",
    "ATOM": "cosmos",
    "LTC": "litecoin",
    "FIL": "filecoin",
    "NEAR": "near",
    "APT": "aptos",
    "ARB": "arbitrum",
    "OP": "optimism",
    "INJ": "injective-protocol",
    "SUI": "sui",
}

COINGECKO_DELAY = 7  # seconds between requests


def fetch_json(url, retries=3):
    """Fetch JSON from URL with retries."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 30 * (attempt + 1)
                print(f"  [Rate limited] Waiting {wait}s before retry...")
                time.sleep(wait)
            else:
                print(f"  [HTTP {e.code}] {url}")
                if attempt < retries - 1:
                    time.sleep(5)
        except Exception as e:
            print(f"  [Error] {e}")
            if attempt < retries - 1:
                time.sleep(5)
    return None


def parse_iso_timestamp(ts_str):
    """Parse ISO timestamp string to datetime (UTC)."""
    # Handle various formats
    ts_str = ts_str.replace("+00:00", "+0000").replace("Z", "+0000")
    # Remove microseconds for simpler parsing
    if "." in ts_str:
        base, rest = ts_str.split(".", 1)
        # Find the timezone part
        tz_part = ""
        for sep in ["+", "-"]:
            if sep in rest[1:]:  # skip first char in case of negative
                idx = rest.index(sep, 1)
                tz_part = rest[idx:]
                break
        ts_str = base + tz_part

    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return dt.astimezone(timezone.utc)


# ─── Step 1: Fetch Signals ──────────────────────────────────────────────────

def fetch_all_signals():
    """Fetch signal history, deduplicate, and sample every ~4 hours."""
    print("=" * 70)
    print("STEP 1: Fetching signal history...")
    print("=" * 70)

    all_rows = []
    batch_size = 200
    offset = 0
    total = 1343

    while offset < total:
        limit = min(batch_size, total - offset)
        url = f"{SIGNAL_API}?limit={limit}&offset={offset}"
        print(f"  Fetching offset={offset}, limit={limit}...")
        data = fetch_json(url)
        if data and "rows" in data:
            all_rows.extend(data["rows"])
            print(f"    Got {len(data['rows'])} rows (total so far: {len(all_rows)})")
        else:
            print(f"    Failed to fetch batch at offset={offset}")
        offset += batch_size
        time.sleep(1)

    print(f"\n  Total raw rows fetched: {len(all_rows)}")

    # Deduplicate by timestamp (keep first occurrence)
    seen_timestamps = set()
    deduped = []
    for row in all_rows:
        ts = row.get("timestamp", "")
        if ts not in seen_timestamps:
            seen_timestamps.add(ts)
            deduped.append(row)

    print(f"  After deduplication: {len(deduped)} unique rows")

    # Parse timestamps and sort
    parsed = []
    for row in deduped:
        dt = parse_iso_timestamp(row["timestamp"])
        if dt:
            parsed.append((dt, row))
    parsed.sort(key=lambda x: x[0])

    print(f"  Parsed timestamps: {len(parsed)}")
    if parsed:
        print(f"  Date range: {parsed[0][0].strftime('%Y-%m-%d %H:%M')} to {parsed[-1][0].strftime('%Y-%m-%d %H:%M')} UTC")

    # Sample every ~4 hours (skip rows that are <3.5 hours apart)
    sampled = []
    last_dt = None
    for dt, row in parsed:
        if last_dt is None or (dt - last_dt) >= timedelta(hours=3, minutes=30):
            sampled.append((dt, row))
            last_dt = dt

    print(f"  After ~4h sampling: {len(sampled)} signal snapshots")

    # Only keep last 7 days
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    sampled = [(dt, row) for dt, row in sampled if dt >= cutoff]
    print(f"  After 7-day filter: {len(sampled)} snapshots")

    return sampled


# ─── Step 2: Fetch Price Data ────────────────────────────────────────────────

def fetch_price_data():
    """Fetch 7-day hourly price data from CoinGecko for all assets."""
    print("\n" + "=" * 70)
    print("STEP 2: Fetching price data from CoinGecko...")
    print("=" * 70)

    price_data = {}  # symbol -> [(unix_ts_seconds, price), ...]

    for i, (symbol, gecko_id) in enumerate(SYMBOL_TO_GECKO.items()):
        url = f"{COINGECKO_API}/{gecko_id}/market_chart?vs_currency=usd&days=8"
        print(f"  [{i+1}/{len(SYMBOL_TO_GECKO)}] Fetching {symbol} ({gecko_id})...")

        data = fetch_json(url)
        if data and "prices" in data:
            prices = [(ts / 1000, price) for ts, price in data["prices"]]
            price_data[symbol] = prices
            print(f"    Got {len(prices)} price points, range: ${prices[0][1]:.2f} - ${prices[-1][1]:.2f}")
        else:
            print(f"    FAILED to fetch {symbol}")

        if i < len(SYMBOL_TO_GECKO) - 1:
            print(f"    Waiting {COINGECKO_DELAY}s for rate limit...")
            time.sleep(COINGECKO_DELAY)

    print(f"\n  Successfully fetched price data for {len(price_data)}/{len(SYMBOL_TO_GECKO)} assets")
    return price_data


def find_price_at_time(price_series, target_ts):
    """
    Find the closest price to a given Unix timestamp.
    Returns (price, actual_ts) or (None, None) if no data within 2 hours.
    """
    if not price_series:
        return None, None

    best_price = None
    best_ts = None
    best_diff = float("inf")

    for ts, price in price_series:
        diff = abs(ts - target_ts)
        if diff < best_diff:
            best_diff = diff
            best_price = price
            best_ts = ts

    # Only accept if within 2 hours
    if best_diff <= 7200:
        return best_price, best_ts
    return None, None


# ─── Step 3: Compute Accuracy ───────────────────────────────────────────────

def gradient_score(direction, pct_change):
    """
    Compute gradient accuracy score.
    For BUY: price up is correct.
    For SELL: price down is correct (invert).
    """
    if direction == "sell":
        pct_change = -pct_change  # Invert for sell signals

    if pct_change > 5:
        return 1.0
    elif pct_change >= 2:
        return 0.7
    elif pct_change >= 0:
        return 0.4
    elif pct_change >= -2:
        return 0.2
    else:
        return 0.0


def compute_accuracy(sampled_signals, price_data):
    """Compute accuracy for all directional signals."""
    print("\n" + "=" * 70)
    print("STEP 3: Computing accuracy scores...")
    print("=" * 70)

    results_24h = []
    results_48h = []
    signal_counts = {"buy": 0, "sell": 0, "neutral": 0, "abstain": 0}
    per_day = defaultdict(lambda: {"scores_24h": [], "scores_48h": [], "buy": 0, "sell": 0, "neutral": 0})
    per_asset = defaultdict(lambda: {"scores_24h": [], "scores_48h": [], "buy": 0, "sell": 0, "neutral": 0})
    best_signals = []
    worst_signals = []

    now_ts = datetime.now(timezone.utc).timestamp()

    for dt, row in sampled_signals:
        signal_ts = dt.timestamp()
        day_key = dt.strftime("%Y-%m-%d")

        # Extract signals
        try:
            signals = row["data"]["data"]["signals"]
        except (KeyError, TypeError):
            continue

        for symbol, sig_data in signals.items():
            if symbol not in SYMBOL_TO_GECKO:
                continue

            direction = sig_data.get("direction", "neutral").lower()
            composite = sig_data.get("composite_score", 0)
            is_abstain = sig_data.get("abstain", False)

            # Count signal types
            if is_abstain or direction == "neutral":
                signal_counts["neutral"] += 1
                per_day[day_key]["neutral"] += 1
                per_asset[symbol]["neutral"] += 1
                continue

            if direction == "buy":
                signal_counts["buy"] += 1
                per_day[day_key]["buy"] += 1
                per_asset[symbol]["buy"] += 1
            elif direction == "sell":
                signal_counts["sell"] += 1
                per_day[day_key]["sell"] += 1
                per_asset[symbol]["sell"] += 1
            else:
                signal_counts["neutral"] += 1
                per_day[day_key]["neutral"] += 1
                per_asset[symbol]["neutral"] += 1
                continue

            if symbol not in price_data:
                continue

            prices = price_data[symbol]

            # Find price at signal time
            price_at_t, _ = find_price_at_time(prices, signal_ts)
            if price_at_t is None:
                continue

            # 24h accuracy
            target_24h = signal_ts + 86400
            if target_24h <= now_ts:
                price_at_24h, _ = find_price_at_time(prices, target_24h)
                if price_at_24h is not None:
                    pct_24h = ((price_at_24h - price_at_t) / price_at_t) * 100
                    score_24h = gradient_score(direction, pct_24h)
                    results_24h.append(score_24h)
                    per_day[day_key]["scores_24h"].append(score_24h)
                    per_asset[symbol]["scores_24h"].append(score_24h)

                    entry = {
                        "symbol": symbol,
                        "direction": direction,
                        "composite": composite,
                        "time": dt.strftime("%m-%d %H:%M"),
                        "price_t": price_at_t,
                        "price_24h": price_at_24h,
                        "pct_24h": pct_24h,
                        "score_24h": score_24h,
                    }
                    best_signals.append(entry)
                    worst_signals.append(entry)

            # 48h accuracy
            target_48h = signal_ts + 172800
            if target_48h <= now_ts:
                price_at_48h, _ = find_price_at_time(prices, target_48h)
                if price_at_48h is not None:
                    pct_48h = ((price_at_48h - price_at_t) / price_at_t) * 100
                    score_48h = gradient_score(direction, pct_48h)
                    results_48h.append(score_48h)
                    per_day[day_key]["scores_48h"].append(score_48h)
                    per_asset[symbol]["scores_48h"].append(score_48h)

    # Sort best/worst
    best_signals.sort(key=lambda x: x["score_24h"], reverse=True)
    worst_signals.sort(key=lambda x: x["score_24h"])

    return results_24h, results_48h, signal_counts, per_day, per_asset, best_signals[:20], worst_signals[:20]


# ─── Step 4: Display Results ────────────────────────────────────────────────

def display_results(results_24h, results_48h, signal_counts, per_day, per_asset, best, worst):
    """Print comprehensive results."""
    print("\n")
    print("=" * 70)
    print("  RETROACTIVE ACCURACY ANALYSIS - Web3 Signal Fusion System")
    print("  Analysis Period: Last 7 Days (Feb 24 - Mar 3, 2026)")
    print("=" * 70)

    # Overall counts
    total_signals = signal_counts["buy"] + signal_counts["sell"] + signal_counts["neutral"]
    print(f"\n--- SIGNAL DISTRIBUTION ---")
    print(f"  Total signal instances:  {total_signals}")
    print(f"  BUY signals:             {signal_counts['buy']} ({signal_counts['buy']/max(total_signals,1)*100:.1f}%)")
    print(f"  SELL signals:            {signal_counts['sell']} ({signal_counts['sell']/max(total_signals,1)*100:.1f}%)")
    print(f"  NEUTRAL/ABSTAIN:         {signal_counts['neutral']} ({signal_counts['neutral']/max(total_signals,1)*100:.1f}%)")
    print(f"  Directional (scored):    {signal_counts['buy'] + signal_counts['sell']}")

    # Overall accuracy
    print(f"\n--- OVERALL ACCURACY ---")
    if results_24h:
        avg_24h = sum(results_24h) / len(results_24h)
        print(f"  24h Accuracy:  {avg_24h:.3f}  (n={len(results_24h)} scored signals)")
        # Distribution
        perfect = sum(1 for s in results_24h if s == 1.0)
        good = sum(1 for s in results_24h if s == 0.7)
        ok = sum(1 for s in results_24h if s == 0.4)
        poor = sum(1 for s in results_24h if s == 0.2)
        wrong = sum(1 for s in results_24h if s == 0.0)
        print(f"    1.0 (perfect): {perfect:4d} ({perfect/len(results_24h)*100:.1f}%)")
        print(f"    0.7 (good):    {good:4d} ({good/len(results_24h)*100:.1f}%)")
        print(f"    0.4 (ok):      {ok:4d} ({ok/len(results_24h)*100:.1f}%)")
        print(f"    0.2 (poor):    {poor:4d} ({poor/len(results_24h)*100:.1f}%)")
        print(f"    0.0 (wrong):   {wrong:4d} ({wrong/len(results_24h)*100:.1f}%)")
    else:
        print("  24h Accuracy:  N/A (no scored signals)")

    if results_48h:
        avg_48h = sum(results_48h) / len(results_48h)
        print(f"\n  48h Accuracy:  {avg_48h:.3f}  (n={len(results_48h)} scored signals)")
        perfect = sum(1 for s in results_48h if s == 1.0)
        good = sum(1 for s in results_48h if s == 0.7)
        ok = sum(1 for s in results_48h if s == 0.4)
        poor = sum(1 for s in results_48h if s == 0.2)
        wrong = sum(1 for s in results_48h if s == 0.0)
        print(f"    1.0 (perfect): {perfect:4d} ({perfect/len(results_48h)*100:.1f}%)")
        print(f"    0.7 (good):    {good:4d} ({good/len(results_48h)*100:.1f}%)")
        print(f"    0.4 (ok):      {ok:4d} ({ok/len(results_48h)*100:.1f}%)")
        print(f"    0.2 (poor):    {poor:4d} ({poor/len(results_48h)*100:.1f}%)")
        print(f"    0.0 (wrong):   {wrong:4d} ({wrong/len(results_48h)*100:.1f}%)")
    else:
        print("  48h Accuracy:  N/A (no scored signals)")

    # Per-day breakdown
    print(f"\n--- PER-DAY BREAKDOWN ---")
    print(f"  {'Date':<12} {'BUY':>5} {'SELL':>5} {'NEUT':>5} {'Avg24h':>8} {'n24':>5} {'Avg48h':>8} {'n48':>5}")
    print(f"  {'-'*60}")
    for day in sorted(per_day.keys()):
        d = per_day[day]
        avg_24 = sum(d["scores_24h"]) / len(d["scores_24h"]) if d["scores_24h"] else 0
        avg_48 = sum(d["scores_48h"]) / len(d["scores_48h"]) if d["scores_48h"] else 0
        n24 = len(d["scores_24h"])
        n48 = len(d["scores_48h"])
        print(f"  {day:<12} {d['buy']:>5} {d['sell']:>5} {d['neutral']:>5} {avg_24:>8.3f} {n24:>5} {avg_48:>8.3f} {n48:>5}")

    # Per-asset breakdown
    print(f"\n--- PER-ASSET ACCURACY (24h) ---")
    print(f"  {'Asset':<8} {'BUY':>5} {'SELL':>5} {'NEUT':>5} {'Avg24h':>8} {'n':>4}")
    print(f"  {'-'*42}")
    asset_rows = []
    for symbol in sorted(per_asset.keys()):
        a = per_asset[symbol]
        avg = sum(a["scores_24h"]) / len(a["scores_24h"]) if a["scores_24h"] else -1
        asset_rows.append((symbol, a, avg))
    asset_rows.sort(key=lambda x: x[2], reverse=True)
    for symbol, a, avg in asset_rows:
        avg_str = f"{avg:.3f}" if avg >= 0 else "N/A"
        n = len(a["scores_24h"])
        print(f"  {symbol:<8} {a['buy']:>5} {a['sell']:>5} {a['neutral']:>5} {avg_str:>8} {n:>4}")

    # Best signals
    print(f"\n--- TOP 10 BEST SIGNALS (24h) ---")
    print(f"  {'Time':<12} {'Asset':<6} {'Dir':<5} {'Score':>6} {'Price@T':>10} {'Price@24h':>10} {'%Chg':>7} {'Accuracy':>8}")
    print(f"  {'-'*70}")
    for s in best[:10]:
        print(f"  {s['time']:<12} {s['symbol']:<6} {s['direction']:<5} {s['composite']:>6.1f} {s['price_t']:>10.2f} {s['price_24h']:>10.2f} {s['pct_24h']:>+7.2f}% {s['score_24h']:>8.1f}")

    # Worst signals
    print(f"\n--- TOP 10 WORST SIGNALS (24h) ---")
    print(f"  {'Time':<12} {'Asset':<6} {'Dir':<5} {'Score':>6} {'Price@T':>10} {'Price@24h':>10} {'%Chg':>7} {'Accuracy':>8}")
    print(f"  {'-'*70}")
    for s in worst[:10]:
        print(f"  {s['time']:<12} {s['symbol']:<6} {s['direction']:<5} {s['composite']:>6.1f} {s['price_t']:>10.2f} {s['price_24h']:>10.2f} {s['pct_24h']:>+7.2f}% {s['score_24h']:>8.1f}")

    # Summary interpretation
    print(f"\n--- INTERPRETATION ---")
    if results_24h:
        avg = sum(results_24h) / len(results_24h)
        if avg >= 0.7:
            print("  System is performing WELL - majority of directional signals are correct.")
        elif avg >= 0.5:
            print("  System is performing MODERATELY - some predictive edge detected.")
        elif avg >= 0.35:
            print("  System is performing MARGINALLY - slight edge, high noise.")
        else:
            print("  System is performing POORLY - signals may be inversely correlated.")

        # Bias analysis
        if signal_counts["buy"] > 0 and signal_counts["sell"] > 0:
            buy_ratio = signal_counts["buy"] / (signal_counts["buy"] + signal_counts["sell"])
            print(f"  Buy/Sell ratio: {buy_ratio:.1%} buy, {1-buy_ratio:.1%} sell")
            if buy_ratio > 0.7:
                print("  WARNING: Strong bullish bias in signals.")
            elif buy_ratio < 0.3:
                print("  WARNING: Strong bearish bias in signals.")

        neutral_ratio = signal_counts["neutral"] / max(total_signals, 1)
        print(f"  Abstain rate: {neutral_ratio:.1%}")
        if neutral_ratio > 0.5:
            print("  NOTE: System is cautious - abstains on >50% of signals.")

    print("\n" + "=" * 70)
    print("  Analysis complete.")
    print("=" * 70)


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start_time = time.time()

    # Step 1: Fetch signals
    sampled_signals = fetch_all_signals()

    # Step 2: Fetch prices
    price_data = fetch_price_data()

    # Step 3: Compute accuracy
    results_24h, results_48h, signal_counts, per_day, per_asset, best, worst = compute_accuracy(
        sampled_signals, price_data
    )

    # Step 4: Display results
    display_results(results_24h, results_48h, signal_counts, per_day, per_asset, best, worst)

    elapsed = time.time() - start_time
    print(f"\n  Total runtime: {elapsed:.0f} seconds")
