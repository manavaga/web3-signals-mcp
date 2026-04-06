# scoring/config.py
"""Pydantic validation for config.yaml and assets.yaml.

Crashes at startup if config is invalid — no silent failures.
"""
from __future__ import annotations
import yaml
from pathlib import Path
from pydantic import BaseModel, model_validator
from typing import Optional


# --- Scoring ---

class AbstainConfig(BaseModel):
    bearish_min_distance: float
    bullish_min_distance: float

class LabelEntry(BaseModel):
    name: str
    min_score: float

class TierWeightOverride(BaseModel):
    assets: list[str]
    weights_bullish: dict[str, float]
    weights_bearish: dict[str, float]

class RelativeMomentumConfig(BaseModel):
    multiplier: float = 0.15
    max_adjustment: float = 5.0

class ScoringConfig(BaseModel):
    weights_default: dict[str, float]
    weights_bullish: dict[str, float]
    weights_bearish: dict[str, float]
    labels: list[LabelEntry]
    abstain: AbstainConfig
    tier_multipliers: dict[str, float]
    momentum_threshold: float = 5.0
    per_tier_weights: Optional[dict[str, TierWeightOverride]] = None
    relative_momentum: RelativeMomentumConfig = RelativeMomentumConfig()

    @model_validator(mode="after")
    def check_weights_sum(self):
        for name in ("weights_default", "weights_bullish", "weights_bearish"):
            w = getattr(self, name)
            total = sum(w.values())
            if abs(total - 1.0) > 0.001:
                raise ValueError(f"{name} must sum to 1.0, got {total:.4f}")
        if self.per_tier_weights:
            for tier_name, tier in self.per_tier_weights.items():
                for wname in ("weights_bullish", "weights_bearish"):
                    w = getattr(tier, wname)
                    total = sum(w.values())
                    if abs(total - 1.0) > 0.001:
                        raise ValueError(f"per_tier_weights.{tier_name}.{wname} must sum to 1.0, got {total:.4f}")
        return self


# --- Regime ---

class FGThresholds(BaseModel):
    extreme_fear: int
    fear: int
    neutral: int
    greed: int

class RegimeConfig(BaseModel):
    btc_ma_period: int
    trending_threshold: float
    ranging_threshold: float
    adx_trending_threshold: float = 25.0
    adx_ranging_threshold: float = 20.0
    weight_shifts: dict[str, dict[str, float]]
    abstain_multiplier: dict[str, float]
    fg_thresholds: FGThresholds


# --- Targets ---

class TargetsConfig(BaseModel):
    timeframe_hours: int
    min_rr_ratio: float
    move_distance_divisor: float
    move_atr_multiplier: float
    move_max_atr_factor: float
    move_min_floor_atr_factor: float
    ml_blend_weight: float
    ml_max_move_pct: float
    model_max_age_hours: int
    min_training_samples: int


# --- Agents ---

class TechnicalAgentConfig(BaseModel):
    cadence_minutes: int
    binance_kline_limit: int
    intervals: list[str]
    rsi_period: int
    rsi_oversold: int
    rsi_overbought: int
    macd_fast: int
    macd_slow: int
    macd_signal: int
    bb_period: int
    bb_std_dev: int
    bb_squeeze_threshold: float
    volume_ma_period: int
    volume_spike_threshold: float
    volume_elevated_threshold: float
    volume_low_threshold: float
    atr_period: int
    scoring_weights: dict[str, float]
    volume_spike_bonus: float

class DerivativesAgentConfig(BaseModel):
    cadence_minutes: int
    scoring_weights: dict[str, float]
    ls_overcrowded: float
    ls_shorts_dominating: float
    ls_contrarian: float
    funding_extreme: float
    funding_extreme_negative: float
    oi_change_threshold_pct: float
    liq_imbalance_threshold: float

class MarketAgentConfig(BaseModel):
    cadence_minutes: int
    scoring_weights: dict[str, float]
    volume_spike_threshold: float
    macro_vix_risk_off: float
    macro_vix_risk_on: float
    macro_sp_risk_off_pct: float
    macro_sp_risk_on_pct: float
    macro_dxy_risk_off_pct: float
    macro_dxy_risk_on_pct: float
    stablecoin_inflow_threshold: float

class AgentsConfig(BaseModel):
    technical: TechnicalAgentConfig
    derivatives: DerivativesAgentConfig
    market: MarketAgentConfig


# --- Evaluation ---

class GradientThresholds(BaseModel):
    strong_correct: float
    correct: float
    weak_correct: float
    weak_wrong: float
    wrong: float

class EvaluationConfig(BaseModel):
    gradient_thresholds: GradientThresholds
    windows_hours: list[int]
    cwa_target_coverage: float
    min_evaluation_age_hours: int


# --- Learning ---

class DriftDetectionConfig(BaseModel):
    cwa_floor: float
    cwa_critical: float
    lookback_windows: int

class LearningConfig(BaseModel):
    shadow_mode: bool
    shadow_min_days: int
    dirichlet_concentration: float
    weight_step_size: float
    min_signals_per_asset: int
    ic_min_observations: int
    drift_detection: DriftDetectionConfig


# --- Top-Level ---

class AppConfig(BaseModel):
    scoring: ScoringConfig
    regime: RegimeConfig
    targets: TargetsConfig
    agents: AgentsConfig
    evaluation: EvaluationConfig
    learning: LearningConfig


# --- Assets ---

class AssetEntry(BaseModel):
    enabled: bool
    tier: str
    binance_symbol: str
    coingecko_id: str
    sl_atr_multiplier: float
    noise_threshold_pct: float
    strong_threshold_pct: float
    blacklist_reason: Optional[str] = None

class AssetsConfig(BaseModel):
    assets: dict[str, AssetEntry]

    def enabled_assets(self) -> list[str]:
        return [name for name, a in self.assets.items() if a.enabled]

    def blacklisted_assets(self) -> list[str]:
        return [name for name, a in self.assets.items() if not a.enabled]

    def get(self, asset: str) -> AssetEntry:
        return self.assets[asset]

    def tier_assets(self, tier: str) -> list[str]:
        return [name for name, a in self.assets.items() if a.tier == tier and a.enabled]


# --- Loaders ---

def load_config(path: str = "config.yaml") -> AppConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return AppConfig(**raw)

def load_assets(path: str = "assets.yaml") -> AssetsConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return AssetsConfig(**raw)
