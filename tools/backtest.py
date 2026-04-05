# tools/backtest.py
"""Full walk-forward backtest runner.

Wires together:
1. historical_fetcher -> load klines + macro from SQLite
2. indicators -> compute technical/market indicators per asset per day
3. scoring/dimensions -> score through scoring pipeline
4. walk_forward -> generate folds, evaluate signals
5. weight_optimizer -> find best weights per asset
6. deploy_gate -> compare to baseline

Usage:
    python3 -m tools.backtest --full                    # Full walk-forward backtest
    python3 -m tools.backtest --full --gate              # Also run deploy gate
    python3 -m tools.backtest --full --update-baseline   # Save results as new baseline
    python3 -m tools.backtest --quick                    # Last 30 days only (fast check)
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tools.historical_fetcher import DB_PATH, load_enabled_assets
from tools.indicators import compute_technical_indicators, compute_market_indicators
from tools.walk_forward import generate_folds, gradient_score, compute_cwa, evaluate_neutral
from tools.weight_optimizer import optimize_asset, run_optimization, get_confidence_tier
from tools.deploy_gate import check_deploy_gate, load_baseline, save_baseline

# Scoring functions
from scoring.dimensions import score_technical, score_market

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data loading from SQLite
# ---------------------------------------------------------------------------

def load_candles(db_path: str, symbol: str, limit: int | None = None) -> list[dict]:
    """Load klines from SQLite in chronological order.

    Returns list of dicts: {date, open, high, low, close, volume, timestamp}.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = "SELECT * FROM klines WHERE symbol = ? ORDER BY date ASC"
    if limit:
        query += f" LIMIT {limit}"
    rows = conn.execute(query, (symbol,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_macro_data(db_path: str) -> dict[str, list[dict]]:
    """Load macro data from SQLite, keyed by source.

    Returns: {"sp500": [...], "dxy": [...], "nasdaq": [...], "vix": [...]}
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM macro ORDER BY date ASC").fetchall()
    conn.close()

    result: dict[str, list[dict]] = {}
    for r in rows:
        source = r["source"]
        if source not in result:
            result[source] = []
        result[source].append({
            "date": r["date"],
            "close": r["close"],
            "change_pct": r["change_pct"],
        })
    return result


def load_fear_greed_data(db_path: str) -> list[dict]:
    """Load Fear & Greed index from SQLite in chronological order."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM fear_greed ORDER BY date ASC").fetchall()
    conn.close()
    return [{"date": r["date"], "value": r["value"]} for r in rows]


def load_asset_config() -> dict:
    """Load assets.yaml and return per-asset configs."""
    assets_path = Path(__file__).resolve().parent.parent / "assets.yaml"
    with open(assets_path) as f:
        return yaml.safe_load(f)


def load_scoring_config() -> dict:
    """Load config.yaml for scoring parameters."""
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Core backtest: compute dimension scores from historical data
# ---------------------------------------------------------------------------

def compute_daily_scores(
    candles: list[dict],
    macro_data: dict[str, list[dict]],
    fg_data: list[dict],
    scoring_cfg: dict,
    start_idx: int = 60,
) -> tuple[dict[int, dict], dict[int, float], dict[int, float]]:
    """Compute dimension scores and forward returns for each day.

    Args:
        candles: Chronological candle data for one asset.
        macro_data: {"sp500": [...], ...} keyed by source.
        fg_data: F&G index entries.
        scoring_cfg: Config dict for scoring functions.
        start_idx: First day to start computing (need history for indicators).

    Returns:
        (dimension_scores, forward_returns_24h, forward_returns_48h)
        Each keyed by day index.
    """
    # Build date -> index maps for macro/fg alignment
    fg_by_date = {e["date"]: e["value"] for e in fg_data}
    macro_by_date: dict[str, dict[str, dict]] = {}
    for source, entries in macro_data.items():
        macro_by_date[source] = {e["date"]: e for e in entries}

    tech_cfg = scoring_cfg.get("agents", {}).get("technical", {})
    market_cfg = scoring_cfg.get("agents", {}).get("market", {})

    dimension_scores: dict[int, dict] = {}
    forward_returns_24h: dict[int, float] = {}
    forward_returns_48h: dict[int, float] = {}

    for idx in range(start_idx, len(candles)):
        # No future data: only use candles[0:idx+1]
        candle_slice = candles[: idx + 1]
        current_date = candles[idx]["date"]
        current_close = candles[idx]["close"]

        # Forward returns (need future candles for evaluation)
        if idx + 1 < len(candles):
            ret_24h = (candles[idx + 1]["close"] - current_close) / current_close * 100
        else:
            continue  # Can't evaluate without forward data

        if idx + 2 < len(candles):
            ret_48h = (candles[idx + 2]["close"] - current_close) / current_close * 100
        else:
            ret_48h = ret_24h  # Fall back to 24h if no 48h data

        # Compute technical indicators from candle slice
        tech_data = compute_technical_indicators(candle_slice, tech_cfg)
        if not tech_data:
            continue

        # Score technical dimension
        tech_score = score_technical(tech_data, tech_cfg)

        # Compute market indicators
        fg_val = fg_by_date.get(current_date, 50)
        market_data = {
            "fear_greed": fg_val,
            "volume_ratio": tech_data.get("volume_ratio", 1.0),
            "order_book_imbalance": 1.0,  # Not available historically
            "macro_status": "neutral",
            "sp500_change": 0.0,
            "dxy_change": 0.0,
            "nasdaq_change": 0.0,
            "vix_roc": 0.0,
            "stablecoin_supply_change_7d": 0.0,
        }

        # Fill in macro data if available for this date
        for source in ["sp500", "dxy", "nasdaq", "vix"]:
            if source in macro_by_date and current_date in macro_by_date[source]:
                entry = macro_by_date[source][current_date]
                if source == "vix":
                    market_data["vix_roc"] = entry.get("change_pct", 0.0)
                    vix_close = entry.get("close", 20.0)
                    sp_change = market_data["sp500_change"]
                    if vix_close > 25 or sp_change < -1.5:
                        market_data["macro_status"] = "strong_risk_off"
                    elif vix_close < 18 and sp_change > 0.5:
                        market_data["macro_status"] = "strong_risk_on"
                    elif sp_change > 0:
                        market_data["macro_status"] = "risk_on"
                    elif sp_change < 0:
                        market_data["macro_status"] = "risk_off"
                elif source == "sp500":
                    market_data["sp500_change"] = entry.get("change_pct", 0.0)
                elif source == "dxy":
                    market_data["dxy_change"] = entry.get("change_pct", 0.0)
                elif source == "nasdaq":
                    market_data["nasdaq_change"] = entry.get("change_pct", 0.0)

        market_score = score_market(market_data, market_cfg)

        # Store dimension scores (0-100 scale)
        day_key = idx - start_idx  # Normalize to 0-based for optimizer
        dimension_scores[day_key] = {
            "technical": tech_score.score,
            "market": market_score.score,
        }
        forward_returns_24h[day_key] = ret_24h
        forward_returns_48h[day_key] = ret_48h

    return dimension_scores, forward_returns_24h, forward_returns_48h


# ---------------------------------------------------------------------------
# Full backtest runner
# ---------------------------------------------------------------------------

def run_backtest(
    days: int = 180,
    assets: list[str] | None = None,
    quick: bool = False,
    db_path: str = DB_PATH,
) -> dict:
    """Run the full walk-forward backtest.

    1. Load historical data from SQLite
    2. For each asset, compute daily dimension scores
    3. Optimize weights via grid search
    4. Return per-asset results

    Args:
        days: Number of days of data to use.
        assets: List of asset names (default: all enabled).
        quick: If True, use last 30 days only.
        db_path: Path to SQLite database.

    Returns: dict in baseline format (compatible with deploy gate).
    """
    if quick:
        days = 30

    # Check database exists
    if not Path(db_path).exists():
        print(f"ERROR: Database not found at {db_path}")
        print("Run: python3 -m tools.historical_fetcher --days 180")
        sys.exit(1)

    # Load asset config
    asset_cfg = load_asset_config()
    all_assets = asset_cfg.get("assets", {})

    # Filter to enabled assets
    if assets:
        enabled = {
            name: info
            for name, info in all_assets.items()
            if name in assets and info.get("enabled", False)
        }
    else:
        enabled = {
            name: info
            for name, info in all_assets.items()
            if info.get("enabled", False)
        }

    if not enabled:
        print("No enabled assets found.")
        sys.exit(1)

    # Load scoring config
    try:
        scoring_cfg = load_scoring_config()
    except FileNotFoundError:
        scoring_cfg = {}

    # Load macro data (shared across assets)
    macro_data = load_macro_data(db_path)
    fg_data = load_fear_greed_data(db_path)

    print(f"\nBACKTEST: {days} days, {len(enabled)} assets")
    print(f"Database: {db_path}")
    print(f"Macro data: {', '.join(f'{k}({len(v)})' for k, v in macro_data.items())}")
    print(f"F&G data: {len(fg_data)} entries")
    print()

    # Process each asset
    all_asset_data: dict = {}
    asset_configs: dict = {}

    for name, info in enabled.items():
        symbol = info["binance_symbol"]
        candles = load_candles(db_path, symbol)

        if not candles:
            print(f"  {name}: No candle data found for {symbol}, skipping")
            continue

        # Limit to requested days
        if len(candles) > days:
            candles = candles[-days:]

        print(f"  {name}: {len(candles)} candles ({candles[0]['date']} to {candles[-1]['date']})")

        # Need at least 60 candles for indicator warmup + some for testing
        min_candles = 70 if not quick else 35
        if len(candles) < min_candles:
            print(f"    WARNING: Only {len(candles)} candles, need {min_candles}. Skipping.")
            continue

        # Start index: need enough history for indicators
        start_idx = min(60, len(candles) - 10)

        dim_scores, fwd_24h, fwd_48h = compute_daily_scores(
            candles, macro_data, fg_data, scoring_cfg, start_idx=start_idx,
        )

        if len(dim_scores) < 20:
            print(f"    WARNING: Only {len(dim_scores)} scored days, need 20+. Skipping.")
            continue

        all_asset_data[name] = {
            "dimension_scores": dim_scores,
            "forward_returns_24h": fwd_24h,
            "forward_returns_48h": fwd_48h,
        }
        asset_configs[name] = {
            "noise_threshold": info.get("noise_threshold_pct", 2.0),
            "strong_threshold": info.get("strong_threshold_pct", 5.0),
        }

    if not all_asset_data:
        print("\nNo assets had sufficient data for backtesting.")
        return {"overall_cwa": 0.0, "assets": {}}

    # Run optimization
    print(f"\nOptimizing weights for {len(all_asset_data)} assets...")
    results = run_optimization(all_asset_data, asset_configs)

    # Enrich with confidence tiers
    for asset, data in results.get("assets", {}).items():
        data["confidence"] = get_confidence_tier(data.get("n_signals", 0))

    return results


# ---------------------------------------------------------------------------
# Results display
# ---------------------------------------------------------------------------

def print_results(results: dict) -> None:
    """Print formatted results table."""
    assets = results.get("assets", {})
    if not assets:
        print("No results to display.")
        return

    # Header
    total_days = sum(a.get("n_signals", 0) for a in assets.values())
    n_assets = len(assets)
    print(f"\nBACKTEST RESULTS")
    sep = "=" * 90
    print(sep)
    print(f"{'Asset':<8} {'Signals':>7} {'Acc24h':>7} {'Acc48h':>7} "
          f"{'Coverage':>9} {'CWA24':>7} {'CWA48':>7} {'AbsMiss':>8} {'Conf':<6}")
    print("-" * 90)

    total_cwa = 0.0
    total_acc = 0.0
    total_signals = 0

    for asset in sorted(assets.keys()):
        a = assets[asset]
        n = a.get("n_signals", 0)
        acc24 = a.get("accuracy_24h", 0.0)
        acc48 = a.get("accuracy_48h", 0.0)
        cov = a.get("coverage", 0.0)
        cwa24 = a.get("cwa_24h", 0.0)
        cwa48 = a.get("cwa_48h", 0.0)
        abm = a.get("abstain_miss_rate", 0.0)
        conf = a.get("confidence", "?")

        print(f"{asset:<8} {n:>7} {acc24:>6.1%} {acc48:>6.1%} "
              f"{cov:>8.1%} {cwa24:>6.1%} {cwa48:>6.1%} {abm:>7.1%} {conf:<6}")

        total_cwa += cwa24
        total_acc += acc24
        total_signals += n

    print("-" * 90)
    avg_cwa = total_cwa / n_assets if n_assets > 0 else 0.0
    avg_acc = total_acc / n_assets if n_assets > 0 else 0.0
    print(f"{'ALL':<8} {total_signals:>7} {avg_acc:>6.1%} {'--':>7} "
          f"{'--':>9} {avg_cwa:>6.1%} {'--':>7} {'--':>8} {'--':<6}")
    print(sep)

    # Optimized weights
    print(f"\nOPTIMIZED WEIGHTS")
    print("=" * 60)
    # Detect dimension names from first asset
    sample_weights = next(iter(assets.values())).get("weights", {})
    dim_names = sorted(sample_weights.keys())
    header = f"{'Asset':<8} " + " ".join(f"{d:>12}" for d in dim_names) + f" {'Conf':<6}"
    print(header)
    print("-" * 60)

    for asset in sorted(assets.keys()):
        a = assets[asset]
        w = a.get("weights", {})
        conf = a.get("confidence", "?")
        row = f"{asset:<8} "
        row += " ".join(f"{w.get(d, 0.0):>12.2f}" for d in dim_names)
        row += f" {conf:<6}"
        print(row)
    print("=" * 60)

    print(f"\nOverall CWA: {results.get('overall_cwa', 0.0):.4f}")
    print(f"Overall Accuracy (24h): {results.get('overall_accuracy_24h', 0.0):.4f}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward backtest")
    parser.add_argument("--full", action="store_true", help="Full 180-day backtest")
    parser.add_argument("--quick", action="store_true", help="Quick 30-day check")
    parser.add_argument("--days", type=int, default=180, help="Number of days (default: 180)")
    parser.add_argument("--assets", type=str, help="Comma-separated assets (default: all)")
    parser.add_argument("--gate", action="store_true", help="Run deploy gate comparison")
    parser.add_argument("--update-baseline", action="store_true", help="Save as new baseline")
    parser.add_argument("--db", type=str, default=DB_PATH, help="SQLite database path")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")

    if not args.full and not args.quick:
        parser.print_help()
        print("\nSpecify --full or --quick to run a backtest.")
        sys.exit(0)

    asset_list = [a.strip().upper() for a in args.assets.split(",")] if args.assets else None
    days = 30 if args.quick else args.days

    results = run_backtest(
        days=days,
        assets=asset_list,
        quick=args.quick,
        db_path=args.db,
    )

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_results(results)

    # Deploy gate
    if args.gate or args.update_baseline:
        print("\n" + "=" * 60)
        print("DEPLOY GATE")
        print("=" * 60)

        baseline = load_baseline()
        gate_result = check_deploy_gate(baseline, results)

        if gate_result["passed"]:
            print("PASSED")
            summary = gate_result["summary"]
            if summary["overall_cwa_baseline"] is not None:
                print(f"  CWA: {summary['overall_cwa_baseline']:.4f} -> "
                      f"{summary['overall_cwa_proposed']:.4f}")
            else:
                print(f"  CWA: (no baseline) -> {summary['overall_cwa_proposed']:.4f}")
            if summary["improved_assets"]:
                print(f"  Improved: {', '.join(summary['improved_assets'])}")
            if summary["regressed_assets"]:
                print(f"  Regressed: {', '.join(summary['regressed_assets'])}")
        else:
            print("FAILED")
            for failure in gate_result["failures"]:
                print(f"  - {failure}")

        if args.update_baseline:
            if gate_result["passed"]:
                path = save_baseline(results)
                print(f"\nBaseline saved to {path}")
            else:
                print("\nBaseline NOT saved (gate failed).")
                print("Use --update-baseline without --gate to force save,")
                print("or fix the regressions first.")


if __name__ == "__main__":
    main()
