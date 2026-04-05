# tests/test_config.py
import pytest
from scoring.config import load_config, load_assets, AppConfig, AssetsConfig


def test_load_config_from_yaml(tmp_path):
    yaml_content = """
scoring:
  weights_default:
    technical: 0.50
    derivatives: 0.10
    market: 0.40
  weights_bullish:
    technical: 0.50
    derivatives: 0.10
    market: 0.40
  weights_bearish:
    technical: 0.50
    derivatives: 0.10
    market: 0.40
  labels:
    - { name: "STRONG BUY", min_score: 70 }
    - { name: "MODERATE BUY", min_score: 60 }
    - { name: "NEUTRAL", min_score: 42 }
    - { name: "MODERATE SELL", min_score: 30 }
    - { name: "STRONG SELL", min_score: 0 }
  abstain:
    bearish_min_distance: 8
    bullish_min_distance: 10
  tier_multipliers:
    full: 1.0
    partial: 0.5
    none: 0.0
  momentum_threshold: 5
regime:
  btc_ma_period: 30
  trending_threshold: 0.08
  ranging_threshold: 0.03
  weight_shifts:
    trending: { technical: 1.2, derivatives: 0.9, market: 1.1 }
    ranging: { technical: 1.1, derivatives: 0.8, market: 1.2 }
  abstain_multiplier:
    trending: 0.7
    ranging: 3.0
    unknown: 1.0
  fg_thresholds: { extreme_fear: 20, fear: 40, neutral: 60, greed: 80 }
targets:
  timeframe_hours: 48
  min_rr_ratio: 0.5
  move_distance_divisor: 10.0
  move_atr_multiplier: 1.5
  move_max_atr_factor: 2.0
  move_min_floor_atr_factor: 0.3
  ml_blend_weight: 0.6
  ml_max_move_pct: 30.0
  model_max_age_hours: 24
  min_training_samples: 30
agents:
  technical: { cadence_minutes: 60, binance_kline_limit: 50, intervals: ["1d"], rsi_period: 14, rsi_oversold: 30, rsi_overbought: 70, macd_fast: 12, macd_slow: 26, macd_signal: 9, bb_period: 20, bb_std_dev: 2, bb_squeeze_threshold: 0.04, volume_ma_period: 20, volume_spike_threshold: 2.0, volume_elevated_threshold: 1.5, volume_low_threshold: 0.5, atr_period: 14, scoring_weights: { rsi: 0.25, macd: 0.25, bollinger: 0.20, trend: 0.30 }, volume_spike_bonus: 10 }
  derivatives: { cadence_minutes: 60, scoring_weights: { long_short: 0.20, funding: 0.25, open_interest: 0.15, liquidations: 0.20, taker_ratio: 0.20 }, ls_overcrowded: 0.65, ls_shorts_dominating: 0.55, ls_contrarian: 0.45, funding_extreme: 0.001, funding_extreme_negative: 0.005, oi_change_threshold_pct: 5.0, liq_imbalance_threshold: 0.3 }
  market: { cadence_minutes: 120, scoring_weights: { fear_greed: 0.25, volume: 0.15, breadth: 0.15, macro: 0.20, order_book: 0.25 }, volume_spike_threshold: 2.0, macro_vix_risk_off: 25, macro_vix_risk_on: 18, macro_sp_risk_off_pct: -1.5, macro_sp_risk_on_pct: 0.5, macro_dxy_risk_off_pct: 0.5, macro_dxy_risk_on_pct: -0.3, stablecoin_inflow_threshold: 0.5 }
evaluation:
  gradient_thresholds: { strong_correct: 1.0, correct: 0.7, weak_correct: 0.4, weak_wrong: 0.2, wrong: 0.0 }
  windows_hours: [24, 48]
  cwa_target_coverage: 0.30
  min_evaluation_age_hours: 48
learning:
  shadow_mode: true
  shadow_min_days: 90
  dirichlet_concentration: 10.0
  weight_step_size: 0.02
  min_signals_per_asset: 20
  ic_min_observations: 8
  drift_detection: { cwa_floor: 0.40, cwa_critical: 0.30, lookback_windows: 3 }
"""
    p = tmp_path / "config.yaml"
    p.write_text(yaml_content)
    cfg = load_config(str(p))
    assert isinstance(cfg, AppConfig)
    assert cfg.scoring.weights_default["technical"] == 0.50
    assert cfg.regime.trending_threshold == 0.08
    assert cfg.targets.timeframe_hours == 48
    assert cfg.learning.shadow_mode is True


def test_weights_must_sum_to_one(tmp_path):
    yaml_content = """
scoring:
  weights_default:
    technical: 0.50
    derivatives: 0.50
    market: 0.50
  weights_bullish:
    technical: 0.50
    derivatives: 0.10
    market: 0.40
  weights_bearish:
    technical: 0.50
    derivatives: 0.10
    market: 0.40
  labels:
    - { name: "STRONG BUY", min_score: 70 }
    - { name: "NEUTRAL", min_score: 42 }
    - { name: "STRONG SELL", min_score: 0 }
  abstain: { bearish_min_distance: 8, bullish_min_distance: 10 }
  tier_multipliers: { full: 1.0, partial: 0.5, none: 0.0 }
  momentum_threshold: 5
regime:
  btc_ma_period: 30
  trending_threshold: 0.08
  ranging_threshold: 0.03
  weight_shifts:
    trending: { technical: 1.0, derivatives: 1.0, market: 1.0 }
    ranging: { technical: 1.0, derivatives: 1.0, market: 1.0 }
  abstain_multiplier: { trending: 0.7, ranging: 3.0, unknown: 1.0 }
  fg_thresholds: { extreme_fear: 20, fear: 40, neutral: 60, greed: 80 }
targets:
  timeframe_hours: 48
  min_rr_ratio: 0.5
  move_distance_divisor: 10.0
  move_atr_multiplier: 1.5
  move_max_atr_factor: 2.0
  move_min_floor_atr_factor: 0.3
  ml_blend_weight: 0.6
  ml_max_move_pct: 30.0
  model_max_age_hours: 24
  min_training_samples: 30
agents:
  technical: { cadence_minutes: 60, binance_kline_limit: 50, intervals: ["1d"], rsi_period: 14, rsi_oversold: 30, rsi_overbought: 70, macd_fast: 12, macd_slow: 26, macd_signal: 9, bb_period: 20, bb_std_dev: 2, bb_squeeze_threshold: 0.04, volume_ma_period: 20, volume_spike_threshold: 2.0, volume_elevated_threshold: 1.5, volume_low_threshold: 0.5, atr_period: 14, scoring_weights: { rsi: 0.25, macd: 0.25, bollinger: 0.20, trend: 0.30 }, volume_spike_bonus: 10 }
  derivatives: { cadence_minutes: 60, scoring_weights: { long_short: 0.20, funding: 0.25, open_interest: 0.15, liquidations: 0.20, taker_ratio: 0.20 }, ls_overcrowded: 0.65, ls_shorts_dominating: 0.55, ls_contrarian: 0.45, funding_extreme: 0.001, funding_extreme_negative: 0.005, oi_change_threshold_pct: 5.0, liq_imbalance_threshold: 0.3 }
  market: { cadence_minutes: 120, scoring_weights: { fear_greed: 0.25, volume: 0.15, breadth: 0.15, macro: 0.20, order_book: 0.25 }, volume_spike_threshold: 2.0, macro_vix_risk_off: 25, macro_vix_risk_on: 18, macro_sp_risk_off_pct: -1.5, macro_sp_risk_on_pct: 0.5, macro_dxy_risk_off_pct: 0.5, macro_dxy_risk_on_pct: -0.3, stablecoin_inflow_threshold: 0.5 }
evaluation:
  gradient_thresholds: { strong_correct: 1.0, correct: 0.7, weak_correct: 0.4, weak_wrong: 0.2, wrong: 0.0 }
  windows_hours: [24, 48]
  cwa_target_coverage: 0.30
  min_evaluation_age_hours: 48
learning:
  shadow_mode: true
  shadow_min_days: 90
  dirichlet_concentration: 10.0
  weight_step_size: 0.02
  min_signals_per_asset: 20
  ic_min_observations: 8
  drift_detection: { cwa_floor: 0.40, cwa_critical: 0.30, lookback_windows: 3 }
"""
    p = tmp_path / "config.yaml"
    p.write_text(yaml_content)
    with pytest.raises(ValueError, match="sum to 1.0"):
        load_config(str(p))


def test_load_assets(tmp_path):
    yaml_content = """
assets:
  BTC:
    enabled: true
    tier: large_cap
    binance_symbol: BTCUSDT
    coingecko_id: bitcoin
    sl_atr_multiplier: 2.0
    noise_threshold_pct: 1.0
    strong_threshold_pct: 3.0
  INJ:
    enabled: false
    blacklist_reason: "Anti-predictive"
    tier: small_cap
    binance_symbol: INJUSDT
    coingecko_id: injective-protocol
    sl_atr_multiplier: 2.5
    noise_threshold_pct: 2.5
    strong_threshold_pct: 6.0
"""
    p = tmp_path / "assets.yaml"
    p.write_text(yaml_content)
    assets = load_assets(str(p))
    assert isinstance(assets, AssetsConfig)
    assert assets.enabled_assets() == ["BTC"]
    assert "INJ" in assets.blacklisted_assets()
    assert assets.get("BTC").binance_symbol == "BTCUSDT"


def test_load_real_config():
    """Test loading the actual config.yaml and assets.yaml from repo root."""
    import os
    root = os.path.join(os.path.dirname(__file__), "..")
    cfg = load_config(os.path.join(root, "config.yaml"))
    assert len(cfg.scoring.labels) == 5
    assert cfg.agents.technical.rsi_period == 14

    assets = load_assets(os.path.join(root, "assets.yaml"))
    enabled = assets.enabled_assets()
    assert "BTC" in enabled
    assert "INJ" not in enabled
    assert len(assets.assets) == 20
