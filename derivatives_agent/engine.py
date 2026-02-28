from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.request import Request, urlopen

from shared.base_agent import BaseAgent
from shared.profile_loader import load_profile, get_assets, get_threshold


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
    # HTTP helper
    # ------------------------------------------------------------------ #

    def _get_json(self, url: str, retries: int = 2) -> Any:
        import time as _time
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
