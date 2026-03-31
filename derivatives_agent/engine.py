from __future__ import annotations

import json
import time as _time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from shared.base_agent import BaseAgent
from shared.profile_loader import load_profile, get_assets, get_threshold
from shared.storage import Storage


class DerivativesAgent(BaseAgent):
    """
    Collects derivatives data (long/short, funding, OI) from Binance Futures.
    Everything is driven by profiles/default.yaml — no hardcoded values.

    Source: Binance Futures API (free, no key).
    """

    def __init__(self, profile_path: str | None = None) -> None:
        default = Path(__file__).resolve().parent / "profiles" / "default.yaml"
        self.profile = load_profile(Path(profile_path) if profile_path else default)
        self.assets = get_assets(self.profile)
        self.timeout = int(self.profile.get("http_timeout_sec", 15))
        self.futures_map: Dict[str, str] = self.profile.get("binance_futures_map", {})
        self.binance_cfg = self.profile.get("binance", {})
        self.base_url = self.binance_cfg.get("base_url", "https://fapi.binance.com")
        self.endpoints = self.binance_cfg.get("endpoints", {})

        # New feature configs
        self.liquidations_cfg = self.profile.get("liquidations", {})
        self.top_traders_cfg = self.profile.get("top_traders", {})
        self.funding_history_cfg = self.profile.get("funding_history", {})
        self.scoring_cfg = self.profile.get("scoring", {})

        super().__init__(
            agent_name="derivatives_agent",
            profile_name=self.profile.get("name", "derivatives_default"),
        )

    def empty_data(self) -> Dict[str, Any]:
        return {
            "by_asset": {sym: self._empty_asset() for sym in self.assets},
            "summary": {
                "healthy_assets": [],
                "overcrowded_longs": [],
                "bearish_dominance": [],
                "high_funding": [],
            },
        }

    @staticmethod
    def _empty_asset() -> Dict[str, Any]:
        return {
            "long_pct": None,
            "short_pct": None,
            "long_short_ratio": None,
            "funding_rate": None,
            "open_interest_usd": None,
            "ls_status": "unknown",
            "funding_status": "unknown",
            "derivatives_condition": False,
            # Lead indicators (computed from historical snapshots)
            "funding_rate_change_4h": None,
            "funding_rate_change_24h": None,
            "oi_change_pct_4h": None,
            "oi_change_pct_24h": None,
            # Phase C1: Taker buy/sell ratio (E4 research)
            "taker_buy_sell_ratio": None,
            "taker_buy_vol": None,
            "taker_sell_vol": None,
            # Liquidation data
            "long_liquidations_usd": None,
            "short_liquidations_usd": None,
            "liquidation_ratio": None,
            "total_liquidations_usd": None,
            "liquidation_status": "unknown",
            # Top trader positions (smart money)
            "top_trader_long_pct": None,
            "top_trader_short_pct": None,
            "top_trader_ls_ratio": None,
            "smart_money_divergence": None,
            # Funding rate term structure
            "funding_rates_history": None,
            "funding_trend": None,
            "funding_extreme": None,
            # Composite derivatives score
            "derivatives_score": None,
        }

    def collect(self) -> Tuple[Dict[str, Any], List[str]]:
        data = self.empty_data()
        errors: List[str] = []

        # Thresholds from YAML
        ls_min = float(get_threshold(self.profile, "thresholds", "long_short_min", default=0.55))
        ls_max = float(get_threshold(self.profile, "thresholds", "long_short_max", default=0.65))
        fr_max = float(get_threshold(self.profile, "thresholds", "funding_rate_max", default=0.0005))
        ls_period = self.binance_cfg.get("long_short_period", "1h")
        ls_limit = int(self.binance_cfg.get("long_short_limit", 1))

        for sym in self.assets:
            futures_sym = self.futures_map.get(sym)
            if not futures_sym:
                errors.append(f"{sym}: no Binance futures mapping in profile")
                continue

            asset = data["by_asset"][sym]

            # --- Long/Short ratio ---
            try:
                ep = self.endpoints.get("long_short", "/futures/data/globalLongShortAccountRatio")
                url = f"{self.base_url}{ep}?symbol={futures_sym}&period={ls_period}&limit={ls_limit}"
                rows = self._get_json(url)
                if rows:
                    row = rows[0]
                    asset["long_pct"] = round(float(row["longAccount"]), 4)
                    asset["short_pct"] = round(float(row["shortAccount"]), 4)
                    asset["long_short_ratio"] = asset["long_pct"]
            except Exception as exc:
                errors.append(f"long_short {sym}: {exc}")

            # --- Funding rate ---
            try:
                ep = self.endpoints.get("funding_rate", "/fapi/v1/premiumIndex")
                url = f"{self.base_url}{ep}?symbol={futures_sym}"
                row = self._get_json(url)
                if isinstance(row, dict):
                    asset["funding_rate"] = float(row.get("lastFundingRate", 0.0))
            except Exception as exc:
                errors.append(f"funding {sym}: {exc}")

            # --- Open Interest ---
            try:
                ep = self.endpoints.get("open_interest", "/fapi/v1/openInterest")
                url = f"{self.base_url}{ep}?symbol={futures_sym}"
                row = self._get_json(url)
                if isinstance(row, dict):
                    asset["open_interest_usd"] = float(row.get("openInterest", 0.0))
            except Exception as exc:
                errors.append(f"oi {sym}: {exc}")

            # --- Taker Buy/Sell Ratio (Phase C1) ---
            try:
                ep = self.endpoints.get("taker_ratio", "/futures/data/takerlongshortRatio")
                tr_period = self.binance_cfg.get("taker_ratio_period", "1h")
                tr_limit = int(self.binance_cfg.get("taker_ratio_limit", 1))
                url = f"{self.base_url}{ep}?symbol={futures_sym}&period={tr_period}&limit={tr_limit}"
                rows = self._get_json(url)
                if rows:
                    row = rows[0]
                    asset["taker_buy_sell_ratio"] = round(float(row.get("buySellRatio", 0)), 4)
                    asset["taker_buy_vol"] = float(row.get("buyVol", 0))
                    asset["taker_sell_vol"] = float(row.get("sellVol", 0))
            except Exception as exc:
                errors.append(f"taker_ratio {sym}: {exc}")

            # --- Liquidation data ---
            if self.liquidations_cfg.get("enabled", True):
                try:
                    ep = self.liquidations_cfg.get("endpoint", "/fapi/v1/forceOrders")
                    liq_limit = int(self.liquidations_cfg.get("limit", 100))
                    url = f"{self.base_url}{ep}?symbol={futures_sym}&limit={liq_limit}"
                    rows = self._get_json(url)
                    if rows and isinstance(rows, list):
                        from datetime import datetime, timezone
                        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                        cutoff_ms = now_ms - 24 * 3600 * 1000  # 24h ago
                        long_liq = 0.0
                        short_liq = 0.0
                        for order in rows:
                            order_time = int(order.get("time", 0))
                            if order_time < cutoff_ms:
                                continue
                            qty = float(order.get("executedQty", 0))
                            price = float(order.get("averagePrice", order.get("price", 0)))
                            value = qty * price
                            side = order.get("side", "").upper()
                            # A SELL force order means a LONG was liquidated
                            if side == "SELL":
                                long_liq += value
                            elif side == "BUY":
                                short_liq += value
                        total_liq = long_liq + short_liq
                        asset["long_liquidations_usd"] = round(long_liq, 2)
                        asset["short_liquidations_usd"] = round(short_liq, 2)
                        asset["total_liquidations_usd"] = round(total_liq, 2)
                        if total_liq > 0:
                            asset["liquidation_ratio"] = round(long_liq / total_liq, 4)
                            if asset["liquidation_ratio"] > 0.7:
                                asset["liquidation_status"] = "cascade_risk_longs"
                            elif asset["liquidation_ratio"] < 0.3:
                                asset["liquidation_status"] = "cascade_risk_shorts"
                            else:
                                asset["liquidation_status"] = "balanced"
                        else:
                            asset["liquidation_ratio"] = 0.0
                            asset["liquidation_status"] = "minimal"
                    # Rate limit: sleep between liquidation calls
                    rate_sleep = float(self.liquidations_cfg.get("rate_limit_sleep", 1.0))
                    _time.sleep(rate_sleep)
                except Exception as exc:
                    errors.append(f"liquidations {sym}: {exc}")

            # --- Top Trader Positions (smart money) ---
            if self.top_traders_cfg.get("enabled", True):
                try:
                    ep = self.top_traders_cfg.get("endpoint", "/futures/data/topLongShortPositionRatio")
                    tt_period = self.top_traders_cfg.get("period", "4h")
                    url = f"{self.base_url}{ep}?symbol={futures_sym}&period={tt_period}&limit=1"
                    rows = self._get_json(url)
                    if rows and isinstance(rows, list):
                        row = rows[0]
                        tt_long = round(float(row.get("longAccount", 0)), 4)
                        tt_short = round(float(row.get("shortAccount", 0)), 4)
                        tt_ratio = round(float(row.get("longShortRatio", 0)), 4)
                        asset["top_trader_long_pct"] = tt_long
                        asset["top_trader_short_pct"] = tt_short
                        asset["top_trader_ls_ratio"] = tt_ratio
                        # Smart money divergence: top traders disagree with general accounts
                        general_ls = asset.get("long_short_ratio")
                        if general_ls is not None:
                            # Divergence if one group is long-heavy and the other is short-heavy
                            general_bullish = general_ls > 0.55
                            top_bullish = tt_long > 0.55
                            asset["smart_money_divergence"] = general_bullish != top_bullish
                        else:
                            asset["smart_money_divergence"] = False
                except Exception as exc:
                    errors.append(f"top_traders {sym}: {exc}")

            # --- Funding Rate Term Structure ---
            if self.funding_history_cfg.get("enabled", True):
                try:
                    ep = self.funding_history_cfg.get("endpoint", "/fapi/v1/fundingRate")
                    fh_limit = int(self.funding_history_cfg.get("limit", 3))
                    url = f"{self.base_url}{ep}?symbol={futures_sym}&limit={fh_limit}"
                    rows = self._get_json(url)
                    if rows and isinstance(rows, list):
                        rates = [float(r.get("fundingRate", 0)) for r in rows]
                        asset["funding_rates_history"] = [round(r, 8) for r in rates]
                        # Determine trend from oldest to newest
                        if len(rates) >= 2:
                            diffs = [rates[i] - rates[i - 1] for i in range(1, len(rates))]
                            avg_diff = sum(diffs) / len(diffs)
                            if avg_diff > 0.00005:
                                asset["funding_trend"] = "increasing"
                            elif avg_diff < -0.00005:
                                asset["funding_trend"] = "decreasing"
                            else:
                                asset["funding_trend"] = "stable"
                        else:
                            asset["funding_trend"] = "stable"
                        # Funding extreme check
                        funding_extreme_thresh = float(
                            self.scoring_cfg.get("thresholds", {}).get("funding_extreme", 0.001)
                        )
                        current_fr = asset.get("funding_rate")
                        if current_fr is not None:
                            asset["funding_extreme"] = (
                                current_fr > funding_extreme_thresh
                                or current_fr < -funding_extreme_thresh
                            )
                        else:
                            asset["funding_extreme"] = False
                except Exception as exc:
                    errors.append(f"funding_history {sym}: {exc}")

            # --- Score (thresholds from YAML) ---
            ls = asset.get("long_short_ratio")
            fr = asset.get("funding_rate")

            if ls is not None:
                if ls_min <= ls <= ls_max:
                    asset["ls_status"] = "healthy"
                elif ls > ls_max:
                    asset["ls_status"] = "overcrowded"
                else:
                    asset["ls_status"] = "bearish"

            if fr is not None:
                if 0 <= fr <= fr_max:
                    asset["funding_status"] = "normal"
                elif fr > fr_max:
                    asset["funding_status"] = "high"
                else:
                    asset["funding_status"] = "negative"

            asset["derivatives_condition"] = (
                asset["ls_status"] == "healthy"
                and asset["funding_status"] in ("normal", "negative", "unknown")
            )

            # --- Composite derivatives_score (0-100) ---
            asset["derivatives_score"] = self._compute_derivatives_score(asset)

        # --- Lead indicators: compute deltas from historical snapshots ---
        try:
            store = Storage()
            history = store.load_history("derivatives_agent", limit=20)
            if len(history) >= 2:
                self._compute_deltas(data, history, errors)
        except Exception as exc:
            errors.append(f"lead indicators: {exc}")

        # Build summary
        healthy, overcrowded, bearish, high_fr = [], [], [], []
        for sym, asset in data["by_asset"].items():
            s = asset["ls_status"]
            if s == "healthy":
                healthy.append(sym)
            elif s == "overcrowded":
                overcrowded.append(sym)
            elif s == "bearish":
                bearish.append(sym)
            if asset["funding_status"] == "high":
                high_fr.append(sym)

        data["summary"] = {
            "healthy_assets": healthy,
            "overcrowded_longs": overcrowded,
            "bearish_dominance": bearish,
            "high_funding": high_fr,
        }

        return data, errors

    # ------------------------------------------------------------------ #
    # Composite derivatives score (0-100)
    # ------------------------------------------------------------------ #

    def _compute_derivatives_score(self, asset: Dict[str, Any]) -> float:
        """Compute a 0-100 derivatives score from all sub-signals."""
        weights_cfg = self.scoring_cfg.get("weights", {})
        thresholds_cfg = self.scoring_cfg.get("thresholds", {})

        w_ls = float(weights_cfg.get("long_short", 0.20))
        w_funding = float(weights_cfg.get("funding", 0.25))
        w_oi = float(weights_cfg.get("open_interest", 0.15))
        w_liq = float(weights_cfg.get("liquidations", 0.20))
        w_taker = float(weights_cfg.get("taker_ratio", 0.20))

        overcrowded_long = float(thresholds_cfg.get("overcrowded_long", 0.65))
        shorts_dominating = float(thresholds_cfg.get("shorts_dominating", 0.55))
        funding_extreme_thresh = float(thresholds_cfg.get("funding_extreme", 0.001))

        # --- L/S score (continuous) ---
        ls = asset.get("long_short_ratio")
        if ls is not None:
            if ls > overcrowded_long:
                # More overcrowded = more bearish. 0.65→35, 0.80→10
                intensity = min((ls - overcrowded_long) / 0.15, 1.0)
                ls_score = 35.0 - intensity * 25.0  # 35→10
            elif ls < shorts_dominating:
                # More shorts = more contrarian bullish. 0.55→65, 0.40→90
                intensity = min((shorts_dominating - ls) / 0.15, 1.0)
                ls_score = 65.0 + intensity * 25.0  # 65→90
            else:
                # Sweet spot: linear interpolation centered at 50
                mid = (shorts_dominating + overcrowded_long) / 2.0
                if ls > mid:
                    ls_score = 50.0 - (ls - mid) / (overcrowded_long - mid) * 15.0
                else:
                    ls_score = 50.0 + (mid - ls) / (mid - shorts_dominating) * 15.0
        else:
            ls_score = 50.0

        # --- Funding score (continuous) ---
        fr = asset.get("funding_rate")
        if fr is not None:
            if fr > funding_extreme_thresh:
                # Extreme positive: very bearish. 0.001→20, 0.003→5
                intensity = min((fr - funding_extreme_thresh) / 0.002, 1.0)
                funding_score = 20.0 - intensity * 15.0  # 20→5
            elif fr > 0.0005:
                # High positive: bearish. 0.0005→35, 0.001→20
                intensity = (fr - 0.0005) / (funding_extreme_thresh - 0.0005) if funding_extreme_thresh > 0.0005 else 0
                funding_score = 35.0 - intensity * 15.0  # 35→20
            elif fr >= 0:
                # Normal positive: slightly bearish to neutral. 0→50, 0.0005→35
                intensity = fr / 0.0005 if fr > 0 else 0
                funding_score = 50.0 - intensity * 15.0  # 50→35
            elif fr > -funding_extreme_thresh:
                # Mild negative: shorts paying, bullish. -0.0005→65, 0→50
                intensity = abs(fr) / funding_extreme_thresh
                funding_score = 50.0 + intensity * 30.0  # 50→80
            else:
                # Extreme negative: strong short squeeze. -0.001→80, -0.003→95
                intensity = min((abs(fr) - funding_extreme_thresh) / 0.002, 1.0)
                funding_score = 80.0 + intensity * 15.0  # 80→95
        else:
            funding_score = 50.0

        # --- OI score (continuous) ---
        oi_change = asset.get("oi_change_pct_4h")
        if oi_change is not None:
            if oi_change > 0 and fr is not None and fr > 0.0005:
                # Rising OI + positive funding = overheating. More OI = worse
                intensity = min(oi_change / 10.0, 1.0)
                oi_score = 35.0 - intensity * 20.0  # 35→15
            elif oi_change > 0 and (fr is None or fr <= 0.0005):
                # Rising OI + low/neg funding = healthy growth
                intensity = min(oi_change / 10.0, 1.0)
                oi_score = 55.0 + intensity * 20.0  # 55→75
            elif oi_change < -5:
                # Sharp OI decline = deleveraging, bullish after flush
                intensity = min(abs(oi_change) / 15.0, 1.0)
                oi_score = 55.0 + intensity * 15.0  # 55→70
            else:
                oi_score = 50.0  # flat OI
        else:
            oi_score = 50.0

        # --- Liquidation score (with imbalance ratio for continuous scoring) ---
        liq_imb = asset.get("liquidation_imbalance")
        if liq_imb is not None:
            # liq_imbalance > 0 = more long liquidations (bearish)
            # liq_imbalance < 0 = more short liquidations (bullish)
            if liq_imb > 0.3:
                intensity = min(liq_imb / 1.0, 1.0)
                liq_score = 35.0 - intensity * 25.0  # 35→10 (cascade risk longs)
            elif liq_imb < -0.3:
                intensity = min(abs(liq_imb) / 1.0, 1.0)
                liq_score = 65.0 + intensity * 25.0  # 65→90 (cascade risk shorts)
            else:
                liq_score = 50.0  # balanced
        else:
            liq_status = asset.get("liquidation_status", "unknown")
            if liq_status == "cascade_risk_longs":
                liq_score = 20.0
            elif liq_status == "cascade_risk_shorts":
                liq_score = 80.0
            else:
                liq_score = 50.0

        # --- Taker score (continuous) ---
        taker = asset.get("taker_buy_sell_ratio")
        if taker is not None:
            if taker > 1.0:
                # Buyers dominating: more aggressive = more bullish
                intensity = min((taker - 1.0) / 0.3, 1.0)
                taker_score = 55.0 + intensity * 35.0  # 55→90
            else:
                # Sellers dominating: more aggressive = more bearish
                intensity = min((1.0 - taker) / 0.3, 1.0)
                taker_score = 45.0 - intensity * 35.0  # 45→10
        else:
            taker_score = 50.0

        # Weighted average
        score = (
            w_ls * ls_score
            + w_funding * funding_score
            + w_oi * oi_score
            + w_liq * liq_score
            + w_taker * taker_score
        )
        # Clamp 0-100
        return round(max(0.0, min(100.0, score)), 1)

    # ------------------------------------------------------------------ #
    # Lead indicator computation
    # ------------------------------------------------------------------ #

    def _compute_deltas(
        self,
        current_data: Dict[str, Any],
        history: List[Dict[str, Any]],
        errors: List[str],
    ) -> None:
        """Compute funding rate change and OI change from historical snapshots.

        History is sorted newest-first. We find snapshots closest to 4h and 24h
        ago and compute deltas against the current data.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)

        # Parse timestamps from history and index by age in hours
        timed: List[Tuple[float, Dict[str, Any]]] = []
        for snap in history:
            ts_str = snap.get("timestamp") or ""
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                age_hours = (now - ts).total_seconds() / 3600
                by_asset = snap.get("data", {}).get("data", {}).get("by_asset", {})
                if not by_asset:
                    by_asset = snap.get("data", {}).get("by_asset", {})
                if by_asset:
                    timed.append((age_hours, by_asset))
            except Exception:
                continue

        if not timed:
            return

        # Find closest snapshot to each target window (with max tolerance)
        targets = {"4h": (4.0, 1.5, 12.0), "24h": (24.0, 8.0, 48.0)}
        closest: Dict[str, Optional[Dict[str, Any]]] = {}
        for label, (target_h, min_age, max_age) in targets.items():
            best = None
            best_diff = float("inf")
            for age_h, by_asset in timed:
                if age_h < min_age or age_h > max_age:
                    continue
                diff = abs(age_h - target_h)
                if diff < best_diff:
                    best = by_asset
                    best_diff = diff
            closest[label] = best

        # Compute deltas per asset
        for sym in self.assets:
            asset = current_data["by_asset"].get(sym, {})
            cur_fr = asset.get("funding_rate")
            cur_oi = asset.get("open_interest_usd")

            for label, snap_data in closest.items():
                if snap_data is None:
                    continue
                prev = snap_data.get(sym, {})
                prev_fr = prev.get("funding_rate")
                prev_oi = prev.get("open_interest_usd")

                # Funding rate change (absolute delta)
                if cur_fr is not None and prev_fr is not None:
                    delta = cur_fr - prev_fr
                    asset[f"funding_rate_change_{label}"] = round(delta, 8)

                # OI change (percentage)
                if cur_oi is not None and prev_oi is not None and prev_oi > 0:
                    pct = ((cur_oi - prev_oi) / prev_oi) * 100
                    asset[f"oi_change_pct_{label}"] = round(pct, 2)

        n_with_delta = sum(
            1 for sym in self.assets
            if current_data["by_asset"].get(sym, {}).get("funding_rate_change_4h") is not None
        )
        errors.append(f"lead indicators: {n_with_delta}/{len(self.assets)} assets have 4h deltas")

    # ------------------------------------------------------------------ #
    # HTTP helper
    # ------------------------------------------------------------------ #

    def _get_json(self, url: str, retries: int = 2) -> Any:
        last_exc = None
        for attempt in range(retries + 1):
            try:
                req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
                with urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    _time.sleep(1 * (attempt + 1))  # backoff: 1s, 2s
        raise last_exc
