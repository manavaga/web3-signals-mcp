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
from tools.abstain_sweep import sweep_abstain_thresholds
from tools.fit_scoring import (
    fit_indicator_params, score_dimension_fitted,
    TECHNICAL_INDICATORS, MARKET_INDICATORS, DERIVATIVES_INDICATORS,
)

# Scoring functions (fallback for non-fitted mode)
from scoring.dimensions import score_technical, score_market
from scoring.modifiers import detect_regime

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


def load_derivatives_data(db_path: str, symbol: str) -> dict[str, dict]:
    """Load derivatives data from SQLite, keyed by date.

    Returns: {"2025-10-08": {"funding_rate": 0.0001, ...}, ...}
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM derivatives WHERE symbol = ? ORDER BY date ASC",
            (symbol,),
        ).fetchall()
    except Exception:
        conn.close()
        return {}  # Table might not exist yet
    conn.close()

    result = {}
    for r in rows:
        result[r["date"]] = {
            "funding_rate": r["funding_rate"],
            "long_short_ratio": r["long_short_ratio"],
            "taker_buy_sell_ratio": r["taker_buy_sell_ratio"],
            "open_interest": r["open_interest"],
            "oi_change_pct": r["oi_change_pct"],
        }
    return result


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
    derivatives_data: dict[str, dict] | None = None,
) -> tuple[dict[int, dict], dict[int, float], dict[int, float]]:
    """Compute dimension scores using DATA-FITTED curves (no hardcoded scoring).

    Two-pass approach:
    1. First pass: compute raw indicator values + forward returns for all days
    2. Fit scoring params from the data (IC-based, zero magic numbers)
    3. Second pass: score each day using fitted params

    Args:
        candles: Chronological candle data for one asset.
        macro_data: {"sp500": [...], ...} keyed by source.
        fg_data: F&G index entries.
        scoring_cfg: Config dict for scoring functions.
        start_idx: First day to start computing (need history for indicators).
        derivatives_data: {"2025-10-08": {"funding_rate": ..., ...}, ...}

    Returns:
        (dimension_scores, forward_returns_24h, forward_returns_48h, last_fitted_params)
        Each keyed by day index.  last_fitted_params is the IC params dict
        from the most recent fold (or the single fit in fallback mode).
    """
    # Build date -> value maps for macro/fg alignment
    fg_by_date = {e["date"]: e["value"] for e in fg_data}
    macro_by_date: dict[str, dict[str, dict]] = {}
    for source, entries in macro_data.items():
        macro_by_date[source] = {e["date"]: e for e in entries}

    tech_cfg = scoring_cfg.get("agents", {}).get("technical", {})
    if derivatives_data is None:
        derivatives_data = {}

    # -----------------------------------------------------------------------
    # PASS 1: Compute raw indicator values + forward returns for ALL days
    # -----------------------------------------------------------------------
    raw_indicators: dict[int, dict] = {}  # day_key -> all raw indicator values
    forward_returns_24h: dict[int, float] = {}
    forward_returns_48h: dict[int, float] = {}

    for idx in range(start_idx, len(candles)):
        candle_slice = candles[: idx + 1]
        current_date = candles[idx]["date"]
        current_close = candles[idx]["close"]

        # Forward returns (need future candles for evaluation)
        if idx + 1 < len(candles):
            ret_24h = (candles[idx + 1]["close"] - current_close) / current_close * 100
        else:
            continue

        if idx + 2 < len(candles):
            ret_48h = (candles[idx + 2]["close"] - current_close) / current_close * 100
        else:
            ret_48h = ret_24h

        # Compute technical indicators
        tech_data = compute_technical_indicators(candle_slice, tech_cfg)
        if not tech_data:
            continue

        # Assemble ALL raw indicator values for this day
        day_key = idx - start_idx
        raw = dict(tech_data)  # Start with all technical indicators

        # Add F&G
        fg_val = fg_by_date.get(current_date, 50)
        raw["fear_greed"] = fg_val

        # Add macro data
        for source in ["sp500", "dxy", "nasdaq", "vix"]:
            if source in macro_by_date and current_date in macro_by_date[source]:
                entry = macro_by_date[source][current_date]
                if source == "sp500":
                    raw["sp500_change"] = entry.get("change_pct", 0.0)
                elif source == "dxy":
                    raw["dxy_change"] = entry.get("change_pct", 0.0)
                elif source == "nasdaq":
                    raw["nasdaq_change"] = entry.get("change_pct", 0.0)
                elif source == "vix":
                    raw["vix_roc"] = entry.get("change_pct", 0.0)

        # Add derivatives data (if available for this date)
        deriv = derivatives_data.get(current_date, {})
        if deriv:
            raw["funding_rate"] = deriv.get("funding_rate", 0.0)
            raw["long_short_ratio"] = deriv.get("long_short_ratio", 0.0)
            raw["taker_buy_sell_ratio"] = deriv.get("taker_buy_sell_ratio", 0.0)
            raw["oi_change_pct"] = deriv.get("oi_change_pct", 0.0)

        raw_indicators[day_key] = raw
        forward_returns_24h[day_key] = ret_24h
        forward_returns_48h[day_key] = ret_48h

    if not raw_indicators:
        return {}, {}, {}, {}

    # -----------------------------------------------------------------------
    # Collect all indicator names (needed for consistent fitting)
    # -----------------------------------------------------------------------
    all_day_keys = sorted(raw_indicators.keys())

    all_indicator_names = set()
    for raw in raw_indicators.values():
        all_indicator_names.update(raw.keys())

    # -----------------------------------------------------------------------
    # FIT + SCORE: Per-fold IC fitting on TRAIN data only (no look-ahead)
    # -----------------------------------------------------------------------
    # Generate walk-forward folds so IC params are fitted only on train data
    folds = generate_folds(len(all_day_keys))

    dimension_scores: dict[int, dict] = {}
    last_fitted_params: dict = {}

    if folds:
        # Per-fold fitting: fit IC on train days, score test days
        for fold in folds:
            train_keys = all_day_keys[fold.train_start:fold.train_end + 1]
            test_keys = all_day_keys[fold.test_start:fold.test_end + 1]

            # Build indicator series from TRAIN data only
            train_fwd_48h = [forward_returns_48h[d] for d in train_keys]
            numeric_indicators = {}
            for name in all_indicator_names:
                values = []
                for d in train_keys:
                    v = raw_indicators[d].get(name)
                    if isinstance(v, (int, float)) and v == v:  # not NaN
                        values.append(v)
                    else:
                        values.append(None)
                numeric_indicators[name] = values

            fold_fitted_params = fit_indicator_params(
                numeric_indicators, train_fwd_48h, min_obs=20,
                base_p_threshold=0.30,  # Lenient: walk-forward prevents overfitting
            )
            # Keep the last fold's fitted params (most recent data)
            last_fitted_params = fold_fitted_params

            # Score TEST days using train-fitted params
            available_tech = [i for i in TECHNICAL_INDICATORS if i in fold_fitted_params]
            available_market = [i for i in MARKET_INDICATORS if i in fold_fitted_params]
            available_deriv = [i for i in DERIVATIVES_INDICATORS if i in fold_fitted_params]

            for day_key in test_keys:
                if day_key not in raw_indicators:
                    continue
                raw = raw_indicators[day_key]
                scores: dict[str, float] = {}

                scores["technical"] = score_dimension_fitted(
                    raw, fold_fitted_params, available_tech,
                )
                if available_market:
                    scores["market"] = score_dimension_fitted(
                        raw, fold_fitted_params, available_market,
                    )
                else:
                    scores["market"] = 50.0
                if available_deriv:
                    scores["derivatives"] = score_dimension_fitted(
                        raw, fold_fitted_params, available_deriv,
                    )

                dimension_scores[day_key] = scores
    else:
        # Fallback: not enough data for folds, fit on all data
        all_fwd_48h = [forward_returns_48h[d] for d in all_day_keys]
        numeric_indicators = {}
        for name in all_indicator_names:
            values = []
            for d in all_day_keys:
                v = raw_indicators[d].get(name)
                if isinstance(v, (int, float)) and v == v:  # not NaN
                    values.append(v)
                else:
                    values.append(None)
            numeric_indicators[name] = values

        fitted_params = fit_indicator_params(
            numeric_indicators, all_fwd_48h, min_obs=20,
            base_p_threshold=0.30,  # Lenient: fallback path, less data
        )
        last_fitted_params = fitted_params

        available_tech = [i for i in TECHNICAL_INDICATORS if i in fitted_params]
        available_market = [i for i in MARKET_INDICATORS if i in fitted_params]
        available_deriv = [i for i in DERIVATIVES_INDICATORS if i in fitted_params]

        for day_key in all_day_keys:
            raw = raw_indicators[day_key]
            scores: dict[str, float] = {}

            scores["technical"] = score_dimension_fitted(
                raw, fitted_params, available_tech,
            )
            if available_market:
                scores["market"] = score_dimension_fitted(
                    raw, fitted_params, available_market,
                )
            else:
                scores["market"] = 50.0
            if available_deriv:
                scores["derivatives"] = score_dimension_fitted(
                    raw, fitted_params, available_deriv,
                )

            dimension_scores[day_key] = scores

    return dimension_scores, forward_returns_24h, forward_returns_48h, last_fitted_params


# ---------------------------------------------------------------------------
# Full backtest runner
# ---------------------------------------------------------------------------

def run_backtest(
    days: int = 180,
    assets: list[str] | None = None,
    quick: bool = False,
    db_path: str = DB_PATH,
    return_raw_data: bool = False,
) -> dict | tuple[dict, dict, dict]:
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
        return_raw_data: If True, return (results, all_asset_data, asset_configs)
                         for downstream use (e.g., abstain sweep).

    Returns: dict in baseline format, or tuple if return_raw_data=True.
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
    all_fitted_params: dict[str, dict] = {}  # per-asset fitted IC params

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

        # Load derivatives data for this asset (if available)
        deriv_data = load_derivatives_data(db_path, symbol)

        dim_scores, fwd_24h, fwd_48h, asset_fitted_params = compute_daily_scores(
            candles, macro_data, fg_data, scoring_cfg, start_idx=start_idx,
            derivatives_data=deriv_data,
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
        if asset_fitted_params:
            all_fitted_params[name] = asset_fitted_params

    if not all_asset_data:
        print("\nNo assets had sufficient data for backtesting.")
        empty = {"overall_cwa": 0.0, "assets": {}}
        if return_raw_data:
            return empty, {}, {}
        return empty

    # Run optimization
    print(f"\nOptimizing weights for {len(all_asset_data)} assets...")
    results = run_optimization(all_asset_data, asset_configs)

    # Enrich with confidence tiers
    for asset, data in results.get("assets", {}).items():
        data["confidence"] = get_confidence_tier(data.get("n_signals", 0))

    # Attach fitted IC params and per-asset weights for baseline saving.
    # Use a merged/representative fitted_params: pick the one from the asset
    # with the most scored days (most data = most reliable IC estimates).
    if all_fitted_params:
        best_asset = max(
            all_fitted_params,
            key=lambda a: len(all_asset_data.get(a, {}).get("dimension_scores", {})),
        )
        results["fitted_params"] = {
            k: {sk: sv for sk, sv in v.items()}
            for k, v in all_fitted_params[best_asset].items()
        }

    # Collect per-asset optimized weights for pipeline consumption
    per_asset_weights = {}
    for asset, data in results.get("assets", {}).items():
        w = data.get("weights")
        if w:
            per_asset_weights[asset] = w
    if per_asset_weights:
        results["per_asset_weights"] = per_asset_weights

    if return_raw_data:
        return results, all_asset_data, asset_configs
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
# Abstain Threshold Sweep
# ---------------------------------------------------------------------------

def run_abstain_sweep(
    all_asset_data: dict,
    asset_configs: dict,
    optimized_weights: dict,
) -> dict:
    """Run abstain threshold sweep for all assets using optimized weights.

    For each asset, computes composite scores using its optimized weights,
    then sweeps abstain thresholds to find the optimal combination.

    Args:
        all_asset_data: Per-asset dimension scores and forward returns.
        asset_configs: Per-asset noise/strong thresholds.
        optimized_weights: Per-asset weights from weight optimization.

    Returns: dict keyed by asset name with sweep results.
    """
    sweep_results = {}

    for asset, data in all_asset_data.items():
        dim_scores = data["dimension_scores"]
        fwd_24h = data["forward_returns_24h"]
        fwd_48h = data["forward_returns_48h"]

        weights = optimized_weights.get(asset, {})
        if not weights:
            continue

        cfg = asset_configs.get(asset, {})
        noise = cfg.get("noise_threshold", 2.0)
        strong = cfg.get("strong_threshold", 5.0)

        # Compute composite scores using optimized weights
        day_indices = sorted(
            set(dim_scores.keys()) & set(fwd_24h.keys()) & set(fwd_48h.keys())
        )

        composites = []
        returns_24h = []
        returns_48h = []
        dim_names = sorted(weights.keys())

        for d in day_indices:
            scores = dim_scores[d]
            composite = sum(scores.get(dim, 50.0) * weights.get(dim, 0.0) for dim in dim_names)
            composites.append(composite)
            returns_24h.append(fwd_24h[d])
            returns_48h.append(fwd_48h[d])

        if not composites:
            continue

        # Estimate ATR as average absolute daily return
        atr_pct = sum(abs(r) for r in returns_24h) / len(returns_24h) if returns_24h else 2.0

        result = sweep_abstain_thresholds(
            composite_scores=composites,
            forward_returns_24h=returns_24h,
            forward_returns_48h=returns_48h,
            noise_threshold=noise,
            strong_threshold=strong,
            atr_pct=atr_pct,
        )
        sweep_results[asset] = result

    return sweep_results


def print_sweep_results(sweep_results: dict) -> None:
    """Print formatted abstain sweep results."""
    if not sweep_results:
        print("No sweep results to display.")
        return

    print(f"\nABSTAIN THRESHOLD SWEEP")
    sep = "=" * 100
    print(sep)
    print(f"{'Asset':<8} {'BearDist':>8} {'BullDist':>8} {'RegMult':>8} "
          f"{'Combined':>9} {'CWA':>7} {'Acc24h':>7} {'MissRate':>9} {'Coverage':>9} {'Combos':>7}")
    print("-" * 100)

    for asset in sorted(sweep_results.keys()):
        r = sweep_results[asset]
        print(f"{asset:<8} {r['best_bearish_distance']:>8} {r['best_bullish_distance']:>8} "
              f"{r['best_regime_multiplier']:>8.1f} {r['combined_score']:>8.4f} "
              f"{r['cwa']:>6.4f} {r['accuracy_24h']:>6.1%} {r['abstain_miss_rate']:>8.1%} "
              f"{r['coverage']:>8.1%} {r['combos_tested']:>7}")

    print(sep)


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
    parser.add_argument("--sweep-abstain", action="store_true",
                        help="Run abstain threshold calibration sweep")
    args = parser.parse_args()

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")

    if not args.full and not args.quick:
        parser.print_help()
        print("\nSpecify --full or --quick to run a backtest.")
        sys.exit(0)

    asset_list = [a.strip().upper() for a in args.assets.split(",")] if args.assets else None
    days = 30 if args.quick else args.days

    need_raw = args.sweep_abstain
    raw_result = run_backtest(
        days=days,
        assets=asset_list,
        quick=args.quick,
        db_path=args.db,
        return_raw_data=need_raw,
    )

    if need_raw:
        results, all_asset_data, asset_configs_raw = raw_result
    else:
        results = raw_result

    if args.json and not args.sweep_abstain:
        print(json.dumps(results, indent=2))
    else:
        print_results(results)

    # Abstain threshold sweep
    if args.sweep_abstain:
        optimized_weights = {
            asset: data.get("weights", {})
            for asset, data in results.get("assets", {}).items()
        }
        sweep_results = run_abstain_sweep(all_asset_data, asset_configs_raw, optimized_weights)
        if args.json:
            output = {"backtest": results, "abstain_sweep": sweep_results}
            print(json.dumps(output, indent=2))
        else:
            print_sweep_results(sweep_results)

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
