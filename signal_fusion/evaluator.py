"""Signal evaluation engine — measures accuracy using TP/SL hit within 48h.

Evaluates both directional (buy/sell) and neutral signals:
- Directional: TP hit first = correct, SL hit first = wrong, neither = use 48h return
- Neutral: price stays within ATR band = correct, breaks out = wrong

Primary metric: CWA (Coverage-Weighted Accuracy)
  CWA = (correct / total_signals) * coverage_factor
  coverage_factor = min(directional_signals / total_signals / target_coverage, 1.0)
"""

from __future__ import annotations

import json
import time
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple


BINANCE_SYMBOLS: Dict[str, str] = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "BNB": "BNBUSDT", "XRP": "XRPUSDT", "ADA": "ADAUSDT",
    "AVAX": "AVAXUSDT", "DOT": "DOTUSDT", "MATIC": "POLUSDT",
    "LINK": "LINKUSDT", "UNI": "UNIUSDT", "ATOM": "ATOMUSDT",
    "LTC": "LTCUSDT", "FIL": "FILUSDT", "NEAR": "NEARUSDT",
    "APT": "APTUSDT", "ARB": "ARBUSDT", "OP": "OPUSDT",
    "INJ": "INJUSDT", "SUI": "SUIUSDT",
}

# ATR multipliers for neutral signal evaluation bands
NEUTRAL_ATR_MULTIPLIERS: Dict[str, float] = {
    "BTC": 1.0, "ETH": 1.0,
    "SOL": 1.0, "BNB": 1.0, "XRP": 1.0, "ADA": 1.0, "LINK": 1.0,
    "LTC": 1.0, "DOT": 1.0, "AVAX": 1.0,
    "MATIC": 1.2, "UNI": 1.2, "FIL": 1.2, "NEAR": 1.2, "APT": 1.2,
    "ARB": 1.2, "SUI": 1.2,
    "ATOM": 1.0, "OP": 1.2, "INJ": 1.5,
}


class SignalEvaluator:
    """Evaluates signal accuracy using TP/SL hit within 48h timeframe."""

    def __init__(self, store: Any) -> None:
        self.store = store

    # ------------------------------------------------------------------ #
    #  Save signal for later evaluation
    # ------------------------------------------------------------------ #

    def save_for_evaluation(self, signal: dict, asset: str) -> str:
        """Save a signal for future evaluation. Returns signal ID."""
        signal_id = str(uuid.uuid4())
        direction = signal.get("direction", "neutral")

        record = {
            "id": signal_id,
            "asset": asset,
            "direction": direction,
            "composite_score": signal.get("composite_score"),
            "entry_price": signal.get("entry_price"),
            "target_price": signal.get("target_price"),
            "stop_loss": signal.get("stop_loss"),
            "signal_timestamp": datetime.now(timezone.utc).isoformat(),
            "features": json.dumps(signal.get("_features", {})),
        }

        self.store.save_signal_for_evaluation(record)
        return signal_id

    # ------------------------------------------------------------------ #
    #  Evaluate all pending signals
    # ------------------------------------------------------------------ #

    def evaluate_pending(self) -> List[Dict]:
        """Evaluate all pending signals older than 48h."""
        pending = self.store.get_pending_evaluations()
        results: List[Dict] = []

        for signal in pending:
            try:
                result = self._evaluate_one(signal)
                if result:
                    self.store.evaluate_signal(signal["id"], result)
                    results.append(result)
            except Exception as exc:
                print(f"[evaluator] error evaluating {signal.get('asset')}: {exc}")

        return results

    def _evaluate_one(self, signal: dict) -> Optional[dict]:
        """Evaluate a single signal."""
        asset = signal["asset"]
        direction = signal["direction"]
        signal_ts = signal["signal_timestamp"]

        # Fetch 48h of price data
        price_data = self._fetch_48h_prices(asset, signal_ts)
        if not price_data or not price_data.get("prices"):
            return None

        if direction in ("buy", "sell"):
            return self._evaluate_directional(signal, price_data)
        else:
            return self._evaluate_neutral(signal, price_data)

    # ------------------------------------------------------------------ #
    #  Directional evaluation (buy/sell)
    # ------------------------------------------------------------------ #

    def _evaluate_directional(self, signal: dict, price_data: dict) -> dict:
        """Evaluate a BUY or SELL signal.

        Priority:
        1. TP hit before SL → correct (1.0)
        2. SL hit before TP → wrong (0.0)
        3. Neither hit → use 48h return direction (0.7 if correct, 0.0 if wrong)
        """
        direction = signal["direction"]
        entry = signal.get("entry_price")
        target = signal.get("target_price")
        stop = signal.get("stop_loss")

        prices = price_data["prices"]  # list of (timestamp, high, low, close)
        highest = price_data["highest"]
        lowest = price_data["lowest"]
        close_48h = price_data["close_48h"]

        target_hit_at = None
        sl_hit_at = None

        if entry and target and stop:
            # Walk through candles chronologically
            for ts, high, low, close in prices:
                if direction == "buy":
                    if high >= target and target_hit_at is None:
                        target_hit_at = ts
                    if low <= stop and sl_hit_at is None:
                        sl_hit_at = ts
                else:  # sell
                    if low <= target and target_hit_at is None:
                        target_hit_at = ts
                    if high >= stop and sl_hit_at is None:
                        sl_hit_at = ts

                # If both hit in same candle, check which is more likely first
                if target_hit_at == sl_hit_at and target_hit_at == ts:
                    if direction == "buy":
                        # In same candle, if close > entry, likely TP hit first
                        if close >= entry:
                            sl_hit_at = None
                        else:
                            target_hit_at = None
                    else:
                        if close <= entry:
                            sl_hit_at = None
                        else:
                            target_hit_at = None

        # Determine outcome
        if target_hit_at and (sl_hit_at is None or target_hit_at <= sl_hit_at):
            outcome = "target_hit"
            score = 1.0
        elif sl_hit_at and (target_hit_at is None or sl_hit_at < target_hit_at):
            outcome = "stop_loss_hit"
            score = 0.0
        else:
            # Neither hit — use 48h return
            if entry and close_48h:
                return_pct = (close_48h - entry) / entry * 100
                if direction == "buy":
                    if return_pct > 0:
                        outcome = "expired_correct"
                        score = 0.7
                    else:
                        outcome = "expired_wrong"
                        score = 0.0
                else:
                    if return_pct < 0:
                        outcome = "expired_correct"
                        score = 0.7
                    else:
                        outcome = "expired_wrong"
                        score = 0.0
            else:
                outcome = "expired_unknown"
                score = 0.0

        # Compute actual move for ML training
        actual_move_pct = None
        if entry and close_48h:
            actual_move_pct = (close_48h - entry) / entry * 100

        return {
            "target_hit_at": target_hit_at,
            "stop_loss_hit_at": sl_hit_at,
            "price_at_48h": close_48h,
            "highest_price_48h": highest,
            "lowest_price_48h": lowest,
            "outcome": outcome,
            "score": score,
            "actual_move_pct": actual_move_pct,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------ #
    #  Neutral evaluation
    # ------------------------------------------------------------------ #

    def _evaluate_neutral(self, signal: dict, price_data: dict) -> dict:
        """Evaluate a neutral / INSUFFICIENT EDGE signal.

        Correct if price stayed within ATR-based band.
        Wrong if price moved beyond the band.
        """
        asset = signal["asset"]
        entry = signal.get("entry_price")
        highest = price_data["highest"]
        lowest = price_data["lowest"]
        close_48h = price_data["close_48h"]

        if not entry or entry <= 0:
            return {
                "outcome": "expired_unknown",
                "score": 0.0,
                "price_at_48h": close_48h,
                "highest_price_48h": highest,
                "lowest_price_48h": lowest,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
            }

        # Calculate ATR band for neutral evaluation
        # Use stored ATR or estimate from price range
        atr_pct = 3.0  # default 3%
        try:
            tech_latest = self.store.load_latest("technical_agent")
            if tech_latest:
                asset_data = tech_latest.get("data", {}).get("by_asset", {}).get(asset, {})
                stored_atr_pct = asset_data.get("atr_pct")
                if stored_atr_pct:
                    atr_pct = float(stored_atr_pct)
        except Exception:
            pass

        multiplier = NEUTRAL_ATR_MULTIPLIERS.get(asset, 1.0)
        band_pct = atr_pct * multiplier

        upper_band = entry * (1 + band_pct / 100)
        lower_band = entry * (1 - band_pct / 100)

        # Price stayed within band?
        if lowest >= lower_band and highest <= upper_band:
            outcome = "neutral_correct"
            score = 0.8  # correct neutral = good but slightly less than perfect directional
        else:
            # How far did it break?
            max_break_pct = max(
                (highest - upper_band) / entry * 100 if highest > upper_band else 0,
                (lower_band - lowest) / entry * 100 if lowest < lower_band else 0,
            )
            if max_break_pct > band_pct:
                outcome = "neutral_wrong"
                score = 0.0  # should have been directional
            else:
                outcome = "neutral_marginal"
                score = 0.4  # marginal — barely broke band

        actual_move_pct = (close_48h - entry) / entry * 100

        return {
            "price_at_48h": close_48h,
            "highest_price_48h": highest,
            "lowest_price_48h": lowest,
            "outcome": outcome,
            "score": score,
            "actual_move_pct": actual_move_pct,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------ #
    #  CWA computation
    # ------------------------------------------------------------------ #

    def compute_cwa(self, days: int = 30, asset: str = None) -> dict:
        """Compute Coverage-Weighted Accuracy.

        CWA = raw_accuracy * coverage_factor
        Where:
            raw_accuracy = correct_signals / total_signals
            coverage = directional_signals / total_signals
            coverage_factor = min(coverage / target_coverage, 1.0)
            target_coverage = 0.30  (at least 30% should be directional)
        """
        evals = self.store.get_accuracy_stats(asset=asset, days=days)
        if not evals:
            return {
                "cwa": 0.0,
                "raw_accuracy": 0.0,
                "coverage": 0.0,
                "total_signals": 0,
                "directional_signals": 0,
                "correct_signals": 0,
            }

        total = len(evals)
        directional = sum(1 for e in evals if e.get("direction") in ("buy", "sell"))
        correct = sum(1 for e in evals if (e.get("score") or 0) >= 0.7)

        raw_accuracy = correct / total if total > 0 else 0.0
        coverage = directional / total if total > 0 else 0.0
        target_coverage = 0.30
        coverage_factor = min(coverage / target_coverage, 1.0)

        cwa = raw_accuracy * coverage_factor

        # Per-asset breakdown
        per_asset: Dict[str, dict] = {}
        asset_groups: Dict[str, List] = {}
        for e in evals:
            a = e.get("asset", "?")
            asset_groups.setdefault(a, []).append(e)

        for a, group in asset_groups.items():
            a_total = len(group)
            a_dir = sum(1 for e in group if e.get("direction") in ("buy", "sell"))
            a_correct = sum(1 for e in group if (e.get("score") or 0) >= 0.7)
            a_accuracy = a_correct / a_total if a_total > 0 else 0.0
            a_cov = a_dir / a_total if a_total > 0 else 0.0
            per_asset[a] = {
                "accuracy": round(a_accuracy, 3),
                "cwa": round(a_accuracy * min(a_cov / target_coverage, 1.0), 3),
                "signals": a_total,
                "directional": a_dir,
                "correct": a_correct,
            }

        return {
            "cwa": round(cwa, 3),
            "raw_accuracy": round(raw_accuracy, 3),
            "coverage": round(coverage, 3),
            "coverage_factor": round(coverage_factor, 3),
            "total_signals": total,
            "directional_signals": directional,
            "correct_signals": correct,
            "per_asset": per_asset,
        }

    # ------------------------------------------------------------------ #
    #  Binance price fetching
    # ------------------------------------------------------------------ #

    def _fetch_48h_prices(self, asset: str, signal_timestamp: str) -> Optional[dict]:
        """Fetch 48h of hourly price data from Binance starting at signal time."""
        symbol = BINANCE_SYMBOLS.get(asset)
        if not symbol:
            return None

        try:
            # Parse signal timestamp
            if signal_timestamp.endswith("Z"):
                signal_timestamp = signal_timestamp[:-1] + "+00:00"
            dt = datetime.fromisoformat(signal_timestamp)
            start_ms = int(dt.timestamp() * 1000)

            url = (
                f"https://api.binance.com/api/v3/klines"
                f"?symbol={symbol}&interval=1h&startTime={start_ms}&limit=48"
            )

            req = urllib.request.Request(url, headers={"User-Agent": "x402-evaluator/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                candles = json.loads(resp.read())

            if not candles:
                return None

            prices: List[Tuple[str, float, float, float]] = []
            all_highs: List[float] = []
            all_lows: List[float] = []

            for c in candles:
                ts = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).isoformat()
                high = float(c[2])
                low = float(c[3])
                close = float(c[4])
                prices.append((ts, high, low, close))
                all_highs.append(high)
                all_lows.append(low)

            return {
                "prices": prices,
                "highest": max(all_highs) if all_highs else 0.0,
                "lowest": min(all_lows) if all_lows else 0.0,
                "close_48h": prices[-1][3] if prices else 0.0,
            }

        except Exception as exc:
            print(f"[evaluator] price fetch error for {asset}: {exc}")
            return None
