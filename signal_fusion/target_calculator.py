"""Target price and stop-loss calculator for directional signals.

Components:
1. ATR-based stop-loss (always available)
2. Score-based target prediction (heuristic, always available)
3. ML price prediction (LightGBM, when enough training data exists)
4. Historical similarity matching (nearest-neighbor confidence check)
"""

from __future__ import annotations

import math
import os
import pickle
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ML imports — optional
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    HAS_LGB = False


# Per-asset ATR multipliers for stop-loss
ATR_SL_MULTIPLIERS: Dict[str, float] = {
    "BTC": 1.5, "ETH": 1.5,
    "SOL": 2.0, "BNB": 2.0, "XRP": 2.0, "ADA": 2.0, "LINK": 2.0,
    "LTC": 2.0, "DOT": 2.0, "AVAX": 2.0,
    "MATIC": 2.5, "UNI": 2.5, "FIL": 2.5, "NEAR": 2.5, "APT": 2.5,
    "ARB": 2.5, "SUI": 2.5,
    "ATOM": 2.0, "OP": 2.5, "INJ": 2.5,
}

# Feature names for ML model (order matters for training consistency)
FEATURE_NAMES = [
    "score_exchange_flow", "score_technical", "score_derivatives",
    "score_narrative", "score_market",
    "rsi_14", "macd_histogram", "bb_position", "volume_ma_ratio", "atr_pct",
    "funding_rate", "long_short_ratio", "taker_buy_sell_ratio",
    "change_24h_pct", "fear_greed",
    "bid_ask_ratio", "volume_change_pct",
]

MODEL_CACHE_DIR = Path(os.getenv("MODEL_CACHE_DIR", "/tmp/x402_models"))
MODEL_MAX_AGE_HOURS = 24
MIN_TRAINING_SAMPLES = 30


class TargetCalculator:
    """Calculate target price, stop loss, and confidence for directional signals."""

    def __init__(self, store: Any, profile: dict) -> None:
        self.store = store
        self.profile = profile
        self._model: Any = None
        self._model_loaded_at: float = 0
        self._feature_stats: Optional[Dict[str, Tuple[float, float]]] = None  # (mean, std) per feature

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def calculate(
        self,
        asset: str,
        direction: str,
        composite_score: float,
        dimension_scores: dict,
        agent_data: dict,
    ) -> dict:
        """Calculate target price and stop loss for a signal.

        Returns dict with entry_price, target_price, stop_loss, etc.
        """
        # 1. Get current price (entry price)
        entry_price = self._get_entry_price(asset, agent_data)
        if entry_price is None or entry_price <= 0:
            return {}

        # 2. Get ATR for stop-loss
        atr_14 = self._get_atr(asset, agent_data)
        if atr_14 is None or atr_14 <= 0:
            # Fallback: estimate ATR as 3% of price
            atr_14 = entry_price * 0.03

        # 3. Calculate stop loss (always ATR-based)
        stop_loss = self._calculate_stop_loss(asset, entry_price, direction, atr_14)

        # 4. Predict price move
        features = self._extract_features(asset, dimension_scores, agent_data)
        predicted_move_pct = self._predict_move(asset, features, composite_score, direction)
        method = "score_heuristic"

        # Try ML prediction if available
        ml_pred = self._ml_predict(features)
        if ml_pred is not None:
            # Blend ML with heuristic (ML gets 60% weight when available)
            predicted_move_pct = ml_pred * 0.6 + predicted_move_pct * 0.4
            method = "ml_blend"

        # 5. Ensure direction alignment
        if direction == "buy" and predicted_move_pct < 0:
            predicted_move_pct = abs(predicted_move_pct) * 0.5  # flip but dampen
        elif direction == "sell" and predicted_move_pct > 0:
            predicted_move_pct = -abs(predicted_move_pct) * 0.5

        # 6. Calculate target price with minimum R:R ratio
        target_price = self._calculate_target(
            entry_price, direction, predicted_move_pct, stop_loss
        )

        # 7. Risk/reward ratio
        risk = abs(entry_price - stop_loss)
        reward = abs(target_price - entry_price)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0

        # 8. Confidence assessment
        similarity = self._find_similar_setups(features)
        confidence = self._assess_confidence(
            predicted_move_pct, similarity, composite_score
        )

        return {
            "entry_price": round(entry_price, self._price_decimals(entry_price)),
            "target_price": round(target_price, self._price_decimals(target_price)),
            "stop_loss": round(stop_loss, self._price_decimals(stop_loss)),
            "risk_reward_ratio": rr_ratio,
            "confidence": confidence,
            "timeframe_hours": 48,
            "method": method,
            "similar_setups_found": similarity.get("found", 0),
            "predicted_move_pct": round(predicted_move_pct, 2),
        }

    # ------------------------------------------------------------------ #
    #  Feature extraction
    # ------------------------------------------------------------------ #

    def _extract_features(
        self, asset: str, dimension_scores: dict, agent_data: dict
    ) -> Dict[str, float]:
        """Extract feature vector from current agent data."""
        features: Dict[str, float] = {}

        # Dimension scores (0-100)
        for dim in ["exchange_flow", "technical", "derivatives", "narrative", "market"]:
            features[f"score_{dim}"] = float(
                dimension_scores.get(dim, {}).get("score", 50.0)
            )

        # Technical indicators
        tech = (
            (agent_data.get("technical") or {})
            .get("data", {})
            .get("by_asset", {})
            .get(asset, {})
        )
        features["rsi_14"] = float(tech.get("rsi_14", 50.0))
        features["macd_histogram"] = float(tech.get("macd_histogram", 0.0))
        features["bb_position"] = float(tech.get("bb_position", 0.5))
        features["volume_ma_ratio"] = float(tech.get("volume_ma_ratio", 1.0))
        features["atr_pct"] = float(tech.get("atr_pct", 3.0))

        # Derivatives indicators
        deriv = (
            (agent_data.get("derivatives") or {})
            .get("data", {})
            .get("by_asset", {})
            .get(asset, {})
        )
        features["funding_rate"] = float(deriv.get("funding_rate", 0.0))
        features["long_short_ratio"] = float(deriv.get("long_short_ratio", 0.5))
        features["taker_buy_sell_ratio"] = float(
            deriv.get("taker_buy_sell_ratio", 1.0)
        )

        # Market indicators
        mkt = (agent_data.get("market") or {}).get("data", {})
        per_asset = mkt.get("per_asset", {}).get(asset, {})
        features["change_24h_pct"] = float(per_asset.get("change_24h_pct", 0.0))

        fg = mkt.get("sentiment", {}).get("value")
        features["fear_greed"] = float(fg) if fg is not None else 50.0

        # Exchange flow
        flow = (
            (agent_data.get("exchange_flow") or {})
            .get("data", {})
            .get("by_asset", {})
            .get(asset, {})
        )
        features["bid_ask_ratio"] = float(flow.get("bid_ask_ratio", 1.0))
        features["volume_change_pct"] = float(flow.get("volume_change_pct", 0.0))

        return features

    # ------------------------------------------------------------------ #
    #  Heuristic price prediction (always available)
    # ------------------------------------------------------------------ #

    def _predict_move(
        self,
        asset: str,
        features: Dict[str, float],
        composite_score: float,
        direction: str,
    ) -> float:
        """Predict 48h price move % using score-based heuristic.

        Maps composite score distance from 50 to expected move magnitude,
        scaled by asset volatility (ATR%).
        """
        distance = composite_score - 50.0  # positive = bullish, negative = bearish
        atr_pct = features.get("atr_pct", 3.0)

        # Base move: score distance maps to fraction of ATR
        # Score 70 (+20 from center) → ~0.5 * ATR expected move
        # Score 85 (+35 from center) → ~1.0 * ATR expected move
        move_fraction = distance / 35.0  # normalize to roughly ±1.0
        predicted_pct = move_fraction * atr_pct * 0.5  # half ATR per unit

        # Clamp to reasonable range
        max_move = atr_pct * 2.0
        predicted_pct = max(-max_move, min(max_move, predicted_pct))

        return predicted_pct

    # ------------------------------------------------------------------ #
    #  ML prediction (when trained model available)
    # ------------------------------------------------------------------ #

    def _ml_predict(self, features: Dict[str, float]) -> Optional[float]:
        """Predict 48h move using trained LightGBM model."""
        if not HAS_LGB or not HAS_NUMPY:
            return None

        model = self._load_model()
        if model is None:
            return None

        try:
            x = np.array([[features.get(f, 0.0) for f in FEATURE_NAMES]])
            pred = model.predict(x)[0]
            # Clamp to reasonable range
            return float(max(-30.0, min(30.0, pred)))
        except Exception:
            return None

    def _load_model(self) -> Any:
        """Load cached model if fresh enough, else return None."""
        if self._model is not None:
            age_hours = (time.time() - self._model_loaded_at) / 3600
            if age_hours < MODEL_MAX_AGE_HOURS:
                return self._model

        model_path = MODEL_CACHE_DIR / "lgb_target_model.pkl"
        if model_path.exists():
            try:
                with open(model_path, "rb") as f:
                    self._model = pickle.load(f)
                self._model_loaded_at = time.time()
                return self._model
            except Exception:
                return None
        return None

    def train_model(self) -> bool:
        """Train LightGBM model on historical data. Called externally (e.g., by evaluator)."""
        if not HAS_LGB or not HAS_NUMPY:
            return False

        try:
            X, y = self._build_training_data()
            if len(X) < MIN_TRAINING_SAMPLES:
                return False

            params = {
                "objective": "regression",
                "metric": "mae",
                "num_leaves": 31,
                "learning_rate": 0.05,
                "n_estimators": 200,
                "min_child_samples": 5,
                "subsample": 0.8,
                "colsample_bytree": 0.8,
                "verbose": -1,
            }

            model = lgb.LGBMRegressor(**params)
            model.fit(X, y)

            MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            model_path = MODEL_CACHE_DIR / "lgb_target_model.pkl"
            with open(model_path, "wb") as f:
                pickle.dump(model, f)

            self._model = model
            self._model_loaded_at = time.time()
            return True
        except Exception:
            return False

    def _build_training_data(self) -> Tuple[Any, Any]:
        """Build training data from historical signal evaluations."""
        if not HAS_NUMPY:
            return [], []

        rows = self.store.get_evaluated_signals(days=90)
        X_list: List[List[float]] = []
        y_list: List[float] = []

        for row in rows:
            features = row.get("features")
            actual_move = row.get("actual_move_pct")
            if features and actual_move is not None:
                x = [features.get(f, 0.0) for f in FEATURE_NAMES]
                X_list.append(x)
                y_list.append(float(actual_move))

        return np.array(X_list) if X_list else np.array([]), np.array(y_list)

    # ------------------------------------------------------------------ #
    #  Historical similarity matching
    # ------------------------------------------------------------------ #

    def _find_similar_setups(
        self, features: Dict[str, float], n_neighbors: int = 10
    ) -> dict:
        """Find similar historical setups and their outcomes."""
        result = {"found": 0, "avg_return_48h": 0.0, "agreement": True}

        if not HAS_NUMPY:
            return result

        try:
            rows = self.store.get_evaluated_signals(days=60)
            if len(rows) < n_neighbors:
                return result

            # Build feature matrix from history
            hist_features: List[List[float]] = []
            hist_returns: List[float] = []
            for row in rows:
                f = row.get("features")
                ret = row.get("actual_move_pct")
                if f and ret is not None:
                    hist_features.append([f.get(k, 0.0) for k in FEATURE_NAMES])
                    hist_returns.append(float(ret))

            if len(hist_features) < n_neighbors:
                return result

            hist_X = np.array(hist_features)
            hist_y = np.array(hist_returns)

            # Normalize
            means = hist_X.mean(axis=0)
            stds = hist_X.std(axis=0)
            stds[stds == 0] = 1.0
            hist_norm = (hist_X - means) / stds

            current = np.array([[features.get(k, 0.0) for k in FEATURE_NAMES]])
            current_norm = (current - means) / stds

            # Euclidean distances
            dists = np.sqrt(((hist_norm - current_norm) ** 2).sum(axis=1))
            nearest_idx = np.argsort(dists)[:n_neighbors]

            nearest_returns = hist_y[nearest_idx]
            result["found"] = len(nearest_idx)
            result["avg_return_48h"] = float(np.mean(nearest_returns))
            result["std_return_48h"] = float(np.std(nearest_returns))

        except Exception:
            pass

        return result

    # ------------------------------------------------------------------ #
    #  Stop loss & target calculation
    # ------------------------------------------------------------------ #

    def _calculate_stop_loss(
        self, asset: str, entry_price: float, direction: str, atr_14: float
    ) -> float:
        """ATR-based stop loss."""
        multiplier = ATR_SL_MULTIPLIERS.get(asset, 2.0)
        if direction == "buy":
            return entry_price - (atr_14 * multiplier)
        else:
            return entry_price + (atr_14 * multiplier)

    def _calculate_target(
        self,
        entry_price: float,
        direction: str,
        predicted_move_pct: float,
        stop_loss: float,
        min_rr_ratio: float = 1.5,
    ) -> float:
        """Calculate target ensuring minimum risk/reward ratio."""
        risk = abs(entry_price - stop_loss)
        min_reward = risk * min_rr_ratio

        # ML/heuristic predicted target
        ml_target = entry_price * (1 + predicted_move_pct / 100)

        # Ensure minimum R:R
        if direction == "buy":
            min_target = entry_price + min_reward
            return max(ml_target, min_target)
        else:
            min_target = entry_price - min_reward
            return min(ml_target, min_target)

    # ------------------------------------------------------------------ #
    #  Confidence assessment
    # ------------------------------------------------------------------ #

    def _assess_confidence(
        self,
        predicted_move_pct: float,
        similarity: dict,
        composite_score: float,
    ) -> str:
        """Determine confidence level."""
        score_distance = abs(composite_score - 50.0)
        similar_found = similarity.get("found", 0)
        sim_avg = similarity.get("avg_return_48h", 0.0)

        # Check agreement between prediction and similarity
        pred_sign = 1 if predicted_move_pct > 0 else -1
        sim_sign = 1 if sim_avg > 0 else -1
        agreement = pred_sign == sim_sign if similar_found >= 5 else True

        if score_distance > 20 and agreement and similar_found >= 5:
            return "high"
        elif score_distance > 12 and agreement:
            return "medium"
        else:
            return "low"

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def _get_entry_price(self, asset: str, agent_data: dict) -> Optional[float]:
        """Get current price from market or technical agent data."""
        # Try market agent first
        mkt = (agent_data.get("market") or {}).get("data", {})
        price = mkt.get("per_asset", {}).get(asset, {}).get("price")
        if price:
            return float(price)

        # Fallback to technical agent
        tech = (agent_data.get("technical") or {}).get("data", {})
        price = tech.get("by_asset", {}).get(asset, {}).get("price")
        if price:
            return float(price)

        return None

    def _get_atr(self, asset: str, agent_data: dict) -> Optional[float]:
        """Get ATR(14) from technical agent data."""
        tech = (agent_data.get("technical") or {}).get("data", {})
        atr = tech.get("by_asset", {}).get(asset, {}).get("atr_14")
        if atr is not None:
            return float(atr)

        # Try market agent ATR
        mkt = (agent_data.get("market") or {}).get("data", {})
        atr = mkt.get("per_asset", {}).get(asset, {}).get("atr_14")
        if atr is not None:
            return float(atr)

        return None

    @staticmethod
    def _price_decimals(price: float) -> int:
        """Determine appropriate decimal places for a price."""
        if price >= 1000:
            return 2
        elif price >= 1:
            return 4
        elif price >= 0.01:
            return 6
        else:
            return 8
