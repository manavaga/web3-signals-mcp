# tools/trade_simulator.py
"""TP/SL trade simulator — the REAL accuracy metric.

Simulates actual trades using historical data:
1. Generate signals (composite + direction) from backtest infrastructure
2. Calculate TP/SL using S/R levels, ATR, etc.
3. Check if TP or SL is hit first using intraday high/low
4. Compute per-trade PnL and cumulative returns

This answers: "How much money would we have made?"

Usage:
    python3 -m tools.trade_simulator --days 90
    python3 -m tools.trade_simulator --days 30 --assets BTC,ETH
    python3 -m tools.trade_simulator --days 180 --capital 10000
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import yaml

from tools.historical_fetcher import DB_PATH
from tools.backtest import (
    load_candles, load_macro_data, load_fear_greed_data,
    load_derivatives_data, load_asset_config, load_scoring_config,
    compute_daily_scores,
)
from tools.indicators import compute_technical_indicators
from tools.fit_scoring import TECHNICAL_INDICATORS, MARKET_INDICATORS, DERIVATIVES_INDICATORS
from tools.learned_params import (
    load_learned_state, learn_asset_params, LearnedState, AssetLearnedParams,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trade data structures
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    asset: str
    date: str
    direction: str              # "bullish" or "bearish"
    composite: float            # 0-100
    entry_price: float
    target_price: float
    stop_loss: float
    risk_reward_ratio: float
    # Outcome
    outcome: str = ""           # "tp_hit", "sl_hit", "expired_win", "expired_loss"
    exit_price: float = 0.0
    exit_date: str = ""
    pnl_pct: float = 0.0       # % return on this trade
    days_held: int = 0
    # Context
    regime: str = ""
    confidence: str = ""


@dataclass
class SimulationResult:
    asset: str
    total_trades: int = 0
    tp_hits: int = 0
    sl_hits: int = 0
    expired_wins: int = 0
    expired_losses: int = 0
    win_rate: float = 0.0       # (tp_hits + expired_wins) / total
    tp_hit_rate: float = 0.0    # tp_hits / total
    total_pnl_pct: float = 0.0
    fee_adjusted_pnl: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0  # gross wins / gross losses
    trades: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Trade outcome evaluation
# ---------------------------------------------------------------------------

def evaluate_trade(
    trade: Trade,
    future_candles: list[dict],
    max_days: int = 2,
) -> Trade:
    """Determine trade outcome using future candle high/low.

    Checks each future candle's high and low to see if TP or SL is hit.
    With daily candles, we check if TP or SL is within the day's range.
    If both could be hit on the same day, assume SL hit first (conservative).

    Args:
        trade: Trade with entry, target, stop_loss filled in.
        future_candles: Next N candles after entry.
        max_days: Max days to hold (default 2 = 48h with daily candles).

    Returns: Trade with outcome, exit_price, pnl_pct filled in.
    """
    for i, candle in enumerate(future_candles[:max_days]):
        high = candle["high"]
        low = candle["low"]
        close = candle["close"]

        tp_hit = False
        sl_hit = False

        if trade.direction == "bullish":
            tp_hit = high >= trade.target_price
            sl_hit = low <= trade.stop_loss
        else:
            tp_hit = low <= trade.target_price
            sl_hit = high >= trade.stop_loss

        if tp_hit and sl_hit:
            # Both possible on same candle — be conservative, count SL
            trade.outcome = "sl_hit"
            trade.exit_price = trade.stop_loss
            trade.exit_date = candle["date"]
            trade.days_held = i + 1
            break
        elif sl_hit:
            trade.outcome = "sl_hit"
            trade.exit_price = trade.stop_loss
            trade.exit_date = candle["date"]
            trade.days_held = i + 1
            break
        elif tp_hit:
            trade.outcome = "tp_hit"
            trade.exit_price = trade.target_price
            trade.exit_date = candle["date"]
            trade.days_held = i + 1
            break
    else:
        # Neither hit within timeframe — evaluate at last close
        if future_candles and len(future_candles) >= max_days:
            last = future_candles[min(max_days - 1, len(future_candles) - 1)]
            trade.exit_price = last["close"]
            trade.exit_date = last["date"]
            trade.days_held = max_days

            if trade.direction == "bullish":
                trade.outcome = "expired_win" if trade.exit_price > trade.entry_price else "expired_loss"
            else:
                trade.outcome = "expired_win" if trade.exit_price < trade.entry_price else "expired_loss"
        else:
            trade.outcome = "expired_loss"
            trade.exit_price = trade.entry_price
            trade.days_held = 0

    # Calculate PnL
    if trade.direction == "bullish":
        trade.pnl_pct = (trade.exit_price - trade.entry_price) / trade.entry_price * 100
    else:
        trade.pnl_pct = (trade.entry_price - trade.exit_price) / trade.entry_price * 100

    return trade


# ---------------------------------------------------------------------------
# Risk-adjusted metrics
# ---------------------------------------------------------------------------

def compute_risk_metrics(
    trades: list[dict],
    monte_carlo_n: int = 1000,
    annualization_factor: float = 365.0,
) -> dict:
    """Compute risk-adjusted metrics from trade results.

    Args:
        trades: list of dicts with at minimum: pnl_pct, date. Optional: regime.
        monte_carlo_n: Number of bootstrap iterations.
        annualization_factor: Days per year for annualization.

    Returns dict with sharpe_ratio, sortino_ratio, calmar_ratio, max_dd_duration_days,
    monte_carlo: {p_value, median_pnl, pnl_5th, pnl_95th}, regime_split: {regime: {trades, win_rate, pnl}}
    """
    import random

    if not trades:
        return {
            "sharpe_ratio": 0, "sortino_ratio": 0, "calmar_ratio": 0,
            "max_dd_duration_days": 0,
            "monte_carlo": {"p_value": 1.0, "median_pnl": 0, "pnl_5th": 0, "pnl_95th": 0},
            "regime_split": {},
        }

    def _get(t, key, default=0):
        """Get attribute from dict or dataclass."""
        if isinstance(t, dict):
            return t.get(key, default)
        return getattr(t, key, default)

    pnls = [_get(t, "pnl_pct") for t in trades]
    n = len(pnls)

    # Daily PnL aggregation
    daily_pnl: dict[str, float] = {}
    for t in trades:
        d = _get(t, "date", "")
        daily_pnl[d] = daily_pnl.get(d, 0) + _get(t, "pnl_pct")
    daily_returns = list(daily_pnl.values())

    # Sharpe Ratio (annualized, excess returns over risk-free rate)
    trades_per_year = 365 / 2  # 48h holding period = ~182.5 trades/year
    risk_free_annual = 0.05  # 5% annual T-bill rate
    risk_free_per_trade = risk_free_annual / trades_per_year

    pnl_fractions = [p / 100 for p in pnls]  # Convert % to fraction
    excess_returns = [p - risk_free_per_trade for p in pnl_fractions]

    mean_excess = sum(excess_returns) / n if n > 0 else 0
    if n > 1:
        variance = sum((r - mean_excess) ** 2 for r in excess_returns) / (n - 1)
        std_excess = variance ** 0.5
    else:
        std_excess = 0

    if std_excess > 0:
        sharpe = mean_excess / std_excess * (trades_per_year ** 0.5)
    elif mean_excess < 0:
        sharpe = -99.0  # All-loss constant returns
    else:
        sharpe = 0

    # Sortino Ratio (downside deviation only) — uses daily returns (not per-trade)
    mean_ret = sum(daily_returns) / len(daily_returns)
    downside = [r for r in daily_returns if r < 0]
    if downside:
        down_var = sum(r ** 2 for r in downside) / len(daily_returns)
        down_std = down_var ** 0.5
        sortino = (mean_ret / down_std * (annualization_factor ** 0.5)) if down_std > 0 else 0
    else:
        sortino = sharpe * 2 if sharpe > 0 else 0

    # Max Drawdown + Duration
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_duration = 0
    current_dd_duration = 0

    for pnl in pnls:
        equity += pnl
        if equity > peak:
            peak = equity
            current_dd_duration = 0
        else:
            current_dd_duration += 1
            max_dd_duration = max(max_dd_duration, current_dd_duration)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    # Calmar Ratio
    total_pnl = sum(pnls)
    calmar = total_pnl / max_dd if max_dd > 0 else (total_pnl if total_pnl > 0 else 0)

    # Monte Carlo: test if strategy returns are significantly different from zero
    # Null hypothesis: mean PnL = 0 (strategy has no edge)
    # Method: randomly flip signs of individual PnLs to destroy directional edge
    actual_pnl = sum(pnls)
    actual_mean = actual_pnl / n if n > 0 else 0

    # Permutation test: randomly flip signs of individual PnLs
    null_pnls = []
    for _ in range(monte_carlo_n):
        shuffled = [p * random.choice([-1, 1]) for p in pnls]
        null_pnls.append(sum(shuffled))

    null_pnls.sort()
    # p-value: fraction of null samples >= actual (one-tailed)
    p_value = sum(1 for p in null_pnls if p >= actual_pnl) / len(null_pnls)

    # Also compute bootstrap confidence interval for actual PnL
    bootstrap_pnls = []
    for _ in range(monte_carlo_n):
        sample = random.choices(pnls, k=n)
        bootstrap_pnls.append(sum(sample))
    bootstrap_pnls.sort()

    mc = {
        "p_value": round(p_value, 4),
        "actual_mean_pnl": round(actual_mean, 4),
        "median_pnl": round(bootstrap_pnls[len(bootstrap_pnls) // 2], 2),
        "pnl_5th": round(bootstrap_pnls[int(len(bootstrap_pnls) * 0.05)], 2),
        "pnl_95th": round(bootstrap_pnls[int(len(bootstrap_pnls) * 0.95)], 2),
    }

    # Regime Split
    regime_groups: dict[str, list[float]] = {}
    for t in trades:
        regime = _get(t, "regime", "unknown")
        if regime:
            regime_groups.setdefault(regime, []).append(_get(t, "pnl_pct"))

    regime_split = {}
    for regime, rpnls in regime_groups.items():
        wins = sum(1 for p in rpnls if p > 0)
        regime_split[regime] = {
            "trades": len(rpnls),
            "win_rate": round(wins / len(rpnls), 4) if rpnls else 0,
            "pnl": round(sum(rpnls), 2),
        }

    return {
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3),
        "calmar_ratio": round(calmar, 3),
        "max_dd_duration_days": max_dd_duration,
        "monte_carlo": mc,
        "regime_split": regime_split,
    }


def apply_fee_model(gross_pnl_pct: float, fee_cfg: dict) -> float:
    """Adjust gross PnL for trading costs (fees + spread + slippage).
    Costs per-leg, applied twice (entry + exit). All values from config.
    """
    fee = fee_cfg.get("base_fee_pct", 0.10)
    spread = fee_cfg.get("spread_pct", 0.05)
    slippage_mult = fee_cfg.get("slippage_multiplier", 1.0)
    cost_per_leg = fee + spread * slippage_mult
    total_cost = 2 * cost_per_leg
    return gross_pnl_pct - total_cost


# ---------------------------------------------------------------------------
# Signal generation from backtest scores
# ---------------------------------------------------------------------------

def generate_signals_for_asset(
    candles: list[dict],
    dim_scores: dict[int, dict],
    weights: dict[str, float],
    asset_cfg: dict,
    scoring_cfg: dict,
    start_idx: int = 60,
    abstain_bearish: float = 5.0,
    abstain_bullish: float = 6.0,
    learned_params: AssetLearnedParams | None = None,
    use_learned_targets: bool = True,
) -> list[Trade]:
    """Generate trades from dimension scores + weights.

    When learned_params is provided and use_learned_targets=True:
      - TP/SL distances come from learned params (data-derived, per-direction)
      - Abstain thresholds adjusted by direction confidence
      - No hardcoded TP/SL values anywhere

    When learned_params is None:
      - Falls back to S/R-based TP/SL calculation
    """
    trades = []
    asset_name = asset_cfg.get("name", "???")
    tech_cfg = scoring_cfg.get("agents", {}).get("technical", {})

    # Apply learned abstain adjustments (per-direction)
    if learned_params and use_learned_targets:
        eff_abstain_bullish = abstain_bullish + learned_params.bullish.abstain_adjustment
        eff_abstain_bearish = abstain_bearish + learned_params.bearish.abstain_adjustment
    else:
        eff_abstain_bullish = abstain_bullish
        eff_abstain_bearish = abstain_bearish

    day_keys = sorted(dim_scores.keys())

    for day_key in day_keys:
        actual_idx = day_key + start_idx
        if actual_idx >= len(candles) - 2:
            continue  # Need at least 2 future candles

        scores = dim_scores[day_key]

        # Compute composite
        composite = sum(
            scores.get(dim, 50.0) * weights.get(dim, 0.0)
            for dim in weights.keys()
        )
        composite = max(0.0, min(100.0, composite))

        # Direction + abstain check (with learned adjustments)
        if composite > 50 + eff_abstain_bullish:
            direction = "bullish"
        elif composite < 50 - eff_abstain_bearish:
            direction = "bearish"
        else:
            continue  # Neutral / abstain — no trade

        # Get current candle data
        current_candle = candles[actual_idx]
        entry_price = current_candle["close"]
        if entry_price <= 0:
            continue

        # --- TP/SL from learned params (data-driven) ---
        if learned_params and use_learned_targets:
            dp = learned_params.bullish if direction == "bullish" else learned_params.bearish

            if dp.optimal_tp_pct <= 0 or dp.optimal_sl_pct <= 0:
                continue  # No valid params learned for this direction

            if direction == "bullish":
                target_price = entry_price * (1 + dp.optimal_tp_pct / 100)
                stop_loss = entry_price * (1 - dp.optimal_sl_pct / 100)
            else:
                target_price = entry_price * (1 - dp.optimal_tp_pct / 100)
                stop_loss = entry_price * (1 + dp.optimal_sl_pct / 100)

            rr = dp.realized_rr
        else:
            # --- Fallback: S/R-based TP/SL via modifiers ---
            from scoring.modifiers import calculate_targets
            candle_slice = candles[:actual_idx + 1]
            tech_data = compute_technical_indicators(candle_slice, tech_cfg)
            if not tech_data:
                continue

            atr_14 = tech_data.get("atr_14", 0)
            if atr_14 <= 0:
                continue

            sl_mult = asset_cfg.get("sl_atr_multiplier", 1.5)
            targets_cfg = scoring_cfg.get("targets", {})
            sr_levels = {
                "ma7": tech_data.get("ma7", 0),
                "ma30": tech_data.get("ma30", 0),
                "bb_upper": tech_data.get("bb_upper", 0),
                "bb_lower": tech_data.get("bb_lower", 0),
                "swing_high": tech_data.get("swing_high", 0),
                "swing_low": tech_data.get("swing_low", 0),
            }

            tgt = calculate_targets(
                entry_price, composite, direction, atr_14, sl_mult,
                targets_cfg, sr_levels=sr_levels,
            )
            if not tgt:
                continue
            target_price = tgt.target_price
            stop_loss = tgt.stop_loss
            rr = tgt.risk_reward_ratio

        # Sanity checks
        if direction == "bullish" and (target_price <= entry_price or stop_loss >= entry_price):
            continue
        if direction == "bearish" and (target_price >= entry_price or stop_loss <= entry_price):
            continue

        # Confidence from composite distance
        dist = abs(composite - 50)
        confidence = "high" if dist > 20 else ("medium" if dist > 12 else "low")

        trade = Trade(
            asset=asset_name,
            date=current_candle["date"],
            direction=direction,
            composite=round(composite, 2),
            entry_price=round(entry_price, 2),
            target_price=round(target_price, 2),
            stop_loss=round(stop_loss, 2),
            risk_reward_ratio=round(rr, 2),
            confidence=confidence,
        )

        # Evaluate against future candles
        future = candles[actual_idx + 1: actual_idx + 3]  # Next 2 days (48h)
        trade = evaluate_trade(trade, future, max_days=2)
        trades.append(trade)

    return trades


# ---------------------------------------------------------------------------
# Full simulation
# ---------------------------------------------------------------------------

def compute_walk_forward_scores(
    candles: list[dict],
    macro_data: dict[str, list[dict]],
    fg_data: list[dict],
    scoring_cfg: dict,
    derivatives_data: dict[str, dict] | None = None,
    min_train: int = 60,
) -> tuple[dict[int, dict], dict[int, float], dict[int, float]]:
    """Walk-forward scoring: fit IC params ONLY on past data for each day.

    For day N, we fit IC params on days [0..N-1], then score day N.
    This eliminates look-ahead bias entirely.

    Returns: (dimension_scores, forward_returns_24h, forward_returns_48h)
    """
    from tools.fit_scoring import (
        fit_indicator_params, score_dimension_fitted,
        TECHNICAL_INDICATORS, MARKET_INDICATORS, DERIVATIVES_INDICATORS,
    )

    fg_by_date = {e["date"]: e["value"] for e in fg_data}
    macro_by_date: dict[str, dict[str, dict]] = {}
    for source, entries in macro_data.items():
        macro_by_date[source] = {e["date"]: e for e in entries}

    tech_cfg = scoring_cfg.get("agents", {}).get("technical", {})
    if derivatives_data is None:
        derivatives_data = {}

    # First: compute raw indicators for ALL days
    raw_indicators: dict[int, dict] = {}
    forward_returns_24h: dict[int, float] = {}
    forward_returns_48h: dict[int, float] = {}

    start_idx = min(60, len(candles) - 10)
    for idx in range(start_idx, len(candles)):
        candle_slice = candles[:idx + 1]
        current_date = candles[idx]["date"]
        current_close = candles[idx]["close"]

        if idx + 1 < len(candles):
            ret_24h = (candles[idx + 1]["close"] - current_close) / current_close * 100
        else:
            continue

        if idx + 2 < len(candles):
            ret_48h = (candles[idx + 2]["close"] - current_close) / current_close * 100
        else:
            ret_48h = ret_24h

        tech_data = compute_technical_indicators(candle_slice, tech_cfg)
        if not tech_data:
            continue

        day_key = idx - start_idx
        raw = dict(tech_data)
        raw["fear_greed"] = fg_by_date.get(current_date, 50)

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
        return {}, {}, {}

    # Second pass: compute series-based lead indicators
    from tools.indicators import calc_funding_accel, calc_oi_accel, calc_vol_price_divergence
    all_sorted = sorted(raw_indicators.keys())

    funding_series = [raw_indicators[d].get("funding_rate", 0.0) for d in all_sorted]
    oi_series = [raw_indicators[d].get("oi_change_pct", 0.0) for d in all_sorted]
    funding_accels = calc_funding_accel(funding_series)
    oi_accels = calc_oi_accel(oi_series)

    for i, day_key in enumerate(all_sorted):
        if i > 0 and i - 1 < len(funding_accels):
            raw_indicators[day_key]["funding_accel"] = funding_accels[i - 1]
        else:
            raw_indicators[day_key]["funding_accel"] = 0.0

        if i > 0 and i - 1 < len(oi_accels):
            raw_indicators[day_key]["oi_accel"] = oi_accels[i - 1]
        else:
            raw_indicators[day_key]["oi_accel"] = 0.0

        if i >= 5:
            window_keys = all_sorted[i - 4:i + 1]
            price_changes = [raw_indicators[wk].get("roc_7d", 0.0) for wk in window_keys]
            volumes = [raw_indicators[wk].get("volume_ratio", 1.0) for wk in window_keys]
            raw_indicators[day_key]["vol_price_div"] = calc_vol_price_divergence(price_changes, volumes)
        else:
            raw_indicators[day_key]["vol_price_div"] = 0.0

        # liq_density defaults to 0 in backtesting (live data only)
        raw_indicators[day_key].setdefault("liq_density", 0.0)

    # Walk-forward: for each day, fit on past data only
    all_day_keys = sorted(raw_indicators.keys())
    dimension_scores: dict[int, dict] = {}

    for i, day_key in enumerate(all_day_keys):
        # Need at least min_train days of history to fit
        if i < min_train:
            continue

        # Fit IC params using ONLY days [0..i-1] (past data)
        train_keys = all_day_keys[:i]
        train_fwd = [forward_returns_48h[d] for d in train_keys if d in forward_returns_48h]

        all_names = set()
        for d in train_keys:
            all_names.update(raw_indicators[d].keys())

        numeric_indicators = {}
        for name in all_names:
            values = []
            for d in train_keys:
                v = raw_indicators[d].get(name)
                if isinstance(v, (int, float)) and v == v:
                    values.append(v)
                else:
                    values.append(None)
            numeric_indicators[name] = values

        fitted_params = fit_indicator_params(numeric_indicators, train_fwd, min_obs=20)

        # Score today using params fitted on past data only
        raw = raw_indicators[day_key]
        scores: dict[str, float] = {}

        avail_tech = [n for n in TECHNICAL_INDICATORS if n in fitted_params]
        avail_market = [n for n in MARKET_INDICATORS if n in fitted_params]
        avail_deriv = [n for n in DERIVATIVES_INDICATORS if n in fitted_params]

        scores["technical"] = score_dimension_fitted(raw, fitted_params, avail_tech) if avail_tech else 50.0
        scores["market"] = score_dimension_fitted(raw, fitted_params, avail_market) if avail_market else 50.0
        scores["derivatives"] = score_dimension_fitted(raw, fitted_params, avail_deriv) if avail_deriv else 50.0

        dimension_scores[day_key] = scores

    return dimension_scores, forward_returns_24h, forward_returns_48h


def run_simulation(
    days: int = 180,
    assets: list[str] | None = None,
    capital: float = 10000.0,
    position_pct: float = 10.0,  # % of capital per trade
    db_path: str = DB_PATH,
    walk_forward: bool = True,
) -> dict[str, SimulationResult]:
    """Run the full trade simulation.

    1. Load data (same as backtest)
    2. Compute scores + weights (walk-forward: no look-ahead bias)
    3. Generate signals with TP/SL
    4. Evaluate each trade
    5. Compute PnL and statistics

    Args:
        walk_forward: If True, use walk-forward IC fitting (no look-ahead).
                      If False, use all-data fitting (faster but biased).
    """
    from tools.weight_optimizer import optimize_asset

    if not Path(db_path).exists():
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    asset_cfg = load_asset_config()
    all_assets = asset_cfg.get("assets", {})
    scoring_cfg = load_scoring_config()

    # Filter enabled
    if assets:
        enabled = {n: i for n, i in all_assets.items() if n in assets and i.get("enabled", False)}
    else:
        enabled = {n: i for n, i in all_assets.items() if i.get("enabled", False)}

    macro_data = load_macro_data(db_path)
    fg_data = load_fear_greed_data(db_path)

    # Abstain thresholds from config
    abstain_cfg = scoring_cfg.get("scoring", {}).get("abstain", {})
    abstain_bearish = abstain_cfg.get("bearish_min_distance", 5)
    abstain_bullish = abstain_cfg.get("bullish_min_distance", 6)

    results: dict[str, SimulationResult] = {}

    mode = "WALK-FORWARD (no look-ahead)" if walk_forward else "ALL-DATA (biased)"
    print(f"\nTRADE SIMULATION: {days} days, {len(enabled)} assets")
    print(f"Mode: {mode}")
    print(f"Starting capital: ${capital:,.0f}, Position size: {position_pct}%")
    print()

    for name, info in enabled.items():
        symbol = info["binance_symbol"]
        candles = load_candles(db_path, symbol)
        if not candles:
            continue
        if len(candles) > days:
            candles = candles[-days:]
        if len(candles) < 70:
            print(f"  {name}: Insufficient data ({len(candles)} candles), skipping")
            continue

        start_idx = min(60, len(candles) - 10)
        deriv_data = load_derivatives_data(db_path, symbol)

        if walk_forward:
            dim_scores, fwd_24h, fwd_48h = compute_walk_forward_scores(
                candles, macro_data, fg_data, scoring_cfg,
                derivatives_data=deriv_data,
                min_train=60,
            )
        else:
            dim_scores, fwd_24h, fwd_48h = compute_daily_scores(
                candles, macro_data, fg_data, scoring_cfg,
                start_idx=start_idx, derivatives_data=deriv_data,
            )

        if len(dim_scores) < 20:
            print(f"  {name}: Insufficient scored days, skipping")
            continue

        # Optimize weights (same as backtest)
        asset_config = {
            "noise_threshold": info.get("noise_threshold_pct", 2.0),
            "strong_threshold": info.get("strong_threshold_pct", 5.0),
        }
        # Estimate ATR as average absolute daily return
        avg_abs_ret = sum(abs(v) for v in fwd_24h.values()) / len(fwd_24h) if fwd_24h else 2.0

        opt_result = optimize_asset(
            name, dim_scores, fwd_24h, fwd_48h,
            noise_threshold=asset_config["noise_threshold"],
            strong_threshold=asset_config["strong_threshold"],
            atr_pct=avg_abs_ret,
        )
        weights = opt_result.get("weights", {"technical": 0.45, "market": 0.50, "derivatives": 0.05})

        # Learn TP/SL params from historical data (walk-forward: use only past data)
        # We learn from the training portion of data (first 60+ days)
        # and apply to the test portion — same as a real daily learning loop
        if walk_forward:
            # Learn from data available at the START of the test period
            train_candles = candles[:start_idx + 60]  # First ~120 days
            asset_learned = learn_asset_params(train_candles, timeframe_days=2, min_history=30)
            asset_learned.asset = name
        else:
            asset_learned = learn_asset_params(candles, timeframe_days=2, min_history=30)
            asset_learned.asset = name

        info_with_name = dict(info, name=name)
        trades = generate_signals_for_asset(
            candles, dim_scores, weights, info_with_name, scoring_cfg,
            start_idx=start_idx,
            abstain_bearish=abstain_bearish,
            abstain_bullish=abstain_bullish,
            learned_params=asset_learned,
            use_learned_targets=True,
        )

        # Compute stats
        result = SimulationResult(asset=name)
        result.total_trades = len(trades)
        result.tp_hits = sum(1 for t in trades if t.outcome == "tp_hit")
        result.sl_hits = sum(1 for t in trades if t.outcome == "sl_hit")
        result.expired_wins = sum(1 for t in trades if t.outcome == "expired_win")
        result.expired_losses = sum(1 for t in trades if t.outcome == "expired_loss")

        wins = result.tp_hits + result.expired_wins
        result.win_rate = wins / result.total_trades if result.total_trades > 0 else 0
        result.tp_hit_rate = result.tp_hits / result.total_trades if result.total_trades > 0 else 0

        # PnL
        gross_wins = sum(t.pnl_pct for t in trades if t.pnl_pct > 0)
        gross_losses = abs(sum(t.pnl_pct for t in trades if t.pnl_pct < 0))
        result.total_pnl_pct = sum(t.pnl_pct for t in trades)

        # Fee-adjusted PnL
        trading_cfg = scoring_cfg.get("trading", {})
        result.fee_adjusted_pnl = sum(apply_fee_model(t.pnl_pct, trading_cfg) for t in trades)

        result.avg_win_pct = gross_wins / wins if wins > 0 else 0
        result.avg_loss_pct = gross_losses / (result.total_trades - wins) if (result.total_trades - wins) > 0 else 0
        result.profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf') if gross_wins > 0 else 0

        # Max drawdown
        equity_curve = []
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            running += t.pnl_pct
            equity_curve.append(running)
            peak = max(peak, running)
            dd = peak - running
            max_dd = max(max_dd, dd)
        result.max_drawdown_pct = max_dd

        result.trades = trades
        results[name] = result

        # Print progress
        status = "profitable" if result.total_pnl_pct > 0 else "losing"
        print(f"  {name}: {result.total_trades} trades, {result.win_rate:.0%} win rate, "
              f"{result.total_pnl_pct:+.1f}% PnL ({status})")

    return results


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_simulation_results(results: dict[str, SimulationResult], capital: float = 10000.0) -> None:
    """Print formatted simulation results."""
    if not results:
        print("No simulation results.")
        return

    sep = "=" * 110
    print(f"\n{'TRADE SIMULATION RESULTS':^110}")
    print(sep)
    print(f"{'Asset':<8} {'Trades':>7} {'TP Hit':>7} {'SL Hit':>7} {'Exp W':>6} {'Exp L':>6} "
          f"{'WinRate':>8} {'PnL%':>8} {'AvgWin':>8} {'AvgLoss':>8} {'PF':>6} {'MaxDD':>8}")
    print("-" * 110)

    total_trades = 0
    total_tp = 0
    total_sl = 0
    total_pnl = 0.0
    all_trades = []

    for asset in sorted(results.keys()):
        r = results[asset]
        total_trades += r.total_trades
        total_tp += r.tp_hits
        total_sl += r.sl_hits
        total_pnl += r.total_pnl_pct
        all_trades.extend(r.trades)

        pf_str = f"{r.profit_factor:.1f}" if r.profit_factor < 100 else "inf"
        print(f"{asset:<8} {r.total_trades:>7} {r.tp_hits:>7} {r.sl_hits:>7} "
              f"{r.expired_wins:>6} {r.expired_losses:>6} "
              f"{r.win_rate:>7.0%} {r.total_pnl_pct:>+7.1f}% "
              f"{r.avg_win_pct:>+7.2f}% {r.avg_loss_pct:>7.2f}% "
              f"{pf_str:>6} {r.max_drawdown_pct:>7.1f}%")

    print("-" * 110)
    overall_wr = (total_tp + sum(r.expired_wins for r in results.values())) / total_trades if total_trades > 0 else 0
    print(f"{'TOTAL':<8} {total_trades:>7} {total_tp:>7} {total_sl:>7} "
          f"{'':>6} {'':>6} "
          f"{overall_wr:>7.0%} {total_pnl:>+7.1f}% "
          f"{'':>8} {'':>8} {'':>6} {'':>8}")
    print(sep)

    # Dollar returns
    dollar_pnl = capital * (total_pnl / 100)
    print(f"\nStarting capital: ${capital:,.0f}")
    print(f"Total PnL: {total_pnl:+.2f}% (${dollar_pnl:+,.0f})")
    print(f"Ending capital: ${capital + dollar_pnl:,.0f}")

    # Trade breakdown by direction
    bullish = [t for t in all_trades if t.direction == "bullish"]
    bearish = [t for t in all_trades if t.direction == "bearish"]
    if bullish:
        bull_wr = sum(1 for t in bullish if t.pnl_pct > 0) / len(bullish)
        bull_pnl = sum(t.pnl_pct for t in bullish)
        print(f"\nLONG trades: {len(bullish)}, Win rate: {bull_wr:.0%}, PnL: {bull_pnl:+.1f}%")
    if bearish:
        bear_wr = sum(1 for t in bearish if t.pnl_pct > 0) / len(bearish)
        bear_pnl = sum(t.pnl_pct for t in bearish)
        print(f"SHORT trades: {len(bearish)}, Win rate: {bear_wr:.0%}, PnL: {bear_pnl:+.1f}%")

    # Best/worst trades
    if all_trades:
        best = max(all_trades, key=lambda t: t.pnl_pct)
        worst = min(all_trades, key=lambda t: t.pnl_pct)
        print(f"\nBest trade: {best.asset} {best.direction} on {best.date} → {best.pnl_pct:+.2f}% ({best.outcome})")
        print(f"Worst trade: {worst.asset} {worst.direction} on {worst.date} → {worst.pnl_pct:+.2f}% ({worst.outcome})")

    # Monthly breakdown (last 30 days vs rest)
    all_trades_sorted = sorted(all_trades, key=lambda t: t.date)
    if len(all_trades_sorted) >= 10:
        # Find recent trades (last 30 entries or last 30 days)
        dates = sorted(set(t.date for t in all_trades_sorted))
        if len(dates) > 30:
            cutoff = dates[-30]
            recent = [t for t in all_trades_sorted if t.date >= cutoff]
            older = [t for t in all_trades_sorted if t.date < cutoff]
            if recent and older:
                recent_pnl = sum(t.pnl_pct for t in recent)
                older_pnl = sum(t.pnl_pct for t in older)
                recent_wr = sum(1 for t in recent if t.pnl_pct > 0) / len(recent)
                print(f"\nLast 30 days: {len(recent)} trades, WR: {recent_wr:.0%}, PnL: {recent_pnl:+.1f}%")
                print(f"Earlier: {len(older)} trades, PnL: {older_pnl:+.1f}%")


def export_trades_json(results: dict[str, SimulationResult], path: str) -> None:
    """Export all trades to JSON for analysis."""
    all_trades = []
    for asset in sorted(results.keys()):
        for t in results[asset].trades:
            all_trades.append({
                "asset": t.asset,
                "date": t.date,
                "direction": t.direction,
                "composite": t.composite,
                "entry": t.entry_price,
                "target": t.target_price,
                "stop_loss": t.stop_loss,
                "rr": t.risk_reward_ratio,
                "outcome": t.outcome,
                "exit_price": t.exit_price,
                "exit_date": t.exit_date,
                "pnl_pct": round(t.pnl_pct, 4),
                "days_held": t.days_held,
                "confidence": t.confidence,
            })
    with open(path, "w") as f:
        json.dump(all_trades, f, indent=2)
    print(f"\nExported {len(all_trades)} trades to {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="TP/SL Trade Simulator")
    parser.add_argument("--days", type=int, default=180, help="Days of data (default: 180)")
    parser.add_argument("--assets", type=str, help="Comma-separated assets")
    parser.add_argument("--capital", type=float, default=10000.0, help="Starting capital")
    parser.add_argument("--export", type=str, help="Export trades to JSON file")
    parser.add_argument("--db", type=str, default=DB_PATH, help="SQLite database path")
    parser.add_argument("--no-walk-forward", action="store_true",
                        help="Disable walk-forward (faster but biased)")
    args = parser.parse_args()

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")

    asset_list = [a.strip().upper() for a in args.assets.split(",")] if args.assets else None

    results = run_simulation(
        days=args.days,
        assets=asset_list,
        capital=args.capital,
        db_path=args.db,
        walk_forward=not args.no_walk_forward,
    )

    print_simulation_results(results, capital=args.capital)

    if args.export:
        export_trades_json(results, args.export)


if __name__ == "__main__":
    main()
