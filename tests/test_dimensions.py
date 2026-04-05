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
        "bb_position": 0.2,
        "price": 84000.0,
        "ma7": 83000.0,
        "ma30": 82000.0,
        "volume_status": "normal",
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.20, "macd": 0.10, "bollinger": 0.10, "trend": 0.20,
            "obv": 0.20, "roc_7d": 0.10, "squeeze": 0.10,
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
    """New indicators (OBV, ROC, squeeze) should influence the score."""
    data = {
        "rsi_14": 35.0,
        "macd_histogram": 0.5,
        "bb_position": 0.2,
        "price": 84000.0,
        "ma7": 83000.0,
        "ma30": 82000.0,
        "volume_status": "normal",
        # New indicators — bullish setup
        "obv_slope": 0.05,         # positive slope = bullish
        "roc_7d": 3.0,             # positive momentum
        "squeeze_on": False,
        "squeeze_momentum": 2.0,   # bullish breakout
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.20, "macd": 0.10, "bollinger": 0.10, "trend": 0.20,
            "obv": 0.20, "roc_7d": 0.10, "squeeze": 0.10,
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
        "bb_position": 0.9,
        "price": 84000.0,
        "ma7": 85000.0,
        "ma30": 86000.0,
        "volume_status": "normal",
        # Bearish setup
        "obv_slope": -0.08,
        "roc_7d": -5.0,
        "squeeze_on": False,
        "squeeze_momentum": -3.0,
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.20, "macd": 0.10, "bollinger": 0.10, "trend": 0.20,
            "obv": 0.20, "roc_7d": 0.10, "squeeze": 0.10,
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
        "bb_position": 0.5,
        "price": 84000.0,
        "ma7": 84000.0,
        "ma30": 84000.0,
        "volume_status": "normal",
        "obv_slope": 0.0,
        "roc_7d": 0.0,
        "squeeze_on": True,
        "squeeze_momentum": 5.0,  # momentum ignored when squeeze is on
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.20, "macd": 0.10, "bollinger": 0.10, "trend": 0.20,
            "obv": 0.20, "roc_7d": 0.10, "squeeze": 0.10,
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
        "bb_position": 0.5,
        "price": 84000.0,
        "ma7": 84000.0,
        "ma30": 84000.0,
        "volume_status": "normal",
        # No new indicator fields
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.20, "macd": 0.10, "bollinger": 0.10, "trend": 0.20,
            "obv": 0.20, "roc_7d": 0.10, "squeeze": 0.10,
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
        "rsi": 0.20, "macd": 0.10, "bollinger": 0.10, "trend": 0.20,
        "obv": 0.20, "roc_7d": 0.10, "squeeze": 0.10,
    },
    "volume_spike_bonus": 10,
    "rsi_oversold": 30, "rsi_overbought": 70,
}


def test_regime_trending_down_suppresses_rsi_buy():
    """In trending_down, RSI oversold should NOT generate buy signal (clamped to ≤50)."""
    data = {
        "rsi_14": 20.0,  # very oversold — would normally score ~90
        "macd_histogram": -0.5,
        "bb_position": -0.1,  # below lower band — would normally score ~90
        "price": 75000.0,
        "ma7": 78000.0,
        "ma30": 82000.0,
        "volume_status": "normal",
        "obv_slope": -0.05,
        "roc_7d": -8.0,
        "squeeze_on": False,
        "squeeze_momentum": -2.0,
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
        "bb_position": 1.1,  # above upper band — would normally score ~10
        "price": 90000.0,
        "ma7": 88000.0,
        "ma30": 85000.0,
        "volume_status": "normal",
        "obv_slope": 0.05,
        "roc_7d": 6.0,
        "squeeze_on": False,
        "squeeze_momentum": 2.0,
    }
    ds_no_regime = score_technical(data, TECH_CFG)
    ds_trending = score_technical(data, TECH_CFG, regime="trending_up")
    assert ds_trending.score > ds_no_regime.score, (
        f"trending_up should suppress mean-reversion sells: {ds_trending.score} vs {ds_no_regime.score}"
    )


def test_regime_ranging_passes_through():
    """In ranging regime, all signals pass through normally."""
    data = {
        "rsi_14": 25.0,
        "macd_histogram": 0.1,
        "bb_position": 0.1,
        "price": 82000.0,
        "ma7": 82000.0,
        "ma30": 82000.0,
        "volume_status": "normal",
    }
    ds_ranging = score_technical(data, TECH_CFG, regime="ranging")
    ds_none = score_technical(data, TECH_CFG, regime="")
    assert ds_ranging.score == ds_none.score, "Ranging should not modify scores"
