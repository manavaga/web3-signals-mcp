from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from shared.profile_loader import load_profile
from shared.storage import Storage


def _fg_regime(fg_value: Optional[float]) -> str:
    """Classify Fear & Greed index into a regime label."""
    if fg_value is None:
        return "unknown"
    if fg_value <= 20:
        return "extreme_fear"
    if fg_value <= 40:
        return "fear"
    if fg_value <= 60:
        return "neutral"
    if fg_value <= 80:
        return "greed"
    return "extreme_greed"


class SignalFusion:
    """
    Fuses 5 agent outputs into composite scored signals per asset.

    All scoring rules, weights, labels, and thresholds live in the YAML profile.
    This engine contains zero domain logic — only generic arithmetic driven by config.
    """

    def __init__(self, profile_path: str | None = None, db_path: str = "signals.db") -> None:
        default = Path(__file__).resolve().parent / "profiles" / "default.yaml"
        self.profile = load_profile(Path(profile_path) if profile_path else default)
        all_assets = [a.upper() for a in self.profile.get("assets", [])]
        # Phase A2: Asset blacklist — filter out anti-predictive assets
        blacklist_cfg = self.profile.get("asset_blacklist", {})
        if blacklist_cfg.get("enabled", False):
            blacklisted = {a.upper() for a in blacklist_cfg.get("assets", [])}
            self.assets: List[str] = [a for a in all_assets if a not in blacklisted]
        else:
            self.assets: List[str] = all_assets
        self.store = Storage(db_path)
        self._target_calculator = None  # lazy-loaded TargetCalculator

        # Config versioning: SHA256 hash of the scoring YAML for signal attribution
        yaml_path = Path(profile_path) if profile_path else default
        try:
            self.config_hash = hashlib.sha256(yaml_path.read_bytes()).hexdigest()[:12]
        except Exception:
            self.config_hash = "unknown"

    def fuse(self) -> Dict[str, Any]:
        """Main entry: load latest agent data, score, label, summarise."""
        start = time.perf_counter()
        errors: List[str] = []

        # Load latest agent snapshots
        agent_names = self.profile.get("agent_names", {})
        raw: Dict[str, Optional[Dict[str, Any]]] = {}
        for role, name in agent_names.items():
            snapshot = self.store.load_latest(name)
            raw[role] = snapshot
            if snapshot is None:
                errors.append(f"{role}: no data in storage")

        # Score each asset across all dimensions
        # Weight selection: asymmetric (direction-aware) > learned > flat YAML
        asym_cfg = self.profile.get("weights_asymmetric", {})
        asym_enabled = asym_cfg.get("enabled", False)
        weights_default = asym_cfg.get("default", self.profile.get("weights", {}))
        weights_bullish = asym_cfg.get("bullish", weights_default)
        weights_bearish = asym_cfg.get("bearish", weights_default)

        if not asym_enabled:
            # Fall back to flat weights (legacy behaviour)
            weights_default = self.profile.get("weights", {})
            weights_bullish = weights_default
            weights_bearish = weights_default

        # Self-learning optimizer can override the default set
        learning_cfg = self.profile.get("learning", {})
        per_asset_learned: Optional[Dict[str, Dict[str, float]]] = None
        if learning_cfg.get("enabled", False):
            try:
                from signal_fusion.optimizer import WeightOptimizer
                optimizer = WeightOptimizer(self.store, self.profile)
                learned = optimizer.get_current_weights()
                if learned:
                    # Learned weights override default only (not directional sets)
                    weights_default = learned
                    if not asym_enabled:
                        weights_bullish = learned
                        weights_bearish = learned
                    errors.append(f"using learned weights: {learned}")
                # Load per-asset weights (Level 2)
                per_asset_learned = optimizer.get_per_asset_weights()
                if per_asset_learned:
                    errors.append(f"per-asset weights: {len(per_asset_learned)} assets")
            except Exception as exc:
                errors.append(f"optimizer load failed: {exc}")

        scoring_cfg = self.profile.get("scoring", {})
        label_cfg = self.profile.get("labels", [])

        signals: Dict[str, Dict[str, Any]] = {}
        all_roles = ["exchange_flow", "technical", "derivatives", "narrative", "market"]

        # Dynamic reweighting config (from YAML)
        reweight_cfg = self.profile.get("reweighting", {})
        reweight_enabled = reweight_cfg.get("enabled", False)
        tier_multipliers = reweight_cfg.get("tier_multipliers", {"full": 1.0, "partial": 0.5, "none": 0.0})
        agent_reweight_rules = reweight_cfg.get("agents", {})

        # --- Extract Fear & Greed value (used by regime scoring + signal tagging) ---
        fg_value = None
        _market_fg = raw.get("market")
        if _market_fg:
            fg_value = _market_fg.get("data", {}).get("sentiment", {}).get("fear_greed_index")

        # --- Phase 6 pre-compute: Dynamic abstain threshold (F&G-driven) ---
        # Resolve the abstain threshold ONCE before the asset loop.
        # In extreme fear/greed, contrarian edge is strongest → narrow the band.
        # In neutral markets, edge is weakest → widen the band.
        abstain_cfg = self.profile.get("abstain", {})
        base_min_distance = float(abstain_cfg.get("min_distance_from_center", 8))
        dynamic_cfg = abstain_cfg.get("dynamic", {})

        if dynamic_cfg.get("enabled", False):
            if fg_value is not None:
                # Find matching zone
                resolved_distance = base_min_distance  # fallback
                for zone in dynamic_cfg.get("zones", []):
                    if zone.get("fg_min", 0) <= fg_value < zone.get("fg_max", 100):
                        resolved_distance = float(zone.get("threshold", base_min_distance))
                        break
                # Edge case: F&G = 100 (exactly) → use last zone
                if fg_value == 100:
                    zones = dynamic_cfg.get("zones", [])
                    if zones:
                        resolved_distance = float(zones[-1].get("threshold", base_min_distance))
                errors.append(f"dynamic abstain: F&G={fg_value} → threshold={resolved_distance} (base={base_min_distance})")
            else:
                resolved_distance = base_min_distance
                errors.append(f"dynamic abstain: F&G unavailable → using base threshold={base_min_distance}")
        else:
            resolved_distance = base_min_distance

        # --- Regime detection pre-compute ---
        # Detect TRENDING vs RANGING using BTC's position relative to MA30.
        regime_cfg = self.profile.get("regime_weighting", {})
        detected_regime = "unknown"  # "trending", "ranging", or "unknown"
        regime_shifts: Dict[str, float] = {}

        if regime_cfg.get("enabled", False):
            det_cfg = regime_cfg.get("detection", {})
            trending_t = float(det_cfg.get("trending_threshold", 0.08))
            ranging_t = float(det_cfg.get("ranging_threshold", 0.03))

            btc_price_r = None
            btc_ma30_r = None
            btc_ma7_r = None
            market_data_r = raw.get("market")
            tech_data_r = raw.get("technical")
            if market_data_r:
                btc_price_r = market_data_r.get("data", {}).get("per_asset", {}).get("BTC", {}).get("price")
            if tech_data_r:
                btc_ma30_r = tech_data_r.get("data", {}).get("by_asset", {}).get("BTC", {}).get("ma_30d")
                btc_ma7_r = tech_data_r.get("data", {}).get("by_asset", {}).get("BTC", {}).get("ma_7d")

            if btc_price_r is not None and btc_ma30_r is not None and btc_ma30_r > 0:
                pct_from_ma30 = abs((btc_price_r - btc_ma30_r) / btc_ma30_r)
                ma_aligned = True
                if det_cfg.get("require_ma_alignment", True) and btc_ma7_r is not None:
                    price_above = btc_price_r > btc_ma30_r
                    ma7_above = btc_ma7_r > btc_ma30_r
                    ma_aligned = (price_above == ma7_above)

                if pct_from_ma30 > trending_t and ma_aligned:
                    detected_regime = "trending"
                    regime_shifts = {k: float(v) for k, v in regime_cfg.get("trending", {}).items()}
                elif pct_from_ma30 < ranging_t:
                    detected_regime = "ranging"
                    regime_shifts = {k: float(v) for k, v in regime_cfg.get("ranging", {}).items()}

                errors.append(
                    f"regime: BTC {pct_from_ma30:.1%} from MA30, aligned={ma_aligned} "
                    f"→ {detected_regime}"
                )
            else:
                errors.append("regime: BTC data unavailable")

        # --- Regime-based abstain modifier ---
        # Backtest v4: ranging regime has 24.3% accuracy. Widen abstain in ranging.
        regime_abstain_cfg = abstain_cfg.get("regime_modifier", {})
        if regime_abstain_cfg.get("enabled", False):
            regime_multiplier = float(regime_abstain_cfg.get(detected_regime, 1.0))
            if regime_multiplier != 1.0:
                old_dist = resolved_distance
                resolved_distance = resolved_distance * regime_multiplier
                errors.append(f"regime abstain: {detected_regime} → distance {old_dist:.1f} × {regime_multiplier:.1f} = {resolved_distance:.1f}")

        # Pre-inject market price changes into derivatives data for OI-price divergence
        _deriv_data = raw.get("derivatives")
        _market_data = raw.get("market")
        if _deriv_data and _market_data:
            d_by_asset = _deriv_data.get("data", {}).get("by_asset", {})
            m_by_asset = _market_data.get("data", {}).get("per_asset", {})
            for sym in self.assets:
                d_asset = d_by_asset.get(sym)
                m_asset = m_by_asset.get(sym)
                if d_asset and m_asset:
                    d_asset["_price_change_24h"] = m_asset.get("change_24h_pct")

        for asset in self.assets:
            # --- Phase 1: Score ALL dimensions first ---
            raw_scores: Dict[str, Tuple[float, str]] = {}
            for role in all_roles:
                agent_data = raw.get(role)
                rules = scoring_cfg.get(role, {})
                raw_scores[role] = self._score_dimension(role, asset, agent_data, rules)

            # --- Phase 2: Determine data tier for EVERY agent ---
            data_tiers: Dict[str, str] = {}
            for role in all_roles:
                if not reweight_enabled:
                    data_tiers[role] = "full"
                else:
                    score_val, detail_str = raw_scores[role]
                    data_tiers[role] = self._detect_data_tier(
                        role, score_val, detail_str,
                        agent_reweight_rules.get(role, {}),
                    )

            # --- Phase 3: Calculate adjusted weights ---
            # Direction-aware weight selection: compute unweighted average of
            # raw dimension scores to determine if the signal leans bullish or
            # bearish, then pick the appropriate weight set.
            raw_avg = sum(raw_scores[r][0] for r in all_roles) / len(all_roles)
            if asym_enabled:
                if raw_avg > 50:
                    weights = weights_bullish
                elif raw_avg < 50:
                    weights = weights_bearish
                else:
                    weights = weights_default
            else:
                weights = weights_default

            # Per-asset weight profiles (from YAML): tier-based overrides
            per_asset_cfg = self.profile.get("per_asset_weights", {})
            if per_asset_cfg.get("enabled", False):
                for tier_name in ["large_cap", "mid_cap", "small_cap", "blacklisted"]:
                    tier_cfg = per_asset_cfg.get(tier_name, {})
                    if asset in tier_cfg.get("assets", []):
                        tier_bullish = tier_cfg.get("weights_bullish")
                        tier_bearish = tier_cfg.get("weights_bearish")
                        if raw_avg > 50 and tier_bullish:
                            weights = tier_bullish
                        elif raw_avg < 50 and tier_bearish:
                            weights = tier_bearish
                        elif tier_bullish:
                            weights = tier_bullish  # fallback to bullish if no bearish
                        break

            # Per-asset weight override (Level 2): if we have IC-learned
            # weights specifically for this asset, use those instead
            using_per_asset = False
            if per_asset_learned and asset in per_asset_learned:
                weights = per_asset_learned[asset]
                using_per_asset = True

            base_weights: Dict[str, float] = {}
            for role in all_roles:
                base_weights[role] = float(weights.get(role, 0.0))

            # Regime-aware weight shifts: boost directional or contrarian dims
            if regime_cfg.get("enabled", False) and regime_shifts:
                for role in all_roles:
                    shift = float(regime_shifts.get(role, 1.0))
                    base_weights[role] *= shift
                # Renormalize to sum to 1.0
                total_w = sum(base_weights.values())
                if total_w > 0:
                    for role in all_roles:
                        base_weights[role] = base_weights[role] / total_w

            # Contrarian extreme sentiment override: when F&G is in extreme
            # fear/greed territory, boost the market dimension (the contrarian
            # signal carrier) at the expense of technical (lagging indicator).
            # This prevents the system from always following trend into reversals.
            contrarian_cfg = self.profile.get("contrarian_override", {})
            if contrarian_cfg.get("enabled", True):
                market_data = raw.get("market")
                if market_data and isinstance(market_data, dict):
                    fg_val = market_data.get("data", {}).get("sentiment", {}).get("fear_greed_index")
                    if fg_val is not None:
                        fg = float(fg_val)
                        extreme_fear_below = float(contrarian_cfg.get("extreme_fear_below", 20))
                        extreme_greed_above = float(contrarian_cfg.get("extreme_greed_above", 80))
                        market_boost = float(contrarian_cfg.get("market_boost", 1.5))
                        technical_dampen = float(contrarian_cfg.get("technical_dampen", 0.7))

                        if fg < extreme_fear_below or fg > extreme_greed_above:
                            if "market" in base_weights:
                                base_weights["market"] *= market_boost
                            if "technical" in base_weights:
                                base_weights["technical"] *= technical_dampen
                            # Renormalize
                            total_w = sum(base_weights.values())
                            if total_w > 0:
                                for role in all_roles:
                                    base_weights[role] = base_weights[role] / total_w

            # Apply tier multipliers to ALL agents, then redistribute freed weight
            adjusted_weights: Dict[str, float] = {}
            total_freed = 0.0
            full_data_roles: List[str] = []

            for role in all_roles:
                tier = data_tiers[role]
                mult = float(tier_multipliers.get(tier, 1.0))
                effective_w = base_weights[role] * mult
                adjusted_weights[role] = effective_w
                freed = base_weights[role] - effective_w
                total_freed += freed
                if mult >= 1.0:
                    full_data_roles.append(role)

            # Redistribute freed weight proportionally to agents with full data
            if total_freed > 0 and full_data_roles:
                full_data_sum = sum(base_weights[r] for r in full_data_roles)
                if full_data_sum > 0:
                    for role in full_data_roles:
                        adjusted_weights[role] += total_freed * (base_weights[role] / full_data_sum)

            # --- Phase 4: Build dimensions dict and compute composite ---
            dimensions: Dict[str, Dict[str, Any]] = {}
            composite = 0.0

            for role in all_roles:
                score, detail = raw_scores[role]
                label_name, direction = self._classify(score, label_cfg)
                adj_w = adjusted_weights[role]

                dimensions[role] = {
                    "score": round(score, 1),
                    "label": label_name,
                    "detail": detail,
                    "weight": round(adj_w, 3),
                    "data_tier": data_tiers[role],
                }
                composite += score * adj_w

            composite = round(composite, 1)

            # --- Phase 4b: Contrarian score nudge ---
            # In extreme sentiment, nudge composite toward the contrarian
            # direction. F&G < 25 nudges bullish, F&G > 75 nudges bearish.
            # This is the "smart layer" — extreme fear historically produces
            # 48h bounces, extreme greed produces corrections.
            if contrarian_cfg.get("enabled", True):
                market_data = raw.get("market")
                if market_data and isinstance(market_data, dict):
                    fg_val_nudge = market_data.get("data", {}).get("sentiment", {}).get("fear_greed_index")
                    if fg_val_nudge is not None:
                        fg_n = float(fg_val_nudge)
                        nudge_max = float(contrarian_cfg.get("score_nudge_max", 5.0))
                        ef_below = float(contrarian_cfg.get("extreme_fear_below", 25))
                        eg_above = float(contrarian_cfg.get("extreme_greed_above", 75))

                        # Tier-aware nudge scaling: large caps bounce more
                        # reliably from extreme sentiment than small caps
                        tier_scales = contrarian_cfg.get("tier_nudge_scale", {})
                        asset_tier = self._get_asset_tier(asset)
                        # Check per-asset-weight tier too
                        pa_cfg = self.profile.get("per_asset_weights", {})
                        for tn in ["large_cap", "mid_cap", "small_cap"]:
                            tc = pa_cfg.get(tn, {})
                            if asset in tc.get("assets", []):
                                asset_tier = tn
                                break
                        tier_scale = float(tier_scales.get(asset_tier, 1.0))

                        if fg_n < ef_below:
                            intensity = (ef_below - fg_n) / ef_below
                            nudge = nudge_max * intensity * tier_scale
                            composite = round(composite + nudge, 1)
                        elif fg_n > eg_above:
                            intensity = (fg_n - eg_above) / (100 - eg_above)
                            nudge = nudge_max * intensity * tier_scale
                            composite = round(composite - nudge, 1)

            # --- Phase 5: Abstain check ---
            # Phase A1 (2026-03-16): Asymmetric abstain zones.
            # Bearish signals (composite < 50) use tighter threshold (more signals pass).
            # Bullish signals (composite > 50) use wider threshold (fewer, higher quality).
            abstain_applied = False
            if abstain_cfg.get("enabled", False):
                asym_cfg = abstain_cfg.get("asymmetric", {})
                if asym_cfg.get("enabled", False):
                    # Apply regime multiplier to asymmetric distances too
                    _regime_mult = float(regime_abstain_cfg.get(detected_regime, 1.0)) if regime_abstain_cfg.get("enabled", False) else 1.0
                    bearish_dist = float(asym_cfg.get("bearish_min_distance", resolved_distance)) * _regime_mult
                    bullish_dist = float(asym_cfg.get("bullish_min_distance", resolved_distance)) * _regime_mult
                    if composite < 50.0 and (50.0 - composite) < bearish_dist:
                        abstain_applied = True
                    elif composite > 50.0 and (composite - 50.0) < bullish_dist:
                        abstain_applied = True
                    elif composite == 50.0:
                        abstain_applied = True
                else:
                    if abs(composite - 50.0) < resolved_distance:
                        abstain_applied = True

                if abstain_applied:
                    label_name = abstain_cfg.get("abstain_label", "INSUFFICIENT EDGE")
                    direction = "neutral"
                else:
                    # Build dynamic label config: MODERATE BUY at 50+threshold,
                    # NEUTRAL lower bound at 50-threshold.
                    buy_threshold = float(asym_cfg.get("bullish_min_distance", resolved_distance)) if asym_cfg.get("enabled") else resolved_distance
                    sell_threshold = float(asym_cfg.get("bearish_min_distance", resolved_distance)) if asym_cfg.get("enabled") else resolved_distance
                    dynamic_labels = []
                    for entry in label_cfg:
                        e = dict(entry)
                        if e.get("name") == "MODERATE BUY":
                            e["min_score"] = 50.0 + buy_threshold
                        elif e.get("name") == "NEUTRAL":
                            e["min_score"] = 50.0 - sell_threshold
                        dynamic_labels.append(e)
                    label_name, direction = self._classify(composite, dynamic_labels)
            else:
                label_name, direction = self._classify(composite, label_cfg)

            # Momentum vs previous run
            prev_score = self.store.load_kv("fusion_scores", asset)
            momentum_cfg = self.profile.get("momentum", {})
            threshold = float(momentum_cfg.get("threshold", 5))
            if prev_score is not None:
                delta = composite - prev_score
                if delta > threshold:
                    momentum = momentum_cfg.get("improving_label", "improving")
                elif delta < -threshold:
                    momentum = momentum_cfg.get("degrading_label", "degrading")
                else:
                    momentum = momentum_cfg.get("stable_label", "stable")
            else:
                momentum = "new"

            # --- Target price / Stop loss calculation ---
            # Only for directional signals (not INSUFFICIENT EDGE)
            target_data = {}
            if not abstain_applied and direction in ("buy", "sell"):
                try:
                    if self._target_calculator is None:
                        from signal_fusion.target_calculator import TargetCalculator
                        self._target_calculator = TargetCalculator(self.store, self.profile)
                    target_data = self._target_calculator.calculate(
                        asset=asset,
                        direction=direction,
                        composite_score=composite,
                        dimension_scores=dimensions,
                        agent_data=raw,
                    )
                except Exception as exc:
                    errors.append(f"target calc error for {asset}: {exc}")

            signals[asset] = {
                "composite_score": composite,
                "label": label_name,
                "direction": direction,
                "dimensions": dimensions,
                "momentum": momentum,
                "prev_score": round(prev_score, 1) if prev_score is not None else None,
                "data_tiers": data_tiers,
                "abstain": abstain_applied,
                "abstain_threshold": resolved_distance,
                "regime": detected_regime,
                "config_version": self.config_hash,
                "regime_at_generation": _fg_regime(fg_value),
                "per_asset_weights": using_per_asset,
                **({
                    "entry_price": target_data.get("entry_price"),
                    "target_price": target_data.get("target_price"),
                    "stop_loss": target_data.get("stop_loss"),
                    "risk_reward_ratio": target_data.get("risk_reward_ratio"),
                    "confidence": target_data.get("confidence"),
                    "timeframe_hours": target_data.get("timeframe_hours", 48),
                } if target_data else {}),
            }

            # Store current score for next momentum comparison
            self.store.save_kv("fusion_scores", asset, composite)

            # Save signal for future evaluation
            try:
                from signal_fusion.evaluator import SignalEvaluator
                eval_signal = dict(signals[asset])
                eval_signal["_features"] = features if not abstain_applied else {}
                evaluator = SignalEvaluator(self.store)
                evaluator.save_for_evaluation(eval_signal, asset)
            except Exception:
                pass  # non-critical — don't block fusion

        # --- Rank-direction overlay (backtest-validated 2026-08-17, R2) ---
        # 176d backtest: absolute-level direction produced 99.6% bullish calls at
        # 31.1% gradient accuracy, while composite RANKING carried real alpha
        # (IC +0.11). Overlay: top quartile by composite = buy, bottom = sell,
        # middle = neutral. R2 result: 34.7% gradient / 48.9% binary, balanced
        # calls, bearish arm 41.5%. Also restarts the learning loop: ~6
        # directional signals per run -> evaluations -> IC -> weight optimizer.
        rank_cfg = self.profile.get("rank_direction", {})
        if rank_cfg.get("enabled", False) and len(signals) >= int(rank_cfg.get("min_assets", 8)):
            side_fraction = float(rank_cfg.get("side_fraction", 0.25))
            ranked = sorted(signals.items(), key=lambda kv: kv[1].get("composite_score", 50), reverse=True)
            n_side = max(1, int(len(ranked) * side_fraction))
            for i, (r_asset, sig) in enumerate(ranked):
                if i < n_side:
                    new_dir, new_label = "buy", "RANK BUY"
                elif i >= len(ranked) - n_side:
                    new_dir, new_label = "sell", "RANK SELL"
                else:
                    if sig.get("direction") != "neutral":
                        sig["direction"], sig["label"] = "neutral", "NEUTRAL"
                    continue
                promoted = sig.get("direction") != new_dir
                sig["direction"], sig["label"], sig["abstain"] = new_dir, new_label, False
                sig["rank_position"] = i + 1
                # Rank-promoted signals need trading levels too
                if promoted and not sig.get("entry_price"):
                    try:
                        if self._target_calculator is None:
                            from signal_fusion.target_calculator import TargetCalculator
                            self._target_calculator = TargetCalculator(self.store, self.profile)
                        td = self._target_calculator.calculate(
                            asset=r_asset, direction=new_dir,
                            composite_score=sig.get("composite_score", 50),
                            dimension_scores=sig.get("dimensions", {}), agent_data=raw,
                        )
                        if td:
                            sig.update({k: td.get(k) for k in (
                                "entry_price", "target_price", "stop_loss",
                                "risk_reward_ratio", "confidence", "timeframe_hours") if td.get(k) is not None})
                    except Exception as exc:
                        errors.append(f"rank target calc {r_asset}: {exc}")

        # Portfolio summary
        portfolio = self._build_portfolio_summary(signals, raw)
        portfolio["fear_greed"] = fg_value
        portfolio["abstain_threshold"] = resolved_distance
        portfolio["detected_regime"] = detected_regime

        duration_ms = int((time.perf_counter() - start) * 1000)

        result = {
            "agent": "signal_fusion",
            "profile": self.profile.get("name", "signal_fusion_default"),
            "model_version": "v0.3.0-calibrated",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "success" if not errors else "partial",
            "data": {
                "portfolio_summary": portfolio,
                "signals": signals,
            },
            "meta": {
                "duration_ms": duration_ms,
                "errors": errors,
                "agents_available": [r for r, d in raw.items() if d is not None],
                "agents_missing": [r for r, d in raw.items() if d is None],
                "config_version": self.config_hash,
                "regime_at_generation": _fg_regime(fg_value),
            },
        }

        # Save fusion result for momentum tracking
        self.store.save("signal_fusion", result)

        return result

    # ================================================================ #
    #  Per-dimension scoring — dispatches by role
    # ================================================================ #

    def _score_dimension(
        self, role: str, asset: str, agent_result: Optional[Dict[str, Any]], rules: Dict[str, Any]
    ) -> Tuple[float, str]:
        """Score a single dimension for a single asset. Returns (score, detail_string)."""
        if agent_result is None:
            return 50.0, "no data"

        data = agent_result.get("data", {})
        scorer = getattr(self, f"_score_{role}", None)
        if scorer is None:
            return 50.0, "no scorer"

        try:
            return scorer(asset, data, rules)
        except Exception as exc:
            return 50.0, f"error: {exc}"

    # ================================================================ #
    #  EXCHANGE FLOW scorer (replaces whale scorer)
    # ================================================================ #

    def _score_exchange_flow(self, asset: str, data: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[float, str]:
        """Score exchange flow dimension for one asset.

        Reads from the exchange_flow_agent which provides:
        - Order book imbalance (bid/ask ratio)
        - Volume momentum (vs 7-day average)
        - Trade intensity
        - Pre-computed exchange_flow_score (0-100)
        """
        by_asset = data.get("by_asset", {})
        asset_data = by_asset.get(asset, {})

        if not asset_data:
            return 50.0, "no exchange flow data"

        # Use the agent's pre-computed score if available
        score = float(asset_data.get("exchange_flow_score", 50.0))
        details: List[str] = []

        # Build detail string from available data
        bid_ask = asset_data.get("bid_ask_ratio")
        if bid_ask is not None:
            details.append(f"bid/ask={bid_ask:.2f}")

        vol_change = asset_data.get("volume_change_pct")
        if vol_change is not None:
            details.append(f"vol_chg={vol_change:+.1f}%")

        status = asset_data.get("exchange_flow_status", "unknown")
        details.append(f"status={status}")

        score = max(float(rules.get("min_score", 0)), min(float(rules.get("max_score", 100)), score))
        return score, "; ".join(details) if details else "no exchange flow data"

    # ================================================================ #
    #  Asset tier helpers (for per-tier scoring overrides)
    # ================================================================ #

    def _get_asset_tier(self, asset: str) -> str:
        """Determine which tier an asset belongs to. Default: 'contrarian'."""
        tier_cfg = self.profile.get("asset_tiers", {})
        if not tier_cfg.get("enabled", False):
            return "contrarian"
        for tier_name, tier_def in tier_cfg.get("tiers", {}).items():
            if asset in [a.upper() for a in tier_def.get("assets", [])]:
                return tier_name
        return "contrarian"

    def _merge_rules(self, base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
        """Shallow merge: for each key in overrides, if both are dicts, merge sub-keys."""
        merged = dict(base)
        for key, val in overrides.items():
            if isinstance(val, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **val}
            else:
                merged[key] = val
        return merged

    # ================================================================ #
    #  TECHNICAL scorer
    # ================================================================ #

    def _score_technical(self, asset: str, data: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[float, str]:
        """Score technical dimension using agent's pre-computed score.

        The enhanced technical agent computes a 0-100 composite score from:
        RSI(25%), MACD(25%), Bollinger Bands(20%), Trend(30%) + volume bonus.
        We use that directly when available, with detail annotation.
        """
        by_asset = data.get("by_asset", {})
        asset_data = by_asset.get(asset, {})
        if not asset_data:
            return 50.0, "no data"

        details: List[str] = []

        # Use agent's pre-computed score when available
        agent_score = asset_data.get("technical_score")
        if agent_score is not None:
            score = float(agent_score)
            # Build detail string from key indicators
            rsi = asset_data.get("rsi_14")
            if rsi is not None:
                details.append(f"RSI={rsi:.0f}")
            macd_h = asset_data.get("macd_histogram")
            if macd_h is not None:
                details.append(f"MACD_H={macd_h:.4f}")
            bb_pos = asset_data.get("bb_position")
            if bb_pos is not None:
                details.append(f"BB={bb_pos:.2f}")
            atr_pct = asset_data.get("atr_pct")
            if atr_pct is not None:
                details.append(f"ATR={atr_pct:.1f}%")
            return max(0.0, min(100.0, score)), "; ".join(details)

        # Fallback: legacy point-based scoring when agent score unavailable
        score = 0.0
        rsi = asset_data.get("rsi_14")
        if rsi is not None:
            if rsi < 30:
                score += 35; details.append(f"RSI {rsi:.0f} oversold")
            elif rsi > 70:
                score += 10; details.append(f"RSI {rsi:.0f} overbought")
            else:
                ratio = (rsi - 30) / 40
                score += 15 + ratio * 25; details.append(f"RSI {rsi:.0f}")

        macd_val = asset_data.get("macd_line")
        macd_signal = asset_data.get("macd_signal")
        if macd_val is not None and macd_signal is not None:
            if macd_val > macd_signal:
                score += 20; details.append("MACD bullish")
            else:
                details.append("MACD bearish")

        price = asset_data.get("price")
        ma30 = asset_data.get("ma_30d")
        if price is not None and ma30 is not None:
            if price > ma30:
                score += 20; details.append("above MA30")

        trend = asset_data.get("trend_30d", "")
        if trend == "bullish":
            score += 20; details.append("trend bullish")
        elif trend == "bearish":
            details.append("trend bearish")
        else:
            score += 10

        return min(100.0, max(0.0, score)), "; ".join(details) if details else "no tech data"

    # ================================================================ #
    #  DERIVATIVES scorer
    # ================================================================ #

    def _score_derivatives(self, asset: str, data: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[float, str]:
        """Score derivatives dimension using agent's pre-computed score.

        The enhanced derivatives agent computes a 0-100 composite score from:
        funding rate, L/S ratio, OI changes, taker ratio, liquidations,
        top trader positions, and funding term structure.
        We use that directly when available, with detail annotation.
        """
        by_asset = data.get("by_asset", {})
        asset_data = by_asset.get(asset, {})
        if not asset_data:
            return 50.0, "no data"

        details: List[str] = []

        # Use agent's pre-computed score when available
        agent_score = asset_data.get("derivatives_score")
        if agent_score is not None:
            score = float(agent_score)
            # Build detail string from key indicators
            ls = asset_data.get("long_short_ratio")
            if ls is not None:
                details.append(f"L/S={ls:.2f}")
            funding = asset_data.get("funding_rate")
            if funding is not None:
                details.append(f"FR={funding:.5f}")
            oi = asset_data.get("open_interest_usd") or asset_data.get("open_interest")
            if oi is not None:
                details.append(f"OI=${float(oi)/1e6:.0f}M")
            taker = asset_data.get("taker_buy_sell_ratio")
            if taker is not None:
                details.append(f"taker={taker:.3f}")
            liq = asset_data.get("liquidation_imbalance")
            if liq is not None:
                details.append(f"liq_imb={liq:.2f}")
            return max(0.0, min(100.0, score)), "; ".join(details)

        # Fallback: legacy point-based scoring when agent score unavailable
        score = 0.0

        # Long/short ratio — with very_overcrowded tier (YAML-driven)
        ls_rules = rules.get("long_short", {})
        ls_ratio = asset_data.get("long_short_ratio")
        ls_tier = None  # Track for combo scoring
        if ls_ratio is not None:
            sweet_min = float(ls_rules.get("sweet_spot_min", 0.55))
            sweet_max = float(ls_rules.get("sweet_spot_max", 0.65))
            very_overcrowded = float(ls_rules.get("very_overcrowded_above", 999))
            overcrowded = float(ls_rules.get("overcrowded_above", 0.70))
            contrarian = float(ls_rules.get("contrarian_below", 0.45))

            if ls_ratio > very_overcrowded:
                # Continuous: more overcrowded = lower score
                very_oc_s = float(ls_rules.get("very_overcrowded_score", 3))
                oc_s = float(ls_rules.get("overcrowded_score", 8))
                denom = 1.0 - very_overcrowded
                ratio = min((ls_ratio - very_overcrowded) / denom, 1.0) if denom > 0 else 1.0
                score += oc_s + ratio * (very_oc_s - oc_s)
                details.append(f"L/S {ls_ratio:.2f} very overcrowded")
                ls_tier = "very_overcrowded"
            elif sweet_min <= ls_ratio <= sweet_max:
                score += float(ls_rules.get("sweet_spot_score", 40))
                details.append(f"L/S {ls_ratio:.2f} sweet spot")
                ls_tier = "sweet_spot"
            elif ls_ratio > overcrowded:
                # Continuous between sweet_max and very_overcrowded
                oc_s = float(ls_rules.get("overcrowded_score", 8))
                sw_s = float(ls_rules.get("sweet_spot_score", 18))
                denom = very_overcrowded - sweet_max
                ratio = (ls_ratio - sweet_max) / denom if denom > 0 else 0.0
                score += sw_s + ratio * (oc_s - sw_s)
                details.append(f"L/S {ls_ratio:.2f} overcrowded")
                ls_tier = "overcrowded"
            elif ls_ratio < contrarian:
                # Continuous: lower ratio = stronger contrarian signal
                contrarian_s = float(ls_rules.get("contrarian_score", 35))
                extreme_contrarian_s = float(ls_rules.get("extreme_contrarian_score", 40))
                ratio = ls_ratio / contrarian if contrarian > 0 else 0.0  # 0.0 at ratio=0, 1.0 at threshold
                score += extreme_contrarian_s + ratio * (contrarian_s - extreme_contrarian_s)
                details.append(f"L/S {ls_ratio:.2f} contrarian")
                ls_tier = "contrarian"
            else:
                score += float(ls_rules.get("default_score", 25))
                details.append(f"L/S {ls_ratio:.2f}")
                ls_tier = "default"

        # Funding rate — continuous within negative and positive zones
        fund_rules = rules.get("funding", {})
        funding = asset_data.get("funding_rate")
        funding_tier = None  # Track for combo scoring
        if funding is not None:
            if funding < 0:
                # Continuous: more negative = stronger squeeze signal
                extreme_neg_threshold = float(fund_rules.get("extreme_negative_threshold", 0.005))
                extreme_neg_score = float(fund_rules.get("extreme_negative_score", 40))
                mild_neg_score = float(fund_rules.get("negative_mild_score", 25))
                intensity = min(abs(funding) / extreme_neg_threshold, 1.0) if extreme_neg_threshold > 0 else 0.0
                score += mild_neg_score + intensity * (extreme_neg_score - mild_neg_score)
                details.append(f"funding {funding:.5f} negative")
                funding_tier = "negative"
            elif funding < float(fund_rules.get("low_threshold", 0.0002)):
                score += float(fund_rules.get("low_score", 30))
                details.append("low funding")
                funding_tier = "low"
            elif funding < float(fund_rules.get("moderate_threshold", 0.0005)):
                # Continuous between low and moderate
                low_t = float(fund_rules.get("low_threshold", 0.0002))
                mod_t = float(fund_rules.get("moderate_threshold", 0.0005))
                low_s = float(fund_rules.get("low_score", 17))
                mod_s = float(fund_rules.get("moderate_score", 12))
                denom = mod_t - low_t
                ratio = (funding - low_t) / denom if denom > 0 else 0.0
                score += low_s + ratio * (mod_s - low_s)
                funding_tier = "moderate"
            else:
                # Continuous above moderate: higher funding = worse
                mod_t = float(fund_rules.get("moderate_threshold", 0.0005))
                high_s = float(fund_rules.get("high_score", 5))
                mod_s = float(fund_rules.get("moderate_score", 12))
                extreme_high_threshold = float(fund_rules.get("extreme_high_threshold", 0.003))
                denom = extreme_high_threshold - mod_t
                ratio = min((funding - mod_t) / denom, 1.0) if denom > 0 else 1.0
                score += mod_s + ratio * (high_s - mod_s)
                details.append("high funding")
                funding_tier = "high"

        # Open interest — compare to previous run to detect rising/falling
        oi_rules = rules.get("open_interest", {})
        oi = asset_data.get("open_interest_usd") or asset_data.get("open_interest")
        if oi is not None:
            prev_oi = self.store.load_kv("oi_prev", asset)
            self.store.save_kv("oi_prev", asset, float(oi))

            if prev_oi is not None and prev_oi > 0:
                oi_change_pct = ((float(oi) - prev_oi) / prev_oi) * 100
                threshold = float(oi_rules.get("change_threshold_pct", 5))
                if oi_change_pct > threshold:
                    score += float(oi_rules.get("rising_score", 25))
                    details.append(f"OI +{oi_change_pct:.1f}%")
                elif oi_change_pct < -threshold:
                    score += float(oi_rules.get("falling_score", 10))
                    details.append(f"OI {oi_change_pct:.1f}%")
                else:
                    score += float(oi_rules.get("stable_score", 15))
            else:
                score += float(oi_rules.get("stable_score", 15))

        # --- Combo signals (YAML-driven cross-indicator patterns) ---
        if ls_tier is not None and funding_tier is not None:
            # Overcrowded longs + high funding = crash risk
            combo_penalty = float(rules.get("combo_overcrowded_high_funding_penalty", 0))
            if ls_tier in ("overcrowded", "very_overcrowded") and funding_tier == "high" and combo_penalty != 0:
                score += combo_penalty
                details.append("combo: overcrowded+high_funding")

            # Contrarian shorts + negative funding = squeeze setup
            combo_bonus = float(rules.get("combo_contrarian_negative_funding_bonus", 0))
            if ls_tier == "contrarian" and funding_tier == "negative" and combo_bonus != 0:
                score += combo_bonus
                details.append("combo: contrarian+neg_funding")

        # --- Lead indicator: Funding rate CHANGE (delta) ---
        # The REVERSAL of funding is the signal, not the level.
        # Funding contraction = pressure easing. Funding expansion = pressure building.
        fr_chg_rules = rules.get("funding_rate_change", {})
        if fr_chg_rules.get("enabled", False):
            fr_chg = asset_data.get("funding_rate_change_4h")
            if fr_chg is None:
                fr_chg = asset_data.get("funding_rate_change_24h")
            if fr_chg is not None:
                # Positive delta = funding becoming more positive = shorts easing = bearish
                # Negative delta = funding becoming more negative = squeeze building = bullish
                threshold = float(fr_chg_rules.get("threshold", 0.00005))
                max_pts = float(fr_chg_rules.get("max_points", 8))
                if abs(fr_chg) > threshold:
                    intensity = min(abs(fr_chg) / (threshold * 5), 1.0)
                    pts = intensity * max_pts
                    if fr_chg < 0:
                        score += pts  # funding falling = bullish (squeeze building)
                        details.append(f"fr_chg {fr_chg:+.6f} bullish")
                    else:
                        score -= pts  # funding rising = bearish (longs paying more)
                        details.append(f"fr_chg {fr_chg:+.6f} bearish")

        # --- Lead indicator: OI-price divergence ---
        # OI rising + price flat/falling = conviction without result = pressure building
        # OI falling + price rising = weak rally (no conviction) = bearish
        oi_div_rules = rules.get("oi_price_divergence", {})
        if oi_div_rules.get("enabled", False):
            oi_chg = asset_data.get("oi_change_pct_4h") or asset_data.get("oi_change_pct_24h")
            price_chg = asset_data.get("_price_change_24h")
            if oi_chg is not None and price_chg is not None:
                oi_thresh = float(oi_div_rules.get("oi_threshold_pct", 3.0))
                price_thresh = float(oi_div_rules.get("price_threshold_pct", 2.0))
                max_pts = float(oi_div_rules.get("max_points", 10))

                # OI rising but price not following = breakout building
                if oi_chg > oi_thresh and abs(price_chg) < price_thresh:
                    score += max_pts
                    details.append(f"OI÷price diverge: OI+{oi_chg:.1f}% price={price_chg:+.1f}%")
                # OI falling but price rising = weak rally
                elif oi_chg < -oi_thresh and price_chg > price_thresh:
                    score -= max_pts
                    details.append(f"OI÷price weak rally: OI{oi_chg:.1f}% price+{price_chg:.1f}%")
                # OI falling with price falling = deleveraging (bearish confirmation)
                elif oi_chg < -oi_thresh and price_chg < -price_thresh:
                    score -= max_pts * 0.5
                    details.append(f"OI÷price delever: OI{oi_chg:.1f}% price{price_chg:.1f}%")

        # --- Taker buy/sell ratio (Phase C1) ---
        tr_rules = rules.get("taker_ratio", {})
        if tr_rules.get("enabled", False):
            taker_ratio = asset_data.get("taker_buy_sell_ratio")
            if taker_ratio is not None:
                bullish_t = float(tr_rules.get("bullish_threshold", 1.1))
                bearish_t = float(tr_rules.get("bearish_threshold", 0.9))
                max_pts = float(tr_rules.get("max_points", 10))
                if taker_ratio > bullish_t:
                    # Aggressive buyers — bullish signal, scale by intensity
                    intensity = min((taker_ratio - bullish_t) / (bullish_t * 0.5), 1.0)
                    pts = float(tr_rules.get("bullish_score", 8)) * max(intensity, 0.5)
                    score += min(pts, max_pts)
                    details.append(f"taker {taker_ratio:.3f} bullish")
                elif taker_ratio < bearish_t:
                    # Aggressive sellers — bearish signal, scale by intensity
                    intensity = min((bearish_t - taker_ratio) / (bearish_t * 0.5), 1.0)
                    pts = float(tr_rules.get("bearish_score", -8)) * max(intensity, 0.5)
                    score += max(pts, -max_pts)
                    details.append(f"taker {taker_ratio:.3f} bearish")
                else:
                    score += float(tr_rules.get("neutral_score", 0))

        return min(100.0, max(0.0, score)), "; ".join(details) if details else "no deriv data"

    # ================================================================ #
    #  NARRATIVE scorer
    # ================================================================ #

    def _score_narrative(self, asset: str, data: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[float, str]:
        """Score narrative dimension using agent's pre-computed score.

        The enhanced narrative agent computes a 0-100 composite score from:
        social volume, LLM sentiment, community sentiment, trending status,
        influencer mentions, multi-source confirmation, and event scoring.
        We use that directly when available, with detail annotation.
        """
        by_asset = data.get("by_asset", {})
        asset_data = by_asset.get(asset, {})
        if not asset_data:
            return 50.0, "no data"

        details: List[str] = []

        # Use agent's pre-computed score when available
        agent_score = asset_data.get("narrative_score")
        if agent_score is not None:
            score = float(agent_score)
            # Build detail string from key indicators
            mentions = asset_data.get("total_mentions", 0)
            if mentions:
                details.append(f"{mentions} mentions")
            coverage = asset_data.get("data_coverage")
            if coverage is not None:
                details.append(f"cov={coverage:.0%}")
            norm = asset_data.get("normalised_score")
            if norm is not None:
                details.append(f"vol={norm:.2f}")
            llm = asset_data.get("llm_sentiment")
            if llm and isinstance(llm, dict):
                tone = llm.get("tone", "neutral")
                details.append(f"LLM:{tone}")
            community = asset_data.get("community_sentiment")
            if community and isinstance(community, dict):
                cs = community.get("score")
                if cs is not None:
                    details.append(f"comm={float(cs):.2f}")
            if asset_data.get("trending_coingecko"):
                details.append("trending")
            # For data tier detection: if score is very low and no mentions, signal "low buzz"
            if score < 1.0 and not mentions:
                return 0.0, "low buzz"
            return max(0.0, min(100.0, score)), "; ".join(details) if details else "narrative data"

        # Fallback: legacy point-based scoring when agent score unavailable

        # Base score (YAML-configurable, allows contrarian penalties room)
        score = float(rules.get("narrative_base_score", 0))

        # --- Component 1: Volume score (0-30 points) ---
        # When volume_invert=true, high mentions → LOW score (contrarian)
        raw_score = float(asset_data.get("normalised_score", 0.0))
        volume_mult = float(rules.get("volume_multiplier", 30))
        volume_invert = rules.get("volume_invert", False)

        if volume_invert:
            volume_pts = (1.0 - raw_score) * volume_mult
        else:
            volume_pts = raw_score * volume_mult
        score += volume_pts

        if raw_score > 0:
            total_mentions = int(asset_data.get("total_mentions", 0))
            inv_tag = " [inv]" if volume_invert else ""
            details.append(f"vol {raw_score:.2f}{inv_tag} ({total_mentions} mentions)")

        # Quiet bonus: low mentions = opportunity (contrarian)
        quiet_threshold = float(rules.get("quiet_threshold", 0))
        quiet_bonus = float(rules.get("quiet_bonus", 0))
        if quiet_threshold > 0 and raw_score < quiet_threshold and quiet_bonus != 0:
            score += quiet_bonus
            details.append("quiet")

        # --- Component 2: LLM sentiment (0-25 points) ---
        llm_data = asset_data.get("llm_sentiment")
        llm_max = float(rules.get("llm_max_points", 25))
        llm_min_conf = float(rules.get("llm_min_confidence", 0.3))
        if llm_data and isinstance(llm_data, dict):
            llm_sent = float(llm_data.get("sentiment", 0.0))
            llm_conf = float(llm_data.get("confidence", 0.0))
            if llm_conf >= llm_min_conf:
                # Map -1..1 to 0..max with 0 = max/2
                llm_pts = (llm_sent + 1.0) / 2.0 * llm_max
                score += llm_pts
                tone = llm_data.get("tone", "neutral")
                narrative = llm_data.get("dominant_narrative", "")
                details.append(f"LLM {tone}")
                if narrative:
                    details.append(narrative)

        # --- Component 3: Community sentiment (0-15 points) ---
        community = asset_data.get("community_sentiment")
        community_max = float(rules.get("community_max_points", 15))
        if community and isinstance(community, dict):
            cs_score = community.get("score")
            if cs_score is not None:
                # Map -1..1 to 0..max
                community_pts = (float(cs_score) + 1.0) / 2.0 * community_max
                score += community_pts
                bull = community.get("bullish", 0)
                bear = community.get("bearish", 0)
                details.append(f"community {bull}B/{bear}S")

        # --- Component 4: Trending bonus (can be NEGATIVE for contrarian) ---
        trending = asset_data.get("trending_coingecko", False)
        trending_bonus = float(rules.get("trending_bonus", 10))
        if trending:
            score += trending_bonus
            if trending_bonus < 0:
                details.append("trending [contrarian]")
            else:
                details.append("trending")

        # --- Component 5: Influencer bonus ---
        inf_count = int(asset_data.get("influencer_mentions", 0))
        inf_threshold = int(rules.get("influencer_threshold", 2))
        inf_bonus = float(rules.get("influencer_bonus", 10))
        if inf_count >= inf_threshold:
            score += inf_bonus
            names = asset_data.get("top_influencers_active", [])
            if names:
                details.append(f"{inf_count} influencers ({', '.join(names[:2])})")
            else:
                details.append(f"{inf_count} influencers")

        # --- Component 6: Multi-source confirmation ---
        sources_with_data = int(asset_data.get("sources_with_data", 0))
        multi_threshold = int(rules.get("multi_source_threshold", 3))
        multi_bonus = float(rules.get("multi_source_bonus", 10))
        if sources_with_data >= multi_threshold:
            score += multi_bonus
            details.append(f"{sources_with_data} sources")

        # --- Component 7: LLM Event scoring ---
        events = asset_data.get("llm_events", [])
        event_rules = rules.get("event_scoring", {})
        if event_rules.get("enabled", False) and events and isinstance(events, list):
            type_weights = event_rules.get("type_weights", {})
            mag_mult = event_rules.get("magnitude_multipliers", {})
            max_events = int(event_rules.get("max_events_scored", 3))
            max_ev_pts = float(event_rules.get("max_points", 20))

            # Sort events by magnitude (critical > high > medium > low)
            mag_order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
            valid_events = [e for e in events if isinstance(e, dict)]
            sorted_events = sorted(
                valid_events,
                key=lambda e: mag_order.get(e.get("magnitude", "low"), 0),
                reverse=True,
            )

            event_pts = 0.0
            scored_labels: List[str] = []
            for ev in sorted_events[:max_events]:
                ev_type = ev.get("type", "general_sentiment")
                ev_impact = ev.get("impact", "neutral")
                ev_mag = ev.get("magnitude", "low")
                ev_conf = float(ev.get("confidence", 0.5))

                base_w = float(type_weights.get(ev_type, 2))
                mult = float(mag_mult.get(ev_mag, 0.3))
                pts = base_w * mult * ev_conf

                if ev_impact == "bearish":
                    pts = -pts

                event_pts += pts
                scored_labels.append(f"{ev_type}:{ev_impact}")

            # Clamp to max
            event_pts = max(-max_ev_pts, min(max_ev_pts, event_pts))
            score += event_pts

            if scored_labels:
                details.append(f"events({len(scored_labels)}): {', '.join(scored_labels[:2])}")

        max_score = float(rules.get("max_score", 100))

        # If zero mentions across all sources, return "low buzz" with score 0.
        # The reweighting system will detect this (none_if_score_below: 1.0)
        # and set narrative weight to 0%, preventing it from dragging the
        # composite down for assets that simply aren't being discussed.
        return min(max_score, max(0.0, score)), "; ".join(details) if details else "low buzz"

    # ================================================================ #
    #  MARKET scorer
    # ================================================================ #

    def _score_market(self, asset: str, data: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[float, str]:
        """Score market dimension using agent's pre-computed score.

        The enhanced market agent computes a 0-100 composite score from:
        price action, volume, F&G index, BTC dominance, order book depth,
        stablecoin flows, macro data (VIX/DXY/S&P), and ATR.
        We use that directly when available, with detail annotation.
        """
        per_asset = data.get("per_asset", {})
        asset_data = per_asset.get(asset, {})
        details: List[str] = []

        # Use agent's pre-computed score when available
        agent_score = asset_data.get("market_score")
        if agent_score is not None:
            score = float(agent_score)
            # Build detail string from key indicators
            chg = asset_data.get("change_24h_pct")
            if chg is not None:
                details.append(f"24h={chg:+.1f}%")
            vol_ratio = asset_data.get("volume_spike_ratio")
            if vol_ratio is not None:
                details.append(f"vol={vol_ratio:.1f}x")
            sentiment = data.get("sentiment", {})
            fg = sentiment.get("fear_greed_index")
            if fg is not None:
                details.append(f"F&G={fg:.0f}")
            ob = asset_data.get("order_book_imbalance")
            if ob is not None:
                details.append(f"OB_imb={ob:.2f}")
            atr_pct = asset_data.get("atr_pct")
            if atr_pct is not None:
                details.append(f"ATR={atr_pct:.1f}%")
            macro = data.get("macro", {})
            if macro:
                vix = macro.get("vix")
                if vix is not None:
                    details.append(f"VIX={vix:.0f}")
            return max(0.0, min(100.0, score)), "; ".join(details) if details else "market data"

        # Fallback: legacy point-based scoring when agent score unavailable
        score = float(rules.get("base_score", 0.0))  # Bipolar: start at 50

        # Price change — continuous scoring (no flat buckets)
        pc_rules = rules.get("price_change", {})
        change_24h = asset_data.get("change_24h_pct")
        if change_24h is not None:
            strong_pos = float(pc_rules.get("strong_positive_above", 5.0))
            pos = float(pc_rules.get("positive_above", 0.0))
            mild_neg = float(pc_rules.get("mild_negative_above", -5.0))

            if change_24h > strong_pos:
                # Continuous: bigger rally = lower contrarian score
                strong_pos_s = float(pc_rules.get("strong_positive_score", 10))
                extreme_rally_s = float(pc_rules.get("extreme_rally_score", 5))
                extreme_rally_above = float(pc_rules.get("extreme_rally_above", 20.0))
                denom = extreme_rally_above - strong_pos
                ratio = min((change_24h - strong_pos) / denom, 1.0) if denom > 0 else 0.0
                score += strong_pos_s + ratio * (extreme_rally_s - strong_pos_s)
                details.append(f"+{change_24h:.1f}% strong")
            elif change_24h > pos:
                # Continuous between 0% and +5%
                pos_s = float(pc_rules.get("positive_score", 15))
                strong_pos_s = float(pc_rules.get("strong_positive_score", 10))
                denom = strong_pos - pos
                ratio = (change_24h - pos) / denom if denom > 0 else 0.0
                score += pos_s + ratio * (strong_pos_s - pos_s)
                details.append(f"+{change_24h:.1f}%")
            elif change_24h > mild_neg:
                # Continuous between -5% and 0%
                mild_neg_s = float(pc_rules.get("mild_negative_score", 25))
                pos_s = float(pc_rules.get("positive_score", 15))
                denom = pos - mild_neg
                ratio = (change_24h - mild_neg) / denom if denom > 0 else 0.0
                score += mild_neg_s + ratio * (pos_s - mild_neg_s)
                details.append(f"{change_24h:.1f}%")
            else:
                # Continuous: bigger drop = higher contrarian score
                strong_neg_s = float(pc_rules.get("strong_negative_score", 30))
                extreme_drop_s = float(pc_rules.get("extreme_drop_score", 35))
                extreme_drop_below = float(pc_rules.get("extreme_drop_below", -20.0))
                denom = mild_neg - extreme_drop_below
                ratio = min((mild_neg - change_24h) / denom, 1.0) if denom > 0 else 0.0
                score += strong_neg_s + ratio * (extreme_drop_s - strong_neg_s)
                details.append(f"{change_24h:.1f}% drop")

        # Volume spike — market agent stores this in per_asset directly
        vol_rules = rules.get("volume", {})
        vol_ratio = asset_data.get("volume_spike_ratio")
        # volume_spike_ratio from market agent is (24h vol / 7d avg) — may be < 1
        # Normalize: the ratio is already 24h/7d_avg, so >2 = spike
        if vol_ratio is not None:
            spike = float(vol_rules.get("spike_multiplier_above", 2.0))
            elevated = float(vol_rules.get("elevated_multiplier_above", 1.5))
            if vol_ratio > spike:
                score += float(vol_rules.get("spike_score", 30))
                details.append(f"{vol_ratio:.1f}x vol spike")
            elif vol_ratio > elevated:
                score += float(vol_rules.get("elevated_score", 20))
                details.append(f"{vol_ratio:.1f}x vol")
            else:
                score += float(vol_rules.get("normal_score", 10))

        # Fear & Greed (global, same for all assets) — continuous scoring
        fg_rules = rules.get("fear_greed", {})
        sentiment = data.get("sentiment", {})
        fg_value = sentiment.get("fear_greed_index")
        if fg_value is not None:
            fg = float(fg_value)
            ef_below = float(fg_rules.get("extreme_fear_below", 25))
            f_below = float(fg_rules.get("fear_below", 45))
            n_below = float(fg_rules.get("neutral_below", 55))
            g_below = float(fg_rules.get("greed_below", 75))

            ef_score = float(fg_rules.get("extreme_fear_score", 25))
            max_ef_score = float(fg_rules.get("max_extreme_fear_score", 30))
            f_score = float(fg_rules.get("fear_score", 20))
            n_score = float(fg_rules.get("neutral_score", 15))
            g_score = float(fg_rules.get("greed_score", 10))
            eg_score = float(fg_rules.get("extreme_greed_score", 5))
            min_eg_score = float(fg_rules.get("min_extreme_greed_score", 3))

            if fg < ef_below:
                # Continuous: F&G 0→25 maps to max_extreme→extreme score
                ratio = fg / ef_below if ef_below > 0 else 0.0
                score += max_ef_score + ratio * (ef_score - max_ef_score)
                details.append(f"F&G {fg:.0f} extreme fear")
            elif fg < f_below:
                # Continuous: F&G 25→45 maps to extreme_fear→fear score
                denom = f_below - ef_below
                ratio = (fg - ef_below) / denom if denom > 0 else 0.0
                score += ef_score + ratio * (f_score - ef_score)
                details.append(f"F&G {fg:.0f} fear")
            elif fg < n_below:
                # Continuous: F&G 45→55 maps to fear→neutral score
                denom = n_below - f_below
                ratio = (fg - f_below) / denom if denom > 0 else 0.0
                score += f_score + ratio * (n_score - f_score)
            elif fg < g_below:
                # Continuous: F&G 55→75 maps to neutral→greed score
                denom = g_below - n_below
                ratio = (fg - n_below) / denom if denom > 0 else 0.0
                score += n_score + ratio * (g_score - n_score)
            else:
                # Continuous: F&G 75→100 maps to greed→min extreme greed score
                denom = 100.0 - g_below
                ratio = min((fg - g_below) / denom, 1.0) if denom > 0 else 1.0
                score += g_score + ratio * (min_eg_score - g_score)
                details.append(f"F&G {fg:.0f} extreme greed")

        # BTC Dominance (global, scored differently for BTC vs alts)
        btcd_rules = rules.get("btc_dominance", {})
        if btcd_rules.get("enabled", False):
            global_market = data.get("global_market", {})
            btc_dom = global_market.get("btc_dominance") if global_market else None
            if btc_dom is not None:
                prev_btc_dom = self.store.load_kv("btc_dom_prev", "__global__")
                self.store.save_kv("btc_dom_prev", "__global__", float(btc_dom))

                is_btc = (asset == "BTC")
                threshold = float(btcd_rules.get("change_threshold_pct", 0.3))

                if prev_btc_dom is not None and prev_btc_dom > 0:
                    btcd_change = btc_dom - prev_btc_dom
                    if btcd_change > threshold:
                        # Rising BTC.D
                        key = "btc_rising_score" if is_btc else "alt_rising_score"
                        score += float(btcd_rules.get(key, 10))
                        tag = "bullish" if is_btc else "bearish"
                        details.append(f"BTC.D +{btcd_change:.1f}% {tag}")
                    elif btcd_change < -threshold:
                        # Falling BTC.D
                        key = "btc_falling_score" if is_btc else "alt_falling_score"
                        score += float(btcd_rules.get(key, 10))
                        tag = "bearish" if is_btc else "alt season"
                        details.append(f"BTC.D {btcd_change:.1f}% {tag}")
                    else:
                        key = "btc_stable_score" if is_btc else "alt_stable_score"
                        score += float(btcd_rules.get(key, 10))
                else:
                    key = "btc_stable_score" if is_btc else "alt_stable_score"
                    score += float(btcd_rules.get(key, 10))

        # Trend awareness penalty: when fear AND price drop confirm each other,
        # this is likely a genuine downtrend — not a dip-buying opportunity.
        # Contrarian signals work when they CONTRADICT (fear + stable price).
        # When they ALIGN (fear + dropping price), dampen the bullish push.
        ta_rules = rules.get("trend_awareness", {})
        if ta_rules.get("enabled", False):
            fg_t = float(ta_rules.get("fg_threshold", 35))
            drop_t = float(ta_rules.get("drop_threshold", -2.0))
            max_pen = float(ta_rules.get("max_penalty", -30))

            # Get the F&G and price change we already computed
            sentiment = data.get("sentiment", {})
            fg_val = sentiment.get("fear_greed_index")
            chg = asset_data.get("change_24h_pct")

            if fg_val is not None and chg is not None:
                fg_f = float(fg_val)
                chg_f = float(chg)
                if fg_f < fg_t and chg_f < drop_t:
                    fg_intensity = (fg_t - fg_f) / fg_t  # 0→1 as fear increases
                    drop_intensity = min(abs(chg_f) / 10.0, 1.0)  # 0→1 as drop deepens
                    penalty = fg_intensity * drop_intensity * max_pen
                    score += penalty
                    details.append(f"downtrend penalty {penalty:.0f}")

        return min(100.0, max(0.0, score)), "; ".join(details) if details else "no market data"

    def _detect_data_tier(
        self, role: str, score: float, detail: str, rules: Dict[str, Any],
    ) -> str:
        """Determine data quality tier for an agent's score on a given asset.

        Returns "full", "partial", or "none".
        Rules are loaded from YAML: reweighting.agents.<role>
        """
        detail_lower = detail.lower()

        # Universal: errors always → none
        if detail_lower.startswith("error:"):
            return "none"

        # Check no-data keywords (YAML-configurable per agent)
        no_data_kws = [kw.lower() for kw in rules.get("no_data_keywords", ["no data", "no scorer"])]
        if any(kw in detail_lower for kw in no_data_kws):
            return "none"

        # Score-based none detection (e.g., narrative score=0 means no data)
        none_below = rules.get("none_if_score_below")
        if none_below is not None and score <= float(none_below):
            return "none"

        # Check full-data keywords (YAML-configurable per agent)
        full_data_kws = [kw.lower() for kw in rules.get("full_data_keywords", [])]
        if full_data_kws:
            if any(kw in detail_lower for kw in full_data_kws):
                return "full"
            # Has data but not the strong keywords → partial
            return "partial"

        # Score-based partial detection
        partial_below = rules.get("partial_if_score_below")
        if partial_below is not None and score < float(partial_below):
            return "partial"

        # Partial-keywords: if detail ONLY contains these, it's partial
        partial_kws = [kw.lower() for kw in rules.get("partial_keywords", [])]
        if partial_kws and all(
            any(pk in part.lower() for pk in partial_kws)
            for part in detail.split("; ")
            if part.strip()
        ) and detail.strip():
            return "partial"

        return "full"

    # ================================================================ #
    #  Classification
    # ================================================================ #

    def _classify(self, score: float, label_cfg: List[Dict[str, Any]]) -> Tuple[str, str]:
        for entry in label_cfg:
            if score >= float(entry.get("min_score", 0)):
                return entry.get("name", "UNKNOWN"), entry.get("direction", "neutral")
        return "STRONG SELL", "sell"

    # ================================================================ #
    #  Portfolio summary
    # ================================================================ #

    def _build_portfolio_summary(
        self, signals: Dict[str, Dict[str, Any]], raw: Dict[str, Optional[Dict[str, Any]]]
    ) -> Dict[str, Any]:
        pcfg = self.profile.get("portfolio", {})
        top_n = int(pcfg.get("top_n", 3))

        sorted_assets = sorted(signals.items(), key=lambda x: x[1]["composite_score"], reverse=True)

        top_buys = []
        for asset, sig in sorted_assets[:top_n]:
            conviction = "high" if sig["composite_score"] >= float(pcfg.get("high_conviction_threshold", 70)) else "moderate"
            top_buys.append({"asset": asset, "score": sig["composite_score"], "label": sig["label"], "conviction": conviction})

        top_sells = []
        for asset, sig in sorted_assets[-top_n:]:
            top_sells.append({"asset": asset, "score": sig["composite_score"], "label": sig["label"]})

        # Market regime from Fear & Greed
        regime = "unknown"
        market_data = raw.get("market")
        if market_data:
            fg = market_data.get("data", {}).get("sentiment", {}).get("fear_greed_index")
            if fg is not None:
                fg = float(fg)
                thresholds = pcfg.get("regime_thresholds", {})
                if fg < float(thresholds.get("extreme_fear", 25)):
                    regime = "extreme_fear"
                elif fg < float(thresholds.get("fear", 45)):
                    regime = "fear"
                elif fg < float(thresholds.get("neutral", 55)):
                    regime = "neutral"
                elif fg < float(thresholds.get("greed", 75)):
                    regime = "greed"
                else:
                    regime = "extreme_greed"

        # Risk level from derivatives
        risk = "unknown"
        deriv_data = raw.get("derivatives")
        if deriv_data and market_data:
            avg_funding = self._avg_funding(deriv_data)
            fg_val = float(market_data.get("data", {}).get("sentiment", {}).get("fear_greed_index", 50))
            for level in pcfg.get("risk_levels", []):
                if avg_funding <= float(level.get("max_avg_funding", 1)) and fg_val >= float(level.get("min_fear_greed", 0)):
                    risk = level["name"]
                    break

        # Signal momentum
        improving = sum(1 for s in signals.values() if s.get("momentum") == "improving")
        degrading = sum(1 for s in signals.values() if s.get("momentum") == "degrading")
        if improving > degrading + 2:
            signal_momentum = "improving"
        elif degrading > improving + 2:
            signal_momentum = "degrading"
        else:
            signal_momentum = "mixed"

        return {
            "top_buys": top_buys,
            "top_sells": top_sells,
            "market_regime": regime,
            "risk_level": risk,
            "signal_momentum": signal_momentum,
            "assets_improving": improving,
            "assets_degrading": degrading,
        }

    def _avg_funding(self, deriv_result: Dict[str, Any]) -> float:
        per_asset = deriv_result.get("data", {}).get("per_asset", {})
        rates = []
        for a_data in per_asset.values():
            if isinstance(a_data, dict):
                fr = a_data.get("funding_rate")
                if fr is not None:
                    rates.append(abs(float(fr)))
        return sum(rates) / len(rates) if rates else 0.0

