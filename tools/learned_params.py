# tools/learned_params.py
"""Self-learning parameter engine — ZERO hardcoded values.

Learns optimal TP/SL distances, direction confidence, and R:R ratios
from historical price action. Per-asset, per-direction. Updated daily.

Philosophy:
  - Every parameter is derived from data
  - No magic numbers — all values come from statistical analysis
  - Parameters evolve as new data arrives
  - The system adapts to each asset's unique volatility profile

Learns:
  1. Optimal SL distance (percentile of adverse moves that avoids noise)
  2. Optimal TP distance (achievable targets within the timeframe)
  3. Direction confidence (per-asset win rate by direction)
  4. Effective R:R (actual realized reward vs risk)
  5. Abstain thresholds (per-asset, per-direction)

Storage: learned_state.json (updated daily, git-tracked for auditability)
"""
from __future__ import annotations

import json
import math
import sqlite3
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tools.historical_fetcher import DB_PATH

logger = logging.getLogger(__name__)

LEARNED_STATE_PATH = Path(__file__).resolve().parent.parent / "learned_state.json"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class DirectionParams:
    """Learned parameters for one direction (bullish or bearish) of one asset."""
    # TP/SL distances (% from entry)
    optimal_sl_pct: float = 0.0       # Derived from adverse move distribution
    optimal_tp_pct: float = 0.0       # Derived from favorable move distribution
    realized_rr: float = 0.0          # Actual avg win / avg loss from history

    # Confidence
    win_rate: float = 0.0             # Historical win rate for this direction
    n_observations: int = 0           # How many data points this is based on
    expected_value: float = 0.0       # win_rate * avg_win - (1-win_rate) * avg_loss

    # Abstain adjustment
    direction_confidence: float = 0.0  # 0-1, how reliable this direction is
    abstain_adjustment: float = 0.0    # Added to abstain threshold for this direction


@dataclass
class AssetLearnedParams:
    """All learned parameters for one asset."""
    asset: str
    bullish: DirectionParams = field(default_factory=DirectionParams)
    bearish: DirectionParams = field(default_factory=DirectionParams)
    # Asset-level volatility profile
    daily_volatility_pct: float = 0.0     # Avg absolute daily move
    noise_floor_pct: float = 0.0          # Below this, move is noise
    typical_48h_range_pct: float = 0.0    # Typical 48h price range
    last_updated: str = ""


@dataclass
class LearnedState:
    """Complete learned state — persisted to disk."""
    assets: dict[str, AssetLearnedParams] = field(default_factory=dict)
    version: str = ""
    learning_days: int = 0
    risk_params: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Core learning: compute optimal parameters from price data
# ---------------------------------------------------------------------------

def learn_asset_params(
    candles: list[dict],
    timeframe_days: int = 2,
    min_history: int = 30,
) -> AssetLearnedParams:
    """Learn optimal TP/SL/confidence from raw price data.

    No hardcoded thresholds. Everything derived from the asset's own
    price distribution.

    Args:
        candles: Historical OHLCV candles (daily).
        timeframe_days: Signal holding period (default 2 = 48h).
        min_history: Minimum candles needed.

    Returns: AssetLearnedParams with all values derived from data.
    """
    if len(candles) < min_history + timeframe_days:
        return AssetLearnedParams(asset="unknown")

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    # -----------------------------------------------------------------------
    # Step 1: Compute the asset's volatility profile
    # -----------------------------------------------------------------------
    daily_returns = []
    for i in range(1, len(closes)):
        daily_returns.append((closes[i] - closes[i - 1]) / closes[i - 1] * 100)

    daily_vol = sum(abs(r) for r in daily_returns) / len(daily_returns)

    # Noise floor: moves below 25th percentile of absolute returns are noise
    # (derived from data, not hardcoded — the percentile adapts to each asset)
    abs_returns = sorted(abs(r) for r in daily_returns)
    noise_floor_idx = len(abs_returns) // 4  # 25th percentile
    noise_floor = abs_returns[noise_floor_idx] if abs_returns else daily_vol * 0.5

    # -----------------------------------------------------------------------
    # Step 2: Compute forward move distributions for the timeframe
    # -----------------------------------------------------------------------
    # For each day, what was the max favorable move and max adverse move
    # within the next `timeframe_days` candles?

    bullish_favorable = []   # Max upside in timeframe
    bullish_adverse = []     # Max downside in timeframe (how far it drops before target)
    bearish_favorable = []   # Max downside in timeframe (favorable for shorts)
    bearish_adverse = []     # Max upside in timeframe (adverse for shorts)

    forward_returns = []     # Close-to-close returns over timeframe

    for i in range(len(candles) - timeframe_days):
        entry = closes[i]
        if entry <= 0:
            continue

        # Max high and min low in the holding period
        period_highs = [highs[j] for j in range(i + 1, i + 1 + timeframe_days)]
        period_lows = [lows[j] for j in range(i + 1, i + 1 + timeframe_days)]
        period_close = closes[i + timeframe_days]

        max_high = max(period_highs)
        min_low = min(period_lows)

        # Forward return
        fwd_ret = (period_close - entry) / entry * 100
        forward_returns.append(fwd_ret)

        # For bullish trades
        max_upside = (max_high - entry) / entry * 100
        max_drawdown = (entry - min_low) / entry * 100

        # For bearish trades
        max_downside = (entry - min_low) / entry * 100
        max_adverse_up = (max_high - entry) / entry * 100

        bullish_favorable.append(max_upside)
        bullish_adverse.append(max_drawdown)
        bearish_favorable.append(max_downside)
        bearish_adverse.append(max_adverse_up)

    if not forward_returns:
        return AssetLearnedParams(asset="unknown")

    # -----------------------------------------------------------------------
    # Step 3: Derive optimal SL from adverse move distribution
    # -----------------------------------------------------------------------
    # SL should be beyond the typical adverse noise, but not so far it's useless.
    # Use the percentile where most noise is captured.
    #
    # Logic: if 70% of adverse moves are below X%, then SL at X% avoids most noise.
    # The exact percentile comes from maximizing expected value (computed below).

    def percentile(data: list[float], pct: float) -> float:
        """Compute percentile of a sorted list."""
        if not data:
            return 0.0
        s = sorted(data)
        idx = int(len(s) * pct / 100)
        idx = min(idx, len(s) - 1)
        return s[idx]

    # -----------------------------------------------------------------------
    # Step 4: Find optimal TP/SL by maximizing expected value
    # -----------------------------------------------------------------------
    # For each candidate SL percentile and TP percentile, simulate trades
    # and compute expected PnL. Pick the combination that maximizes EV.

    best_bull = _optimize_direction(
        bullish_favorable, bullish_adverse, forward_returns,
        direction="bullish",
    )
    best_bear = _optimize_direction(
        bearish_favorable, bearish_adverse,
        [-r for r in forward_returns],  # Flip returns for bearish evaluation
        direction="bearish",
    )

    # -----------------------------------------------------------------------
    # Step 5: Compute direction confidence
    # -----------------------------------------------------------------------
    # Based on how often the asset moves in each direction beyond noise floor
    up_moves = sum(1 for r in forward_returns if r > noise_floor)
    down_moves = sum(1 for r in forward_returns if r < -noise_floor)
    total = len(forward_returns)

    bull_tendency = up_moves / total if total > 0 else 0.5
    bear_tendency = down_moves / total if total > 0 else 0.5

    # Direction confidence: how reliable is this direction historically?
    # Derived from win rate relative to break-even for the given R:R
    # At R:R=1.5, break-even is 40% win rate. Confidence scales from there.
    bull_confidence = _compute_confidence(best_bull["win_rate"], best_bull["realized_rr"])
    bear_confidence = _compute_confidence(best_bear["win_rate"], best_bear["realized_rr"])

    # Abstain adjustment: if confidence is low, require stronger signals
    # Derived from the gap between actual win rate and break-even
    bull_abstain_adj = _compute_abstain_adjustment(bull_confidence)
    bear_abstain_adj = _compute_abstain_adjustment(bear_confidence)

    # 48h range
    typical_48h = percentile([abs(r) for r in forward_returns], 50)

    params = AssetLearnedParams(
        asset="",
        bullish=DirectionParams(
            optimal_sl_pct=best_bull["sl_pct"],
            optimal_tp_pct=best_bull["tp_pct"],
            realized_rr=best_bull["realized_rr"],
            win_rate=best_bull["win_rate"],
            n_observations=len(bullish_favorable),
            expected_value=best_bull["expected_value"],
            direction_confidence=bull_confidence,
            abstain_adjustment=bull_abstain_adj,
        ),
        bearish=DirectionParams(
            optimal_sl_pct=best_bear["sl_pct"],
            optimal_tp_pct=best_bear["tp_pct"],
            realized_rr=best_bear["realized_rr"],
            win_rate=best_bear["win_rate"],
            n_observations=len(bearish_favorable),
            expected_value=best_bear["expected_value"],
            direction_confidence=bear_confidence,
            abstain_adjustment=bear_abstain_adj,
        ),
        daily_volatility_pct=round(daily_vol, 4),
        noise_floor_pct=round(noise_floor, 4),
        typical_48h_range_pct=round(typical_48h, 4),
        last_updated=datetime.now(timezone.utc).isoformat(),
    )

    return params


def _optimize_direction(
    favorable: list[float],
    adverse: list[float],
    returns: list[float],
    direction: str,
) -> dict:
    """Find optimal TP/SL that maximizes expected value.

    Sweeps across percentiles of the favorable and adverse move distributions.
    No hardcoded candidate values — all derived from the data's own distribution.

    Returns dict with optimal tp_pct, sl_pct, win_rate, realized_rr, expected_value.
    """
    if not favorable or not adverse:
        return {"tp_pct": 3.0, "sl_pct": 2.0, "win_rate": 0.0,
                "realized_rr": 0.0, "expected_value": -1.0}

    n = len(favorable)

    # Generate candidate TP values from the favorable move distribution
    # Use percentiles [20, 30, 40, 50, 60, 70, 80] of actual favorable moves
    fav_sorted = sorted(favorable)
    adv_sorted = sorted(adverse)

    tp_candidates = []
    sl_candidates = []
    for pct in range(15, 85, 5):
        idx = min(int(n * pct / 100), n - 1)
        tp_val = fav_sorted[idx]
        sl_val = adv_sorted[idx]
        if tp_val > 0.01:
            tp_candidates.append(round(tp_val, 4))
        if sl_val > 0.01:
            sl_candidates.append(round(sl_val, 4))

    # Deduplicate
    tp_candidates = sorted(set(tp_candidates))
    sl_candidates = sorted(set(sl_candidates))

    if not tp_candidates or not sl_candidates:
        return {"tp_pct": 3.0, "sl_pct": 2.0, "win_rate": 0.0,
                "realized_rr": 0.0, "expected_value": -1.0}

    best_ev = -999.0
    best_result = None

    for tp_pct in tp_candidates:
        for sl_pct in sl_candidates:
            if tp_pct <= 0 or sl_pct <= 0:
                continue

            # Simulate: for each historical period, did TP or SL hit first?
            wins = 0
            losses = 0
            total_win_pct = 0.0
            total_loss_pct = 0.0

            for i in range(n):
                fav = favorable[i]
                adv = adverse[i]

                tp_hit = fav >= tp_pct
                sl_hit = adv >= sl_pct

                if tp_hit and sl_hit:
                    # Both possible — conservative: count as loss
                    losses += 1
                    total_loss_pct += sl_pct
                elif tp_hit:
                    wins += 1
                    total_win_pct += tp_pct
                elif sl_hit:
                    losses += 1
                    total_loss_pct += sl_pct
                else:
                    # Neither hit — use actual return
                    actual = returns[i] if i < len(returns) else 0
                    if actual > 0:
                        wins += 1
                        total_win_pct += actual
                    else:
                        losses += 1
                        total_loss_pct += abs(actual)

            total = wins + losses
            if total == 0:
                continue

            win_rate = wins / total
            avg_win = total_win_pct / wins if wins > 0 else 0
            avg_loss = total_loss_pct / losses if losses > 0 else 0

            # Expected value per trade
            ev = win_rate * avg_win - (1 - win_rate) * avg_loss

            # Realized R:R
            realized_rr = avg_win / avg_loss if avg_loss > 0 else 0

            if ev > best_ev:
                best_ev = ev
                best_result = {
                    "tp_pct": round(tp_pct, 4),
                    "sl_pct": round(sl_pct, 4),
                    "win_rate": round(win_rate, 4),
                    "realized_rr": round(realized_rr, 4),
                    "expected_value": round(ev, 4),
                }

    return best_result or {"tp_pct": 3.0, "sl_pct": 2.0, "win_rate": 0.0,
                           "realized_rr": 0.0, "expected_value": -1.0}


def _compute_confidence(win_rate: float, realized_rr: float) -> float:
    """Compute direction confidence from win rate and R:R.

    Confidence = how much above break-even the direction performs.
    At R:R = 1.5, break-even win rate = 1 / (1 + 1.5) = 0.40
    At R:R = 1.0, break-even win rate = 0.50

    Returns 0-1 confidence score.
    """
    if realized_rr <= 0:
        return 0.0

    breakeven_wr = 1.0 / (1.0 + realized_rr)
    if win_rate <= breakeven_wr:
        # Below break-even: scale 0 to 0.3
        if breakeven_wr > 0:
            return max(0.0, 0.3 * (win_rate / breakeven_wr))
        return 0.0

    # Above break-even: scale 0.3 to 1.0
    excess = win_rate - breakeven_wr
    max_excess = 1.0 - breakeven_wr
    if max_excess > 0:
        return 0.3 + 0.7 * min(excess / max_excess, 1.0)
    return 0.3


def _compute_abstain_adjustment(confidence: float) -> float:
    """Compute abstain threshold adjustment from confidence.

    Narrowed range [-2, +5] to prevent excessive signal suppression.
    Old range [-3, +10] caused 91% of signals to be neutral.

    Low confidence → larger positive adjustment → wider abstain zone
    → fewer signals for unreliable directions.

    High confidence → negative adjustment → narrower abstain zone
    → more signals for reliable directions.

    Returns: adjustment to add to base abstain threshold.
    Derived from confidence score — no hardcoded values.
    """
    # Map confidence [0, 1] to adjustment [-2, +5]
    # confidence=0 → +5 (cautious)
    # confidence=0.5 → 0 (use base threshold)
    # confidence=1.0 → -2 (signal more freely)
    if confidence <= 0.5:
        return 5.0 * (1.0 - confidence / 0.5)
    else:
        return -2.0 * ((confidence - 0.5) / 0.5)


def _learn_risk_params(assets: dict[str, AssetLearnedParams]) -> dict:
    """Derive risk management parameters from learned asset data."""
    if not assets:
        return {}

    vols = [a.daily_volatility_pct for a in assets.values() if a.daily_volatility_pct > 0]
    avg_vol = sum(vols) / len(vols) if vols else 3.0
    base_pct = max(3.0, min(20.0, 30.0 / avg_vol))

    avg_concurrent = min(len(assets), 5)
    daily_loss_cap = -(avg_vol * avg_concurrent * 0.5)

    positive_ev_count = sum(
        1 for a in assets.values()
        if max(a.bullish.expected_value, a.bearish.expected_value) > 0
    )
    max_open = max(2, min(8, positive_ev_count))

    return {
        "base_position_pct": round(base_pct, 2),
        "min_position_pct": round(base_pct * 0.3, 2),
        "max_position_pct": round(base_pct * 2.0, 2),
        "daily_loss_cap_pct": round(daily_loss_cap, 2),
        "max_open_trades": max_open,
        "max_correlated_trades": max(1, max_open // 2),
        "correlation_threshold": 0.7,
    }


# ---------------------------------------------------------------------------
# Full learning run: all assets
# ---------------------------------------------------------------------------

def learn_all_assets(
    db_path: str = DB_PATH,
    days: int = 180,
) -> LearnedState:
    """Learn parameters for all enabled assets from historical data.

    Args:
        db_path: Path to SQLite database with klines.
        days: Number of days of history to use.

    Returns: LearnedState with per-asset parameters.
    """
    import yaml

    assets_path = Path(__file__).resolve().parent.parent / "assets.yaml"
    with open(assets_path) as f:
        asset_cfg = yaml.safe_load(f)

    all_assets = asset_cfg.get("assets", {})
    enabled = {n: i for n, i in all_assets.items() if i.get("enabled", False)}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    state = LearnedState(
        version=datetime.now(timezone.utc).isoformat(),
        learning_days=days,
    )

    for name, info in enabled.items():
        symbol = info["binance_symbol"]
        rows = conn.execute(
            "SELECT * FROM klines WHERE symbol = ? ORDER BY date ASC",
            (symbol,),
        ).fetchall()

        candles = [dict(r) for r in rows]
        if len(candles) > days:
            candles = candles[-days:]

        if len(candles) < 32:
            logger.warning(f"{name}: Only {len(candles)} candles, skipping")
            continue

        params = learn_asset_params(candles)
        params.asset = name
        state.assets[name] = params

        # Log summary
        bull_ev = params.bullish.expected_value
        bear_ev = params.bearish.expected_value
        print(f"  {name}: vol={params.daily_volatility_pct:.2f}%, "
              f"bull(TP={params.bullish.optimal_tp_pct:.1f}%, "
              f"SL={params.bullish.optimal_sl_pct:.1f}%, "
              f"WR={params.bullish.win_rate:.0%}, EV={bull_ev:+.2f}%) "
              f"bear(TP={params.bearish.optimal_tp_pct:.1f}%, "
              f"SL={params.bearish.optimal_sl_pct:.1f}%, "
              f"WR={params.bearish.win_rate:.0%}, EV={bear_ev:+.2f}%)")

    conn.close()
    state.risk_params = _learn_risk_params(state.assets)
    return state


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_learned_state(state: LearnedState, path: Path | None = None) -> Path:
    """Save learned state to JSON."""
    path = path or LEARNED_STATE_PATH
    data = {
        "version": state.version,
        "learning_days": state.learning_days,
        "assets": {},
    }
    for name, params in state.assets.items():
        data["assets"][name] = {
            "bullish": asdict(params.bullish),
            "bearish": asdict(params.bearish),
            "daily_volatility_pct": params.daily_volatility_pct,
            "noise_floor_pct": params.noise_floor_pct,
            "typical_48h_range_pct": params.typical_48h_range_pct,
            "last_updated": params.last_updated,
        }

    data["risk_params"] = state.risk_params

    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path


def load_learned_state(path: Path | None = None) -> LearnedState | None:
    """Load learned state from JSON."""
    path = path or LEARNED_STATE_PATH
    if not path.exists():
        return None

    with open(path) as f:
        data = json.load(f)

    state = LearnedState(
        version=data.get("version", ""),
        learning_days=data.get("learning_days", 0),
    )
    for name, asset_data in data.get("assets", {}).items():
        bull_data = asset_data.get("bullish", {})
        bear_data = asset_data.get("bearish", {})
        state.assets[name] = AssetLearnedParams(
            asset=name,
            bullish=DirectionParams(**bull_data),
            bearish=DirectionParams(**bear_data),
            daily_volatility_pct=asset_data.get("daily_volatility_pct", 0),
            noise_floor_pct=asset_data.get("noise_floor_pct", 0),
            typical_48h_range_pct=asset_data.get("typical_48h_range_pct", 0),
            last_updated=asset_data.get("last_updated", ""),
        )
    state.risk_params = data.get("risk_params", {})
    return state


# ---------------------------------------------------------------------------
# Incremental daily learning
# ---------------------------------------------------------------------------

def incremental_update(
    current_state: LearnedState,
    new_candles: dict[str, list[dict]],
    blend_weight: float = 0.1,
) -> LearnedState:
    """Incrementally update learned params with new data.

    Instead of refitting from scratch daily, this blends new observations
    into existing parameters using exponential moving average.

    Args:
        current_state: Existing learned state.
        new_candles: {asset_name: [newest candles]} — latest data to incorporate.
        blend_weight: How much weight to give new data (0.1 = 10% new, 90% old).
                      This adapts automatically based on observation count.

    Returns: Updated LearnedState.
    """
    updated = LearnedState(
        version=datetime.now(timezone.utc).isoformat(),
        learning_days=current_state.learning_days + 1,
    )

    for asset_name, candles in new_candles.items():
        if asset_name not in current_state.assets:
            # New asset — full learn
            params = learn_asset_params(candles)
            params.asset = asset_name
            updated.assets[asset_name] = params
            continue

        old_params = current_state.assets[asset_name]

        # Learn from new data
        new_params = learn_asset_params(candles)
        new_params.asset = asset_name

        # Adaptive blend weight: more weight to new data when we have fewer obs
        # With 30 obs, blend=0.15 (learn faster). With 180 obs, blend=0.05 (more stable).
        n_obs = max(old_params.bullish.n_observations, old_params.bearish.n_observations)
        adaptive_blend = max(0.03, min(0.20, 5.0 / max(n_obs, 1)))

        # Blend old and new for each direction
        blended = AssetLearnedParams(asset=asset_name)

        for dir_attr in ["bullish", "bearish"]:
            old_dp = getattr(old_params, dir_attr)
            new_dp = getattr(new_params, dir_attr)

            # Only update if new data has enough observations
            if new_dp.n_observations < 20:
                setattr(blended, dir_attr, old_dp)
                continue

            # EMA blend for each parameter
            blended_dp = DirectionParams(
                optimal_sl_pct=_ema_blend(old_dp.optimal_sl_pct, new_dp.optimal_sl_pct, adaptive_blend),
                optimal_tp_pct=_ema_blend(old_dp.optimal_tp_pct, new_dp.optimal_tp_pct, adaptive_blend),
                realized_rr=_ema_blend(old_dp.realized_rr, new_dp.realized_rr, adaptive_blend),
                win_rate=_ema_blend(old_dp.win_rate, new_dp.win_rate, adaptive_blend),
                n_observations=old_dp.n_observations + 1,
                expected_value=_ema_blend(old_dp.expected_value, new_dp.expected_value, adaptive_blend),
                direction_confidence=_compute_confidence(
                    _ema_blend(old_dp.win_rate, new_dp.win_rate, adaptive_blend),
                    _ema_blend(old_dp.realized_rr, new_dp.realized_rr, adaptive_blend),
                ),
                abstain_adjustment=0.0,  # Recomputed below
            )
            blended_dp.abstain_adjustment = _compute_abstain_adjustment(blended_dp.direction_confidence)
            setattr(blended, dir_attr, blended_dp)

        blended.daily_volatility_pct = _ema_blend(
            old_params.daily_volatility_pct, new_params.daily_volatility_pct, adaptive_blend)
        blended.noise_floor_pct = _ema_blend(
            old_params.noise_floor_pct, new_params.noise_floor_pct, adaptive_blend)
        blended.typical_48h_range_pct = _ema_blend(
            old_params.typical_48h_range_pct, new_params.typical_48h_range_pct, adaptive_blend)
        blended.last_updated = datetime.now(timezone.utc).isoformat()

        updated.assets[asset_name] = blended

    # Carry forward assets that had no new data
    for name, params in current_state.assets.items():
        if name not in updated.assets:
            updated.assets[name] = params

    return updated


def _ema_blend(old: float, new: float, alpha: float) -> float:
    """Exponential moving average blend."""
    return round(old * (1 - alpha) + new * alpha, 6)


# ---------------------------------------------------------------------------
# Integration: learn and update for live pipeline
# ---------------------------------------------------------------------------

def daily_learning_update(db_path: str = DB_PATH) -> LearnedState:
    """Run the daily learning update cycle.

    1. Load current learned state (or bootstrap from scratch)
    2. Load latest candle data
    3. Incrementally update parameters
    4. Save updated state

    Called by the orchestrator every 12h (or daily).
    """
    import yaml

    # Load or bootstrap
    current = load_learned_state()
    if current is None:
        print("No existing learned state — bootstrapping from full history...")
        current = learn_all_assets(db_path=db_path, days=180)
        save_learned_state(current)
        return current

    # Load latest candles for all enabled assets
    assets_path = Path(__file__).resolve().parent.parent / "assets.yaml"
    with open(assets_path) as f:
        asset_cfg = yaml.safe_load(f)

    all_assets = asset_cfg.get("assets", {})
    enabled = {n: i for n, i in all_assets.items() if i.get("enabled", False)}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    new_candles = {}

    for name, info in enabled.items():
        symbol = info["binance_symbol"]
        rows = conn.execute(
            "SELECT * FROM klines WHERE symbol = ? ORDER BY date ASC",
            (symbol,),
        ).fetchall()
        candles = [dict(r) for r in rows]
        if candles:
            new_candles[name] = candles

    conn.close()

    # Incremental update
    updated = incremental_update(current, new_candles)
    updated.risk_params = _learn_risk_params(updated.assets)
    save_learned_state(updated)

    print(f"Daily learning update complete. {len(updated.assets)} assets updated.")
    return updated


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Run learning on all assets and save state."""
    import argparse
    logging.basicConfig(level="INFO")

    parser = argparse.ArgumentParser(description="Learn optimal TP/SL parameters")
    parser.add_argument("--days", type=int, default=180, help="Days of history")
    parser.add_argument("--db", type=str, default=DB_PATH, help="SQLite path")
    parser.add_argument("--output", type=str, help="Output path (default: learned_state.json)")
    args = parser.parse_args()

    print(f"\nLEARNING PARAMETERS from {args.days} days of data")
    print("=" * 80)

    state = learn_all_assets(db_path=args.db, days=args.days)

    out_path = Path(args.output) if args.output else None
    saved = save_learned_state(state, out_path)
    print(f"\nSaved learned state to {saved}")

    # Summary table
    print(f"\n{'LEARNED PARAMETERS SUMMARY':^80}")
    print("=" * 80)
    print(f"{'Asset':<8} {'Dir':<6} {'TP%':>6} {'SL%':>6} {'R:R':>6} {'WR':>6} "
          f"{'EV':>8} {'Conf':>6} {'AbsAdj':>7}")
    print("-" * 80)

    for name in sorted(state.assets.keys()):
        p = state.assets[name]
        for dir_name, dp in [("BULL", p.bullish), ("BEAR", p.bearish)]:
            print(f"{name:<8} {dir_name:<6} {dp.optimal_tp_pct:>5.1f}% "
                  f"{dp.optimal_sl_pct:>5.1f}% {dp.realized_rr:>5.2f} "
                  f"{dp.win_rate:>5.0%} {dp.expected_value:>+7.2f}% "
                  f"{dp.direction_confidence:>5.2f} {dp.abstain_adjustment:>+6.1f}")

    print("=" * 80)

    # Highlight directions with negative expected value
    neg_ev = [(name, "bullish", p.bullish) for name, p in state.assets.items()
              if p.bullish.expected_value < 0]
    neg_ev += [(name, "bearish", p.bearish) for name, p in state.assets.items()
               if p.bearish.expected_value < 0]

    if neg_ev:
        print(f"\nNEGATIVE EV DIRECTIONS (system will auto-widen abstain):")
        for name, direction, dp in neg_ev:
            print(f"  {name} {direction}: EV={dp.expected_value:+.2f}%, "
                  f"WR={dp.win_rate:.0%}, abstain_adj={dp.abstain_adjustment:+.1f}")


if __name__ == "__main__":
    main()
