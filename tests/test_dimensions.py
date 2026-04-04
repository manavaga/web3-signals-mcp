# tests/test_dimensions.py
from scoring.types import DimensionScore
from scoring.dimensions import (
    score_technical, score_derivatives, score_market,
    score_narrative, score_exchange_flow, detect_data_tier
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
        "scoring_weights": {"rsi": 0.25, "macd": 0.25, "bollinger": 0.20, "trend": 0.30},
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


def test_detect_data_tier():
    assert detect_data_tier(65.0, "RSI=42, MACD bullish, BB=0.3") == "full"
    assert detect_data_tier(50.0, "no data available") == "none"
    assert detect_data_tier(50.0, "error: API timeout") == "none"
    assert detect_data_tier(50.0, "partial data") == "partial"
