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
        weights = self.profile.get("weights", {})
        scoring_cfg = self.profile.get("scoring", {})
        label_cfg = self.profile.get("labels", [])

        signals: Dict[str, Dict[str, Any]] = {}
        all_roles = ["whale", "technical", "derivatives", "narrative", "market"]
        non_whale_roles = ["technical", "derivatives", "narrative", "market"]

        # Dynamic reweighting config (from YAML)
        reweight_cfg = self.profile.get("reweighting", {})
        reweight_enabled = reweight_cfg.get("enabled", False)
        tier_multipliers = reweight_cfg.get("tier_multipliers", {"full": 1.0, "sparse": 0.5, "none": 0.0})
        no_data_kws = [kw.lower() for kw in reweight_cfg.get("no_data_keywords", ["no data", "no whale activity", "no scorer"])]
        full_data_kws = [kw.lower() for kw in reweight_cfg.get("full_data_keywords", ["accumulate", "sell"])]

        for asset in self.assets:
            # --- Phase 1: Score ALL dimensions first ---
            raw_scores: Dict[str, Tuple[float, str]] = {}
            for role in all_roles:
                agent_data = raw.get(role)
                rules = scoring_cfg.get(role, {})
                raw_scores[role] = self._score_dimension(role, asset, agent_data, rules)

            # --- Phase 2: Determine whale data tier ---
            whale_score, whale_detail = raw_scores["whale"]
            whale_detail_lower = whale_detail.lower()

            if not reweight_enabled:
                whale_data_tier = "full"  # reweighting disabled = always use full weight
            elif (
                any(kw in whale_detail_lower for kw in no_data_kws)
                or whale_detail_lower.startswith("error:")
            ):
                whale_data_tier = "none"
            elif any(kw in whale_detail_lower for kw in full_data_kws):
                whale_data_tier = "full"
            else:
                whale_data_tier = "sparse"

            # --- Phase 3: Calculate adjusted weights ---
            base_weights: Dict[str, float] = {}
            for role in all_roles:
                base_weights[role] = float(weights.get(role, 0.0))

            tier_mult = float(tier_multipliers.get(whale_data_tier, 1.0))
            if tier_mult >= 1.0:
                adjusted_weights = dict(base_weights)
            else:
                original_whale_w = base_weights["whale"]
                effective_whale_w = original_whale_w * tier_mult

                freed_weight = original_whale_w - effective_whale_w
                non_whale_sum = sum(base_weights[r] for r in non_whale_roles)

                adjusted_weights = {}
                adjusted_weights["whale"] = effective_whale_w
                for role in non_whale_roles:
                    if non_whale_sum > 0:
                        adjusted_weights[role] = base_weights[role] + freed_weight * (base_weights[role] / non_whale_sum)
                    else:
                        adjusted_weights[role] = base_weights[role]

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
                "whale_data_tier": whale_data_tier,
                "conviction_boost": conviction_applied,
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
    #  TECHNICAL scorer
    # ================================================================ #

    def _score_technical(self, asset: str, data: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[float, str]:
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

        # Long/short ratio
        ls_rules = rules.get("long_short", {})
        ls_ratio = asset_data.get("long_short_ratio")
        if ls_ratio is not None:
            sweet_min = float(ls_rules.get("sweet_spot_min", 0.55))
            sweet_max = float(ls_rules.get("sweet_spot_max", 0.65))
            overcrowded = float(ls_rules.get("overcrowded_above", 0.70))
            contrarian = float(ls_rules.get("contrarian_below", 0.45))

            if sweet_min <= ls_ratio <= sweet_max:
                score += float(ls_rules.get("sweet_spot_score", 40))
                details.append(f"L/S {ls_ratio:.2f} sweet spot")
            elif ls_ratio > overcrowded:
                score += float(ls_rules.get("overcrowded_score", 10))
                details.append(f"L/S {ls_ratio:.2f} overcrowded")
            elif ls_ratio < contrarian:
                score += float(ls_rules.get("contrarian_score", 35))
                details.append(f"L/S {ls_ratio:.2f} contrarian")
            else:
                score += float(ls_rules.get("default_score", 25))
                details.append(f"L/S {ls_ratio:.2f}")

        # Funding rate
        fund_rules = rules.get("funding", {})
        funding = asset_data.get("funding_rate")
        if funding is not None:
            if funding < 0:
                score += float(fund_rules.get("negative_score", 35))
                details.append(f"funding {funding:.5f} negative")
            elif funding < float(fund_rules.get("low_threshold", 0.0002)):
                score += float(fund_rules.get("low_score", 30))
                details.append("low funding")
            elif funding < float(fund_rules.get("moderate_threshold", 0.0005)):
                score += float(fund_rules.get("moderate_score", 15))
            else:
                score += float(fund_rules.get("high_score", 5))
                details.append("high funding")

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
        score = 0.0

        # --- Component 1: Volume score (0-30 points) ---
        # Uses normalised_score (0.0-1.0 vs rolling peak) * multiplier
        raw_score = float(asset_data.get("normalised_score", 0.0))
        volume_mult = float(rules.get("volume_multiplier", 30))
        volume_pts = raw_score * volume_mult
        score += volume_pts
        if raw_score > 0:
            total_mentions = int(asset_data.get("total_mentions", 0))
            details.append(f"vol {raw_score:.2f} ({total_mentions} mentions)")

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

        # --- Component 4: Trending bonus (0-10 points) ---
        trending = asset_data.get("trending_coingecko", False)
        trending_bonus = float(rules.get("trending_bonus", 10))
        if trending:
            score += trending_bonus
            details.append("trending")

        # --- Component 5: Influencer bonus (0-10 points) ---
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

        # --- Component 6: Multi-source confirmation (0-10 points) ---
        sources_with_data = int(asset_data.get("sources_with_data", 0))
        multi_threshold = int(rules.get("multi_source_threshold", 3))
        multi_bonus = float(rules.get("multi_source_bonus", 10))
        if sources_with_data >= multi_threshold:
            score += multi_bonus
            details.append(f"{sources_with_data} sources")

        max_score = float(rules.get("max_score", 100))
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

        return min(100.0, max(0.0, score)), "; ".join(details) if details else "no market data"

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
