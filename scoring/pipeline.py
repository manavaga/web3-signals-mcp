# scoring/pipeline.py
"""7-step scoring pipeline orchestrator.

Steps:
1. Score all dimensions independently (0-100)
2. Detect market regime (trending/ranging)
3. Select direction-aware weights + apply regime shifts + tier redistribution
4. Compute weighted composite
5. Check abstain (asymmetric, regime-adjusted)
6. Calculate TP/SL targets (for directional signals)
7. Assign label

Pure function: inputs in, signals out. No side effects.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from scoring.types import DimensionScore, RegimeContext, Signal
from scoring.config import AppConfig, AssetsConfig
from scoring.dimensions import (
    score_technical, score_derivatives, score_market,
    detect_data_tier
)
from scoring.modifiers import (
    detect_regime, select_weights, apply_regime_shifts,
    apply_tier_redistribution, check_abstain, assign_label,
    calculate_targets
)
from tools.learned_params import load_learned_state

ALL_DIMENSIONS = ["technical", "derivatives", "market"]

# Cache per-asset weights from backtest baseline (reload every 30 min)
_baseline_cache: dict = {"data": None, "timestamp": 0.0,
                         "path": Path(__file__).parent.parent / "backtest_baseline.json"}

# Cache fitted IC params (reload every 30 min)
_fitted_cache: dict = {"data": None, "timestamp": 0.0}

# Cache learned params (reload every 30 min)
_learned_cache: dict = {"data": None, "timestamp": 0.0}


def _load_learned_params():
    """Load learned params with 30-min cache."""
    now = time.time()
    if _learned_cache["data"] is not None and (now - _learned_cache["timestamp"]) < 1800:
        return _learned_cache["data"]
    state = load_learned_state()
    if state:
        _learned_cache["data"] = state
        _learned_cache["timestamp"] = now
    return state


def _load_per_asset_weights(path_override: Path | None = None) -> dict:
    """Load per-asset weights from backtest baseline, with caching.

    Only returns weights for assets with 'high' or 'medium' confidence.
    Caches for 30 minutes to avoid repeated disk reads.
    """
    now = time.time()
    cache = _baseline_cache
    if cache["data"] is not None and (now - cache["timestamp"]) < 1800 and path_override is None:
        return cache["data"]

    path = path_override or cache["path"]
    if not path.exists():
        return {}

    try:
        baseline = json.loads(path.read_text())
        per_asset: dict[str, dict[str, float]] = {}
        for asset, data in baseline.get("assets", {}).items():
            confidence = data.get("confidence", "insufficient")
            if confidence in ("high", "medium"):
                weights = data.get("weights", {})
                if weights:
                    per_asset[asset] = weights
        if path_override is None:
            cache["data"] = per_asset
            cache["timestamp"] = now
        return per_asset
    except Exception:
        return {}


def _load_fitted_params() -> dict | None:
    """Load fitted IC params from backtest baseline, with 30-min cache."""
    now = time.time()
    if _fitted_cache["data"] is not None and (now - _fitted_cache["timestamp"]) < 1800:
        return _fitted_cache["data"]

    path = _baseline_cache["path"]
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
        fitted = data.get("fitted_params")
        if fitted:
            _fitted_cache["data"] = fitted
            _fitted_cache["timestamp"] = now
        return fitted
    except Exception:
        return None


SCORE_FNS = {
    "technical": score_technical,
    "derivatives": score_derivatives,
    "market": score_market,
}


def fuse_signals(agent_data: dict, cfg: AppConfig, assets_cfg: AssetsConfig,
                 prev_scores: dict[str, float] | None = None) -> dict[str, Signal]:
    """Run the 7-step pipeline for all enabled assets."""
    enabled = assets_cfg.enabled_assets()
    signals: dict[str, Signal] = {}

    # Extract BTC data for regime detection
    btc_tech = (agent_data.get("technical") or {}).get("BTC", {})
    btc_price = btc_tech.get("price", 0)
    btc_ma30 = btc_tech.get("ma30", btc_price)
    btc_ma7 = btc_tech.get("ma7", btc_price)
    btc_adx = btc_tech.get("adx_14", 25.0)
    btc_market = (agent_data.get("market") or {}).get("BTC", {})
    fg_value = btc_market.get("fear_greed", 50)

    # Step 2: Detect regime (once, applies to all assets)
    regime = detect_regime(
        btc_price=btc_price, btc_ma30=btc_ma30, fg_value=fg_value,
        fg_thresholds=cfg.regime.fg_thresholds.model_dump(),
        trending_threshold=cfg.regime.trending_threshold,
        ranging_threshold=cfg.regime.ranging_threshold,
        btc_adx=btc_adx, btc_ma7=btc_ma7,
        adx_trending=cfg.regime.adx_trending_threshold,
        adx_ranging=cfg.regime.adx_ranging_threshold,
    )

    # Load fitted IC params (if backtest_baseline.json has them)
    fitted = _load_fitted_params()

    # --- Step 1: Score all dimensions for all assets ---
    all_dimensions: dict[str, dict[str, DimensionScore]] = {}
    for asset in enabled:
        dimensions: dict[str, DimensionScore] = {}
        for dim in ALL_DIMENSIONS:
            dim_data = (agent_data.get(dim) or {}).get(asset)
            agent_cfg = getattr(cfg.agents, dim, None)
            dim_cfg = agent_cfg.model_dump() if agent_cfg else {}
            if dim == "technical":
                dimensions[dim] = SCORE_FNS[dim](dim_data, dim_cfg, regime=regime.regime,
                                                  fitted_params=fitted)
            else:
                dimensions[dim] = SCORE_FNS[dim](dim_data, dim_cfg, fitted_params=fitted)
        all_dimensions[asset] = dimensions

    # --- Step 1b: Compute relative features (asset vs BTC) ---
    btc_tech_data = (agent_data.get("technical") or {}).get("BTC", {})
    btc_deriv_data = (agent_data.get("derivatives") or {}).get("BTC", {})
    has_btc_ref = bool(btc_tech_data)

    relative_metadata: dict[str, dict] = {}  # asset -> relative features

    if has_btc_ref:
        btc_rsi = btc_tech_data.get("rsi_14", 50.0)
        btc_roc = btc_tech_data.get("roc_7d", 0.0)
        btc_funding = btc_deriv_data.get("funding_rate", 0.0)

        for asset in enabled:
            if asset == "BTC":
                continue

            asset_tech_data = (agent_data.get("technical") or {}).get(asset, {})
            asset_deriv_data = (agent_data.get("derivatives") or {}).get(asset, {})

            relative_momentum = asset_tech_data.get("rsi_14", 50.0) - btc_rsi
            relative_strength = asset_tech_data.get("roc_7d", 0.0) - btc_roc
            relative_funding = asset_deriv_data.get("funding_rate", 0.0) - btc_funding

            # Adjust technical dimension score by relative momentum
            # Max adjustment: +/-5 points
            rel_adjustment = min(max(relative_momentum * 0.15, -5), 5)

            old_dim = all_dimensions[asset]["technical"]
            new_tech_score = max(0.0, min(100.0, old_dim.score + rel_adjustment))
            all_dimensions[asset]["technical"] = DimensionScore(
                name=old_dim.name,
                score=new_tech_score,
                detail=old_dim.detail,
                tier=old_dim.tier,
            )

            relative_metadata[asset] = {
                "relative_momentum": relative_momentum,
                "relative_strength": relative_strength,
                "relative_funding": relative_funding,
                "relative_tech_adjustment": round(rel_adjustment, 4),
            }

    # Load per-asset weights from backtest baseline (if available)
    per_asset_weights = _load_per_asset_weights()

    for asset in enabled:
        asset_entry = assets_cfg.get(asset)
        dimensions = all_dimensions[asset]

        # Compute raw average for weight selection (exclude zero-weight dimensions)
        active_dims = [ds for dim, ds in dimensions.items()
                       if cfg.scoring.weights_default.get(dim, 0) > 0]
        raw_avg = (sum(ds.score for ds in active_dims) / len(active_dims)) if active_dims else 50.0

        # Step 3a: Select weights
        # Priority: per-asset backtest weights > per-tier weights > config weights
        if asset in per_asset_weights:
            weights = dict(per_asset_weights[asset])
        else:
            tier_weights = None
            if cfg.scoring.per_tier_weights:
                for tier_name, tw in cfg.scoring.per_tier_weights.items():
                    if asset in tw.assets:
                        tier_weights = tw
                        break

            if tier_weights and raw_avg > 50:
                weights = dict(tier_weights.weights_bullish)
            elif tier_weights and raw_avg < 50:
                weights = dict(tier_weights.weights_bearish)
            else:
                weights = select_weights(
                    raw_avg,
                    cfg.scoring.weights_default,
                    cfg.scoring.weights_bullish,
                    cfg.scoring.weights_bearish,
                )

        # Step 3b: Apply regime shifts
        regime_shifts = cfg.regime.weight_shifts.get(regime.regime, {})
        if regime_shifts:
            weights = apply_regime_shifts(weights, regime_shifts)

        # Step 3c: Tier redistribution
        tiers = {dim: ds.tier for dim, ds in dimensions.items()}
        weights = apply_tier_redistribution(weights, tiers, cfg.scoring.tier_multipliers)

        # Step 4: Compute weighted composite
        composite = sum(dimensions[dim].score * weights.get(dim, 0) for dim in ALL_DIMENSIONS)
        composite = max(0.0, min(100.0, composite))

        # Step 4b: Soft-dampen bullish signals in trending_down regime
        # Data shows LONG trades in trending_down: 43% win rate, -31% PnL
        # Pull bullish composites toward 50 — reduces conviction without killing signal
        if regime.regime == "trending_down" and composite > 50.0:
            dampen_factor = regime_shifts.get("bullish_dampen", 0.5) if regime_shifts else 0.5
            composite = 50.0 + (composite - 50.0) * dampen_factor

        # Step 5: Check abstain (with learned per-asset, per-direction adjustments)
        learned_state = _load_learned_params()
        learned_asset = learned_state.assets.get(asset) if learned_state else None

        regime_mult = cfg.regime.abstain_multiplier.get(regime.regime, 1.0)
        base_bearish = cfg.scoring.abstain.bearish_min_distance
        base_bullish = cfg.scoring.abstain.bullish_min_distance

        # Apply learned direction confidence adjustments
        if learned_asset:
            eff_bearish = base_bearish + learned_asset.bearish.abstain_adjustment
            eff_bullish = base_bullish + learned_asset.bullish.abstain_adjustment
        else:
            eff_bearish = base_bearish
            eff_bullish = base_bullish

        abstained = check_abstain(
            composite, eff_bearish, eff_bullish, regime_mult,
        )

        # Step 7: Assign label
        if abstained:
            label = "INSUFFICIENT EDGE"
            direction = "neutral"
        else:
            labels_list = [{"name": l.name, "min_score": l.min_score} for l in cfg.scoring.labels]
            label, direction = assign_label(composite, labels_list)

        # Step 5b: Suppress directions with proven negative EV
        if not abstained and direction != "neutral" and learned_asset:
            dp = learned_asset.bullish if direction == "bullish" else learned_asset.bearish
            min_samples = cfg.learning.min_signals_per_asset
            if (dp.n_observations >= min_samples
                    and dp.expected_value < 0
                    and dp.win_rate < 0.45):
                abstained = True
                label = "INSUFFICIENT EDGE"
                direction = "neutral"

        # Step 6: Calculate targets (using learned params when available)
        targets = None
        if not abstained and direction != "neutral":
            asset_tech = (agent_data.get("technical") or {}).get(asset, {})
            entry_price = asset_tech.get("price", 0)

            if entry_price > 0 and learned_asset:
                # Use learned TP/SL distances (data-derived, per-direction)
                dp = learned_asset.bullish if direction == "bullish" else learned_asset.bearish
                if dp.optimal_tp_pct > 0 and dp.optimal_sl_pct > 0:
                    from scoring.types import TargetLevels
                    if direction == "bullish":
                        tp = entry_price * (1 + dp.optimal_tp_pct / 100)
                        sl = entry_price * (1 - dp.optimal_sl_pct / 100)
                    else:
                        tp = entry_price * (1 - dp.optimal_tp_pct / 100)
                        sl = entry_price * (1 + dp.optimal_sl_pct / 100)

                    reward = abs(tp - entry_price)
                    risk = abs(entry_price - sl)
                    rr = reward / risk if risk > 0 else 0

                    score_dist = abs(composite - 50)
                    conf = "high" if score_dist > 20 else ("medium" if score_dist > 12 else "low")
                    predicted_pct = (tp - entry_price) / entry_price * 100

                    targets = TargetLevels(
                        entry_price=round(entry_price, 2),
                        target_price=round(tp, 2),
                        stop_loss=round(sl, 2),
                        risk_reward_ratio=round(rr, 2),
                        predicted_move_pct=round(predicted_pct, 2),
                        confidence=conf,
                        timeframe_hours=cfg.targets.timeframe_hours,
                    )

            # Fallback to S/R-based targets if no learned params
            if targets is None and entry_price > 0:
                atr_14 = asset_tech.get("atr_14", 0)
                if atr_14 > 0:
                    sr_levels = {
                        "ma7": asset_tech.get("ma7", 0),
                        "ma30": asset_tech.get("ma30", 0),
                        "bb_upper": asset_tech.get("bb_upper", 0),
                        "bb_lower": asset_tech.get("bb_lower", 0),
                        "swing_high": asset_tech.get("swing_high", 0),
                        "swing_low": asset_tech.get("swing_low", 0),
                    }
                    targets = calculate_targets(
                        entry_price=entry_price,
                        composite=composite,
                        direction=direction,
                        atr_14=atr_14,
                        sl_multiplier=asset_entry.sl_atr_multiplier,
                        cfg=cfg.targets.model_dump(),
                        sr_levels=sr_levels,
                    )

        # Momentum
        prev = (prev_scores or {}).get(asset)
        if prev is not None:
            delta = composite - prev
            if delta > cfg.scoring.momentum_threshold:
                momentum = "improving"
            elif delta < -cfg.scoring.momentum_threshold:
                momentum = "degrading"
            else:
                momentum = "stable"
        else:
            momentum = "stable"

        signals[asset] = Signal(
            asset=asset,
            composite=round(composite, 2),
            label=label,
            direction=direction,
            dimensions=dimensions,
            weights_used={k: round(v, 4) for k, v in weights.items()},
            regime=regime,
            targets=targets,
            momentum=momentum,
            abstained=abstained,
            metadata=relative_metadata.get(asset, {}),
        )

    return signals
