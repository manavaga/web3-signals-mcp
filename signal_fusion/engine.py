from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from shared.profile_loader import load_profile
from shared.storage import Storage


class SignalFusion:
    """
    Fuses 5 agent outputs into composite scored signals per asset.

    All scoring rules, weights, labels, and thresholds live in the YAML profile.
    This engine contains zero domain logic — only generic arithmetic driven by config.
    """

    def __init__(self, profile_path: str | None = None, db_path: str = "signals.db") -> None:
        default = Path(__file__).resolve().parent / "profiles" / "default.yaml"
        self.profile = load_profile(Path(profile_path) if profile_path else default)
        self.assets: List[str] = [a.upper() for a in self.profile.get("assets", [])]
        self.store = Storage(db_path)
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

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
            except Exception as exc:
                errors.append(f"optimizer load failed: {exc}")

        scoring_cfg = self.profile.get("scoring", {})
        label_cfg = self.profile.get("labels", [])

        signals: Dict[str, Dict[str, Any]] = {}
        all_roles = ["whale", "technical", "derivatives", "narrative", "market"]

        # Load delta scorer (YAML-driven change detection)
        delta_scorer = None
        prev_signals: Dict[str, Dict] = {}
        delta_cfg = self.profile.get("delta_scoring", {})
        if delta_cfg.get("enabled", False):
            try:
                from signal_fusion.delta import DeltaScorer
                delta_scorer = DeltaScorer(self.profile)
                # Load previous fusion run's signals for delta comparison
                prev_run = self.store.load_latest("signal_fusion")
                if prev_run:
                    prev_signals = prev_run.get("data", {}).get("signals", {})
            except Exception as exc:
                errors.append(f"delta scorer: {exc}")

        # Dynamic reweighting config (from YAML)
        reweight_cfg = self.profile.get("reweighting", {})
        reweight_enabled = reweight_cfg.get("enabled", False)
        tier_multipliers = reweight_cfg.get("tier_multipliers", {"full": 1.0, "partial": 0.5, "none": 0.0})
        agent_reweight_rules = reweight_cfg.get("agents", {})

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

            # Keep whale_data_tier for backward compatibility in output
            whale_data_tier = data_tiers.get("whale", "full")

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

            base_weights: Dict[str, float] = {}
            for role in all_roles:
                base_weights[role] = float(weights.get(role, 0.0))

            # Direction gating: zero out dimensions toxic in specific directions
            gating_cfg = self.profile.get("direction_gating", {})
            if gating_cfg.get("enabled", False):
                gates = gating_cfg.get("gates", {})
                direction_lean = "bullish" if raw_avg > 50 else "bearish" if raw_avg < 50 else "neutral"
                for role in all_roles:
                    role_gates = gates.get(role, {})
                    gate_key = f"{direction_lean}_gate"
                    if role_gates.get(gate_key, False):
                        base_weights[role] = 0.0
                # Renormalize to sum to 1.0
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

            # --- Phase 5: Conviction multiplier ---
            # When 3+ dimensions agree on direction, amplify composite away from 50.
            # This breaks the "everything is neutral" clustering problem.
            conviction_cfg = self.profile.get("conviction", {})
            if conviction_cfg.get("enabled", True):
                min_agreeing = int(conviction_cfg.get("min_agreeing_dimensions", 3))
                boost_factor = float(conviction_cfg.get("boost_factor", 1.25))
                center = 50.0

                bullish_count = sum(1 for r in all_roles if raw_scores[r][0] > 55)
                bearish_count = sum(1 for r in all_roles if raw_scores[r][0] < 45)

                if bullish_count >= min_agreeing and composite > center:
                    # Amplify distance from center
                    distance = composite - center
                    composite = round(center + distance * boost_factor, 1)
                elif bearish_count >= min_agreeing and composite < center:
                    distance = center - composite
                    composite = round(center - distance * boost_factor, 1)

                # Clamp to 0-100
                composite = round(max(0.0, min(100.0, composite)), 1)
                conviction_applied = bullish_count >= min_agreeing or bearish_count >= min_agreeing
            else:
                bullish_count = 0
                bearish_count = 0
                conviction_applied = False

            # --- Phase 5b: Delta (change-detection) scoring ---
            # Blend absolute composite with delta composite based on dimension changes.
            delta_details = {}
            if delta_scorer and delta_scorer.is_enabled():
                prev_dims = prev_signals.get(asset, {}).get("dimensions")
                delta_composite, delta_details = delta_scorer.compute_delta_composite(
                    asset, dimensions, prev_dims
                )
                if delta_composite is not None:
                    composite = delta_scorer.blend(composite, delta_composite)

            # --- Phase 6: Abstain check ---
            # When composite is too close to 50 (no edge), force neutral.
            abstain_cfg = self.profile.get("abstain", {})
            abstain_applied = False
            if abstain_cfg.get("enabled", False):
                min_distance = float(abstain_cfg.get("min_distance_from_center", 8))
                if abs(composite - 50.0) < min_distance:
                    abstain_applied = True
                    label_name = abstain_cfg.get("abstain_label", "INSUFFICIENT EDGE")
                    direction = "neutral"
                else:
                    label_name, direction = self._classify(composite, label_cfg)
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

            signals[asset] = {
                "composite_score": composite,
                "label": label_name,
                "direction": direction,
                "dimensions": dimensions,
                "momentum": momentum,
                "prev_score": round(prev_score, 1) if prev_score is not None else None,
                "data_tiers": data_tiers,
                "conviction_boost": conviction_applied,
                "abstain": abstain_applied,
                "delta": delta_details if delta_details else None,
            }

            # Store current score for next momentum comparison
            self.store.save_kv("fusion_scores", asset, composite)

        # Portfolio summary
        portfolio = self._build_portfolio_summary(signals, raw)

        # LLM insights
        llm_cfg = self.profile.get("llm_insights", {})
        if llm_cfg.get("enabled", False) and self.anthropic_key:
            try:
                prev_run = self.store.load_latest("signal_fusion")
                prev_signals = prev_run.get("data", {}).get("signals", {}) if prev_run else {}

                if llm_cfg.get("portfolio_summary", False):
                    portfolio["llm_insight"] = self._llm_portfolio_insight(
                        portfolio, signals, prev_signals, llm_cfg
                    )

                if llm_cfg.get("per_asset", False):
                    # Only generate for top buys + top sells (not all 20 — saves cost)
                    top_assets = set()
                    for item in portfolio.get("top_buys", []):
                        top_assets.add(item["asset"])
                    for item in portfolio.get("top_sells", []):
                        top_assets.add(item["asset"])

                    for asset in top_assets:
                        sig = signals.get(asset, {})
                        prev_sig = prev_signals.get(asset, {})
                        insight = self._llm_asset_insight(asset, sig, prev_sig, llm_cfg)
                        signals[asset]["llm_insight"] = insight

            except Exception as exc:
                errors.append(f"llm_insights: {exc}")
        elif llm_cfg.get("enabled", False) and not self.anthropic_key:
            errors.append("llm_insights: ANTHROPIC_API_KEY not set")

        duration_ms = int((time.perf_counter() - start) * 1000)

        result = {
            "agent": "signal_fusion",
            "profile": self.profile.get("name", "signal_fusion_default"),
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
    #  WHALE scorer
    # ================================================================ #

    def _score_whale(self, asset: str, data: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[float, str]:
        base_score = float(rules.get("base_score", 50))
        score = base_score
        details: List[str] = []

        # Per-asset moves
        by_asset = data.get("by_asset", {})
        asset_moves = by_asset.get(asset, [])
        accum_count = sum(1 for m in asset_moves if m.get("action") == "accumulate")
        sell_count = sum(1 for m in asset_moves if m.get("action") == "sell")

        scoring_mode = str(rules.get("scoring_mode", "ratio"))
        directional = accum_count + sell_count

        if scoring_mode == "ratio" and directional >= int(rules.get("min_directional_moves", 2)):
            # Ratio-based: accumulate/(accumulate+sell) mapped to 0-max points
            ratio = accum_count / directional
            max_pts = float(rules.get("ratio_max_points", 60))
            # ratio 1.0 → max_pts, ratio 0.5 → max_pts/2, ratio 0.0 → 0
            score = ratio * max_pts
            details.append(f"{accum_count} accumulate, {sell_count} sell (ratio {ratio:.0%})")
        elif directional > 0:
            # Legacy per-move scoring (fallback)
            score += accum_count * float(rules.get("accumulate_points", 10))
            score += sell_count * float(rules.get("sell_points", -10))
            details.append(f"{accum_count} accumulate, {sell_count} sell")

        # Exchange flow (adds up to ±10 on top)
        summary = data.get("summary", {})
        net_dir = summary.get("net_exchange_direction", "")
        if net_dir == "net_outflow":
            score += float(rules.get("exchange_outflow_bonus", 10))
            details.append("exchange outflow")
        elif net_dir == "net_inflow":
            score += float(rules.get("exchange_inflow_penalty", -10))
            details.append("exchange inflow")

        # Whale wallet signals (adds up to ±8 per wallet)
        wallet_signals = summary.get("whale_wallet_signals", [])
        for ws in wallet_signals:
            if "accumulating" in ws.lower():
                score += float(rules.get("whale_wallet_accumulating_bonus", 8))
            elif "reducing" in ws.lower():
                score += float(rules.get("whale_wallet_reducing_penalty", -8))

        score = max(float(rules.get("min_score", 0)), min(float(rules.get("max_score", 100)), score))
        return score, "; ".join(details) if details else "no whale activity"

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
        # Apply asset tier overrides (momentum vs contrarian)
        tier_cfg = self.profile.get("asset_tiers", {})
        if tier_cfg.get("enabled", False):
            tier = self._get_asset_tier(asset)
            overrides = tier_cfg.get("technical_overrides", {}).get(tier, {})
            if overrides:
                rules = self._merge_rules(rules, overrides)

        by_asset = data.get("by_asset", {})
        asset_data = by_asset.get(asset, {})
        if not asset_data:
            return 50.0, "no data"

        score = 0.0
        details: List[str] = []

        # RSI
        rsi_rules = rules.get("rsi", {})
        rsi = asset_data.get("rsi_14")
        if rsi is not None:
            oversold = float(rsi_rules.get("oversold_below", 30))
            overbought = float(rsi_rules.get("overbought_above", 70))
            if rsi < oversold:
                score += float(rsi_rules.get("oversold_score", 30))
                details.append(f"RSI {rsi:.0f} oversold")
            elif rsi > overbought:
                score += float(rsi_rules.get("overbought_score", 10))
                details.append(f"RSI {rsi:.0f} overbought")
            else:
                # Linear interpolation between oversold and overbought
                ratio = (rsi - oversold) / (overbought - oversold)
                min_s = float(rsi_rules.get("neutral_min_score", 15))
                max_s = float(rsi_rules.get("neutral_max_score", 40))
                score += min_s + ratio * (max_s - min_s)
                details.append(f"RSI {rsi:.0f}")

        # MACD
        macd_rules = rules.get("macd", {})
        macd_val = asset_data.get("macd_line")
        macd_signal = asset_data.get("macd_signal")
        if macd_val is not None and macd_signal is not None:
            if macd_val > macd_signal:
                score += float(macd_rules.get("bullish_cross_points", 20))
                details.append("MACD bullish")
            else:
                score += float(macd_rules.get("bearish_cross_points", 0))
                details.append("MACD bearish")

        # Moving averages
        ma_rules = rules.get("ma", {})
        price = asset_data.get("price")
        ma7 = asset_data.get("ma_7d")
        ma30 = asset_data.get("ma_30d")
        if price is not None and ma7 is not None:
            if price > ma7:
                score += float(ma_rules.get("above_ma7_points", 10))
            else:
                score += float(ma_rules.get("below_ma7_points", 0))
        if price is not None and ma30 is not None:
            if price > ma30:
                score += float(ma_rules.get("above_ma30_points", 10))
                details.append("above MA30")
            else:
                score += float(ma_rules.get("below_ma30_points", 0))

        # Trend — use 30d as primary (macro trend), 7d as secondary
        trend_rules = rules.get("trend", {})
        trend_30d = asset_data.get("trend_30d", "")
        trend_7d = asset_data.get("trend_7d", "")
        # Combine: if both bullish = "bullish", if both bearish = "bearish", else use 30d
        trend = trend_30d if trend_30d else trend_7d
        if trend == "bullish":
            score += float(trend_rules.get("bullish_points", 20))
            details.append("trend bullish")
        elif trend == "bearish":
            score += float(trend_rules.get("bearish_points", 0))
            details.append("trend bearish")
        else:
            score += float(trend_rules.get("neutral_points", 10))

        return min(100.0, max(0.0, score)), "; ".join(details) if details else "no tech data"

    # ================================================================ #
    #  DERIVATIVES scorer
    # ================================================================ #

    def _score_derivatives(self, asset: str, data: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[float, str]:
        by_asset = data.get("by_asset", {})
        asset_data = by_asset.get(asset, {})
        if not asset_data:
            return 50.0, "no data"

        score = 0.0
        details: List[str] = []

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
                score += float(ls_rules.get("very_overcrowded_score", 3))
                details.append(f"L/S {ls_ratio:.2f} very overcrowded")
                ls_tier = "very_overcrowded"
            elif sweet_min <= ls_ratio <= sweet_max:
                score += float(ls_rules.get("sweet_spot_score", 40))
                details.append(f"L/S {ls_ratio:.2f} sweet spot")
                ls_tier = "sweet_spot"
            elif ls_ratio > overcrowded:
                score += float(ls_rules.get("overcrowded_score", 10))
                details.append(f"L/S {ls_ratio:.2f} overcrowded")
                ls_tier = "overcrowded"
            elif ls_ratio < contrarian:
                score += float(ls_rules.get("contrarian_score", 35))
                details.append(f"L/S {ls_ratio:.2f} contrarian")
                ls_tier = "contrarian"
            else:
                score += float(ls_rules.get("default_score", 25))
                details.append(f"L/S {ls_ratio:.2f}")
                ls_tier = "default"

        # Funding rate
        fund_rules = rules.get("funding", {})
        funding = asset_data.get("funding_rate")
        funding_tier = None  # Track for combo scoring
        if funding is not None:
            if funding < 0:
                score += float(fund_rules.get("negative_score", 35))
                details.append(f"funding {funding:.5f} negative")
                funding_tier = "negative"
            elif funding < float(fund_rules.get("low_threshold", 0.0002)):
                score += float(fund_rules.get("low_score", 30))
                details.append("low funding")
                funding_tier = "low"
            elif funding < float(fund_rules.get("moderate_threshold", 0.0005)):
                score += float(fund_rules.get("moderate_score", 15))
                funding_tier = "moderate"
            else:
                score += float(fund_rules.get("high_score", 5))
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

        return min(100.0, max(0.0, score)), "; ".join(details) if details else "no deriv data"

    # ================================================================ #
    #  NARRATIVE scorer
    # ================================================================ #

    def _score_narrative(self, asset: str, data: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[float, str]:
        by_asset = data.get("by_asset", {})
        asset_data = by_asset.get(asset, {})
        if not asset_data:
            return 50.0, "no data"

        details: List[str] = []

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
        per_asset = data.get("per_asset", {})
        asset_data = per_asset.get(asset, {})
        details: List[str] = []
        score = 0.0

        # Price change
        pc_rules = rules.get("price_change", {})
        change_24h = asset_data.get("change_24h_pct")
        if change_24h is not None:
            strong_pos = float(pc_rules.get("strong_positive_above", 5.0))
            pos = float(pc_rules.get("positive_above", 0.0))
            mild_neg = float(pc_rules.get("mild_negative_above", -5.0))

            if change_24h > strong_pos:
                score += float(pc_rules.get("strong_positive_score", 40))
                details.append(f"+{change_24h:.1f}% strong")
            elif change_24h > pos:
                score += float(pc_rules.get("positive_score", 30))
                details.append(f"+{change_24h:.1f}%")
            elif change_24h > mild_neg:
                score += float(pc_rules.get("mild_negative_score", 20))
                details.append(f"{change_24h:.1f}%")
            else:
                score += float(pc_rules.get("strong_negative_score", 10))
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

        # Fear & Greed (global, same for all assets)
        fg_rules = rules.get("fear_greed", {})
        sentiment = data.get("sentiment", {})
        fg_value = sentiment.get("fear_greed_index")
        if fg_value is not None:
            fg = float(fg_value)
            if fg < float(fg_rules.get("extreme_fear_below", 25)):
                score += float(fg_rules.get("extreme_fear_score", 30))
                details.append(f"F&G {fg:.0f} extreme fear")
            elif fg < float(fg_rules.get("fear_below", 45)):
                score += float(fg_rules.get("fear_score", 25))
                details.append(f"F&G {fg:.0f} fear")
            elif fg < float(fg_rules.get("neutral_below", 55)):
                score += float(fg_rules.get("neutral_score", 15))
            elif fg < float(fg_rules.get("greed_below", 75)):
                score += float(fg_rules.get("greed_score", 10))
            else:
                score += float(fg_rules.get("extreme_greed_score", 5))
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

        return min(100.0, max(0.0, score)), "; ".join(details) if details else "no market data"

    # ================================================================ #
    #  Data-tier detection (universal reweighting)
    # ================================================================ #

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

    # ================================================================ #
    #  LLM insight generation (Claude Haiku)
    # ================================================================ #

    def _llm_call(self, messages: List[Dict[str, str]], cfg: Dict[str, Any]) -> str:
        """Call Anthropic Messages API."""
        from urllib.error import HTTPError

        url = "https://api.anthropic.com/v1/messages"
        system_prompt = cfg.get("system_prompt", "").strip()
        payload = {
            "model": cfg.get("model", "claude-haiku-4-5-20251001"),
            "max_tokens": int(cfg.get("max_tokens", 1024)),
            "messages": messages,
        }
        if system_prompt:
            payload["system"] = system_prompt

        # Ensure payload is JSON-safe (replace None, NaN, etc.)
        data = json.dumps(payload, default=str).encode()
        req = Request(url, data=data, headers={
            "Content-Type": "application/json",
            "x-api-key": self.anthropic_key,
            "anthropic-version": "2023-06-01",
        })
        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
            content = result.get("content", [])
            return content[0].get("text", "") if content else ""
        except HTTPError as exc:
            # Capture the response body for better diagnostics
            body = ""
            try:
                body = exc.read().decode()[:500]
            except Exception:
                pass
            print(f"LLM call failed ({exc.code}): {body}")
            return f"[LLM unavailable: HTTP {exc.code} — {body[:200]}]"
        except Exception as exc:
            # Log but don't crash — LLM insights are optional
            return f"[LLM unavailable: {exc}]"

    def _llm_portfolio_insight(
        self,
        portfolio: Dict[str, Any],
        signals: Dict[str, Dict[str, Any]],
        prev_signals: Dict[str, Dict[str, Any]],
        cfg: Dict[str, Any],
    ) -> str:
        # Build compact context for the LLM
        context = {
            "portfolio": portfolio,
            "top_signals": {},
            "prev_top_signals": {},
        }
        # Include top buys + sells detail
        for item in portfolio.get("top_buys", []) + portfolio.get("top_sells", []):
            asset = item["asset"]
            sig = signals.get(asset, {})
            context["top_signals"][asset] = {
                "score": sig.get("composite_score"),
                "dimensions": sig.get("dimensions"),
                "momentum": sig.get("momentum"),
            }
            if cfg.get("include_previous_run") and asset in prev_signals:
                context["prev_top_signals"][asset] = {
                    "score": prev_signals[asset].get("composite_score"),
                    "dimensions": prev_signals[asset].get("dimensions"),
                }

        prompt = (
            f"Current fusion data:\n{json.dumps(context, indent=1)}\n\n"
            f"Give a portfolio-level market summary: what's the dominant signal, "
            f"key cross-dimensional patterns, and 1-2 actionable takeaways. "
            f"Compare with previous run if available. Max 5 sentences."
        )

        return self._llm_call([{"role": "user", "content": prompt}], cfg)

    def _llm_asset_insight(
        self,
        asset: str,
        signal: Dict[str, Any],
        prev_signal: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> str:
        context = {
            "asset": asset,
            "current": {
                "score": signal.get("composite_score"),
                "label": signal.get("label"),
                "dimensions": signal.get("dimensions"),
                "momentum": signal.get("momentum"),
            },
        }
        if cfg.get("include_previous_run") and prev_signal:
            context["previous"] = {
                "score": prev_signal.get("composite_score"),
                "dimensions": prev_signal.get("dimensions"),
            }

        prompt = (
            f"Signal data for {asset}:\n{json.dumps(context, indent=1)}\n\n"
            f"Give a concise insight: what's the dominant signal across dimensions, "
            f"any notable cross-dimensional patterns, and one actionable takeaway. "
            f"Compare with previous data if available. Max 3 sentences."
        )

        return self._llm_call([{"role": "user", "content": prompt}], cfg)
