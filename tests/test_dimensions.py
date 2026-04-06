# tests/test_dimensions.py
from scoring.types import DimensionScore
from scoring.dimensions import (
    score_technical, score_derivatives, score_market,
    detect_data_tier
)


def test_score_technical_bullish():
    data = {
        "rsi_14": 35.0,
        "macd_histogram": 0.5,
        "bb_bandwidth": 0.03,  # tight = bullish
        "price": 84000.0,
        "ma7": 83000.0,
        "ma30": 82000.0,
        "volume_status": "normal",
        "macd_zscore": 0.5,
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.15, "macd": 0.10, "bb_bandwidth": 0.15, "trend": 0.15,
            "obv": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "macd_zscore": 0.10,
        },
        "volume_spike_bonus": 10,
        "rsi_oversold": 30, "rsi_overbought": 70,
    }
    ds = score_technical(data, cfg)
    assert isinstance(ds, DimensionScore)
    assert ds.name == "technical"
    assert ds.score > 55


def test_score_technical_no_data():
    ds = score_technical(None, {})
    assert ds.score == 50.0
    assert ds.tier == "none"


def test_score_derivatives_overcrowded_longs():
    data = {
        "long_short_ratio": 0.72,
        "funding_rate": 0.002,
        "oi_change_pct": 5.0,
        "liq_imbalance": 0.0,
        "taker_buy_sell_ratio": 0.95,
    }
    cfg = {
        "scoring_weights": {"long_short": 0.20, "funding": 0.25, "open_interest": 0.15,
                            "liquidations": 0.20, "taker_ratio": 0.20},
        "ls_overcrowded": 0.65, "ls_shorts_dominating": 0.55, "ls_contrarian": 0.45,
        "funding_extreme": 0.001, "funding_extreme_negative": 0.005,
        "oi_change_threshold_pct": 5.0, "liq_imbalance_threshold": 0.3,
    }
    ds = score_derivatives(data, cfg)
    assert ds.score < 40


def test_score_market_extreme_fear():
    data = {
        "fear_greed": 15,
        "volume_ratio": 1.2,
        "breadth_status": "neutral",
        "macro_status": "neutral",
        "order_book_imbalance": 1.0,
    }
    cfg = {
        "scoring_weights": {"fear_greed": 0.25, "volume": 0.15, "breadth": 0.15,
                            "macro": 0.20, "order_book": 0.25},
    }
    ds = score_market(data, cfg)
    assert ds.score > 55


def test_score_technical_with_new_indicators():
    """New indicators (OBV, ROC, squeeze, macd_zscore) should influence the score."""
    data = {
        "rsi_14": 35.0,
        "macd_histogram": 0.5,
        "bb_bandwidth": 0.03,     # tight = bullish
        "price": 84000.0,
        "ma7": 83000.0,
        "ma30": 82000.0,
        "volume_status": "normal",
        # OBV inverted: negative slope = bullish (high OBV → price DOWN)
        "obv_slope": -0.05,
        "roc_7d": 3.0,
        "squeeze_on": False,
        "squeeze_momentum": 2.0,
        "macd_zscore": 1.0,       # bullish momentum
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.15, "macd": 0.10, "bb_bandwidth": 0.15, "trend": 0.15,
            "obv": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "macd_zscore": 0.10,
        },
        "volume_spike_bonus": 10,
        "rsi_oversold": 30, "rsi_overbought": 70,
    }
    ds = score_technical(data, cfg)
    assert ds.score > 60, f"Bullish setup should score > 60, got {ds.score}"


def test_score_technical_bearish_new_indicators():
    """Bearish new indicator values should pull score below 40."""
    data = {
        "rsi_14": 75.0,
        "macd_histogram": -0.5,
        "bb_bandwidth": 0.12,     # wide = bearish
        "price": 84000.0,
        "ma7": 85000.0,
        "ma30": 86000.0,
        "volume_status": "normal",
        # OBV inverted: positive slope = bearish
        "obv_slope": 0.08,
        "roc_7d": -5.0,
        "squeeze_on": False,
        "squeeze_momentum": -3.0,
        "macd_zscore": -1.5,      # bearish momentum
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.15, "macd": 0.10, "bb_bandwidth": 0.15, "trend": 0.15,
            "obv": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "macd_zscore": 0.10,
        },
        "volume_spike_bonus": 10,
        "rsi_oversold": 30, "rsi_overbought": 70,
    }
    ds = score_technical(data, cfg)
    assert ds.score < 40, f"Bearish setup should score < 40, got {ds.score}"


def test_score_technical_squeeze_on_neutral():
    """When squeeze is on, squeeze score should be neutral (50)."""
    data = {
        "rsi_14": 50.0,
        "macd_histogram": 0.0,
        "bb_bandwidth": 0.06,    # mid-range = neutral
        "price": 84000.0,
        "ma7": 84000.0,
        "ma30": 84000.0,
        "volume_status": "normal",
        "obv_slope": 0.0,
        "roc_7d": 0.0,
        "squeeze_on": True,
        "squeeze_momentum": 5.0,  # momentum ignored when squeeze is on
        "macd_zscore": 0.0,
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.15, "macd": 0.10, "bb_bandwidth": 0.15, "trend": 0.15,
            "obv": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "macd_zscore": 0.10,
        },
        "volume_spike_bonus": 10,
        "rsi_oversold": 30, "rsi_overbought": 70,
    }
    ds = score_technical(data, cfg)
    # With all neutral values and squeeze on, score should be near 50
    assert 40 < ds.score < 60, f"Neutral setup should score near 50, got {ds.score}"


def test_score_technical_defaults_when_missing_new_indicators():
    """When new indicator fields are missing, scoring should still work (backwards compatible)."""
    data = {
        "rsi_14": 50.0,
        "macd_histogram": 0.0,
        "price": 84000.0,
        "ma7": 84000.0,
        "ma30": 84000.0,
        "volume_status": "normal",
        # No bb_bandwidth, obv_slope, roc_7d, squeeze, macd_zscore fields
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.15, "macd": 0.10, "bb_bandwidth": 0.15, "trend": 0.15,
            "obv": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "macd_zscore": 0.10,
        },
        "volume_spike_bonus": 10,
        "rsi_oversold": 30, "rsi_overbought": 70,
    }
    ds = score_technical(data, cfg)
    assert isinstance(ds, DimensionScore)
    assert 0 <= ds.score <= 100


def test_detect_data_tier():
    assert detect_data_tier(65.0, "RSI=42, MACD bullish, BB=0.3") == "full"
    assert detect_data_tier(50.0, "no data available") == "none"
    assert detect_data_tier(50.0, "error: API timeout") == "none"
    assert detect_data_tier(50.0, "partial data") == "partial"


# --- New market scoring tests (Task 6) ---

NEW_MARKET_WEIGHTS = {
    "fear_greed": 0.15, "volume": 0.10, "macro": 0.15, "order_book": 0.15,
    "stablecoin": 0.15, "dxy": 0.10, "nasdaq": 0.10, "vix_roc": 0.10,
}

def _base_market_data(**overrides):
    """Neutral baseline market data."""
    d = {
        "fear_greed": 50, "volume_ratio": 1.0,
        "breadth_status": "neutral", "macro_status": "neutral",
        "order_book_imbalance": 1.0,
        "stablecoin_supply_change_7d": 0.0,
        "dxy_change": 0.0, "nasdaq_change": 0.0, "vix_roc": 0.0,
    }
    d.update(overrides)
    return d


def test_market_score_stablecoin_growth_bullish():
    """Positive stablecoin supply growth should push score above neutral."""
    data = _base_market_data(stablecoin_supply_change_7d=3.0)
    cfg = {"scoring_weights": NEW_MARKET_WEIGHTS}
    ds = score_market(data, cfg)
    neutral = score_market(_base_market_data(), cfg)
    assert ds.score > neutral.score, f"Stablecoin growth should be bullish: {ds.score} vs {neutral.score}"


def test_market_score_dxy_up_bearish():
    """DXY up should push score below neutral (inverse correlation)."""
    data = _base_market_data(dxy_change=1.5)
    cfg = {"scoring_weights": NEW_MARKET_WEIGHTS}
    ds = score_market(data, cfg)
    neutral = score_market(_base_market_data(), cfg)
    assert ds.score < neutral.score, f"DXY up should be bearish: {ds.score} vs {neutral.score}"


def test_market_score_nasdaq_up_bullish():
    """NASDAQ up should push score above neutral."""
    data = _base_market_data(nasdaq_change=2.0)
    cfg = {"scoring_weights": NEW_MARKET_WEIGHTS}
    ds = score_market(data, cfg)
    neutral = score_market(_base_market_data(), cfg)
    assert ds.score > neutral.score, f"NASDAQ up should be bullish: {ds.score} vs {neutral.score}"


def test_market_score_vix_falling_bullish():
    """Falling VIX (negative ROC) should push score above neutral."""
    data = _base_market_data(vix_roc=-5.0)
    cfg = {"scoring_weights": NEW_MARKET_WEIGHTS}
    ds = score_market(data, cfg)
    neutral = score_market(_base_market_data(), cfg)
    assert ds.score > neutral.score, f"Falling VIX should be bullish: {ds.score} vs {neutral.score}"


def test_market_score_full_bullish_data():
    """All bullish inputs should produce score > 65."""
    data = _base_market_data(
        fear_greed=10,               # extreme fear = contrarian bullish
        volume_ratio=2.5,            # high volume
        macro_status="strong_risk_on",
        order_book_imbalance=2.0,    # bid heavy
        stablecoin_supply_change_7d=5.0,  # growing supply
        dxy_change=-2.0,             # DXY falling = bullish
        nasdaq_change=3.0,           # NASDAQ up
        vix_roc=-10.0,              # VIX falling fast
    )
    cfg = {"scoring_weights": NEW_MARKET_WEIGHTS}
    ds = score_market(data, cfg)
    assert ds.score > 65, f"Full bullish should score > 65, got {ds.score}"


def test_market_score_full_bearish_data():
    """All bearish inputs should produce score < 35."""
    data = _base_market_data(
        fear_greed=90,               # extreme greed = contrarian bearish
        volume_ratio=0.3,            # low volume
        macro_status="strong_risk_off",
        order_book_imbalance=0.3,    # ask heavy
        stablecoin_supply_change_7d=-5.0,  # shrinking supply
        dxy_change=2.0,              # DXY rising = bearish
        nasdaq_change=-3.0,          # NASDAQ down
        vix_roc=10.0,               # VIX rising fast
    )
    cfg = {"scoring_weights": NEW_MARKET_WEIGHTS}
    ds = score_market(data, cfg)
    assert ds.score < 35, f"Full bearish should score < 35, got {ds.score}"


def test_market_score_backward_compatible():
    """When new fields are missing, scoring should still work with defaults."""
    data = {
        "fear_greed": 50, "volume_ratio": 1.0,
        "breadth_status": "neutral", "macro_status": "neutral",
        "order_book_imbalance": 1.0,
        # No stablecoin, dxy, nasdaq, vix_roc fields
    }
    cfg = {"scoring_weights": NEW_MARKET_WEIGHTS}
    ds = score_market(data, cfg)
    assert isinstance(ds, DimensionScore)
    assert 40 < ds.score < 60, f"Missing new fields should default to neutral, got {ds.score}"


# --- Regime-aware scoring tests ---

TECH_CFG = {
    "scoring_weights": {
        "rsi": 0.15, "macd": 0.10, "bb_bandwidth": 0.15, "trend": 0.15,
        "obv": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "macd_zscore": 0.10,
    },
    "volume_spike_bonus": 10,
    "rsi_oversold": 30, "rsi_overbought": 70,
}


def test_regime_trending_down_suppresses_rsi_buy():
    """In trending_down, RSI oversold should NOT generate buy signal (clamped to ≤50)."""
    data = {
        "rsi_14": 20.0,  # very oversold — would normally score ~90
        "macd_histogram": -0.5,
        "bb_bandwidth": 0.02,  # very tight — would normally score bullish
        "price": 75000.0,
        "ma7": 78000.0,
        "ma30": 82000.0,
        "volume_status": "normal",
        "obv_slope": 0.05,     # positive slope = bearish (inverted)
        "roc_7d": -8.0,
        "squeeze_on": False,
        "squeeze_momentum": -2.0,
        "macd_zscore": -1.0,
    }
    # Without regime — RSI and BB would push bullish
    ds_no_regime = score_technical(data, TECH_CFG)
    # With trending_down — RSI and BB clamped to ≤50
    ds_trending = score_technical(data, TECH_CFG, regime="trending_down")
    assert ds_trending.score < ds_no_regime.score, (
        f"trending_down should suppress mean-reversion: {ds_trending.score} vs {ds_no_regime.score}"
    )
    assert ds_trending.score <= 50.0, f"Score should be ≤50 in downtrend, got {ds_trending.score}"


def test_regime_trending_up_suppresses_rsi_sell():
    """In trending_up, RSI overbought should NOT generate sell signal (clamped to ≥50)."""
    data = {
        "rsi_14": 80.0,  # overbought — would normally score ~15
        "macd_histogram": 0.5,
        "bb_bandwidth": 0.13,  # wide — would normally score bearish
        "price": 90000.0,
        "ma7": 88000.0,
        "ma30": 85000.0,
        "volume_status": "normal",
        "obv_slope": -0.05,    # negative slope = bullish (inverted)
        "roc_7d": 6.0,
        "squeeze_on": False,
        "squeeze_momentum": 2.0,
        "macd_zscore": 1.5,
    }
    ds_no_regime = score_technical(data, TECH_CFG)
    ds_trending = score_technical(data, TECH_CFG, regime="trending_up")
    assert ds_trending.score > ds_no_regime.score, (
        f"trending_up should suppress mean-reversion sells: {ds_trending.score} vs {ds_no_regime.score}"
    )


def test_score_technical_with_fitted_params():
    """When fitted_params provided, score_technical uses IC-based scoring."""
    data = {"rsi_14": 25.0, "macd_histogram": 0.005, "price": 100.0,
            "bb_bandwidth": 0.04, "ma7": 101.0, "ma30": 102.0,
            "obv_slope": 0.01, "roc_7d": -2.0, "squeeze_on": False,
            "squeeze_momentum": 0.5, "macd_zscore": 1.0}
    cfg = {}

    # Without fitted params
    result_legacy = score_technical(data, cfg, regime="")

    # With fitted params
    fitted = {
        "rsi_14": {"mean": 50.0, "std": 15.0, "ic": -0.15},
        "macd_histogram": {"mean": 0.0, "std": 0.01, "ic": 0.12},
        "bb_bandwidth": {"mean": 0.06, "std": 0.03, "ic": -0.22},
    }
    result_fitted = score_technical(data, cfg, regime="", fitted_params=fitted)

    assert 0 <= result_legacy.score <= 100
    assert 0 <= result_fitted.score <= 100
    assert "[IC-fitted]" in result_fitted.detail


def test_score_technical_fallback_without_fitted():
    """Without fitted params, score_technical uses legacy hardcoded scoring."""
    data = {"rsi_14": 25.0, "macd_histogram": 0.005, "price": 100.0,
            "bb_bandwidth": 0.04, "ma7": 101.0, "ma30": 102.0}
    cfg = {}

    result = score_technical(data, cfg, regime="")
    assert "[IC-fitted]" not in result.detail


def test_score_derivatives_with_fitted_params():
    """When fitted_params provided, score_derivatives uses IC-based scoring."""
    data = {"long_short_ratio": 0.6, "funding_rate": 0.0005,
            "oi_change_pct": 3.0, "taker_buy_sell_ratio": 1.1,
            "liq_imbalance": 0.0}
    cfg = {"scoring_weights": {"long_short": 0.20, "funding": 0.25,
                                "open_interest": 0.15, "liquidations": 0.20,
                                "taker_ratio": 0.20},
           "ls_overcrowded": 0.65, "ls_shorts_dominating": 0.55,
           "funding_extreme": 0.001, "liq_imbalance_threshold": 0.3}

    result_legacy = score_derivatives(data, cfg)

    fitted = {
        "funding_rate": {"mean": 0.0003, "std": 0.0005, "ic": -0.18},
        "long_short_ratio": {"mean": 0.55, "std": 0.08, "ic": -0.10},
    }
    result_fitted = score_derivatives(data, cfg, fitted_params=fitted)

    assert 0 <= result_legacy.score <= 100
    assert 0 <= result_fitted.score <= 100
    assert "[IC-fitted]" in result_fitted.detail


def test_score_market_with_fitted_params():
    """When fitted_params provided, score_market uses IC-based scoring."""
    data = _base_market_data(fear_greed=20, dxy_change=-1.0)
    cfg = {"scoring_weights": NEW_MARKET_WEIGHTS}

    result_legacy = score_market(data, cfg)

    fitted = {
        "fear_greed": {"mean": 50.0, "std": 20.0, "ic": -0.12},
        "dxy_change": {"mean": 0.0, "std": 1.0, "ic": -0.08},
    }
    result_fitted = score_market(data, cfg, fitted_params=fitted)

    assert 0 <= result_legacy.score <= 100
    assert 0 <= result_fitted.score <= 100
    assert "[IC-fitted]" in result_fitted.detail


def test_regime_ranging_passes_through():
    """In ranging regime, all signals pass through normally (no clamping)."""
    data = {
        "rsi_14": 25.0,
        "macd_histogram": 0.1,
        "bb_bandwidth": 0.04,
        "price": 82000.0,
        "ma7": 82000.0,
        "ma30": 82000.0,
        "volume_status": "normal",
        "macd_zscore": 0.0,
    }
    # With no regime_scoring_weights in cfg, both should use default weights
    # and ranging should not clamp RSI/bb_bandwidth
    ds_ranging = score_technical(data, TECH_CFG, regime="ranging")
    ds_none = score_technical(data, TECH_CFG, regime="")
    # Both use same default weights (no regime_scoring_weights in TECH_CFG)
    # and no clamping applied — scores should be equal
    assert ds_ranging.score == ds_none.score, (
        f"Ranging should not clamp scores: {ds_ranging.score} vs {ds_none.score}"
    )
