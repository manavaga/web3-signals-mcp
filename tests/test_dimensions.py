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
            "rsi": 0.10, "macd": 0.10, "bollinger": 0.10, "trend": 0.15,
            "obv": 0.15, "mfi": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "stoch_rsi": 0.05,
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
    """New indicators (OBV, MFI, ROC, StochRSI, squeeze) should influence the score."""
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
        "mfi": 25.0,               # near oversold = bullish
        "roc_7d": 3.0,             # positive momentum
        "stoch_rsi": 0.15,         # oversold = bullish
        "squeeze_on": False,
        "squeeze_momentum": 2.0,   # bullish breakout
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.10, "macd": 0.10, "bollinger": 0.10, "trend": 0.15,
            "obv": 0.15, "mfi": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "stoch_rsi": 0.05,
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
        "mfi": 82.0,
        "roc_7d": -5.0,
        "stoch_rsi": 0.9,
        "squeeze_on": False,
        "squeeze_momentum": -3.0,
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.10, "macd": 0.10, "bollinger": 0.10, "trend": 0.15,
            "obv": 0.15, "mfi": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "stoch_rsi": 0.05,
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
        "mfi": 50.0,
        "roc_7d": 0.0,
        "stoch_rsi": 0.5,
        "squeeze_on": True,
        "squeeze_momentum": 5.0,  # momentum ignored when squeeze is on
    }
    cfg = {
        "scoring_weights": {
            "rsi": 0.10, "macd": 0.10, "bollinger": 0.10, "trend": 0.15,
            "obv": 0.15, "mfi": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "stoch_rsi": 0.05,
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
            "rsi": 0.10, "macd": 0.10, "bollinger": 0.10, "trend": 0.15,
            "obv": 0.15, "mfi": 0.15, "roc_7d": 0.10, "squeeze": 0.10, "stoch_rsi": 0.05,
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
