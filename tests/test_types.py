# tests/test_types.py
from scoring.types import DimensionScore, RegimeContext, TargetLevels, Signal


def test_dimension_score_is_frozen():
    ds = DimensionScore(name="technical", score=65.0, detail="RSI=42, MACD bullish", tier="full")
    assert ds.score == 65.0
    try:
        ds.score = 70.0
        assert False, "Should be frozen"
    except AttributeError:
        pass


def test_dimension_score_clamped():
    ds = DimensionScore(name="technical", score=150.0, detail="test", tier="full")
    assert ds.score == 100.0
    ds2 = DimensionScore(name="technical", score=-10.0, detail="test", tier="full")
    assert ds2.score == 0.0


def test_regime_context_defaults():
    rc = RegimeContext(regime="trending_up", fg_value=45, fg_regime="neutral", btc_pct_from_ma30=0.12)
    assert rc.regime == "trending_up"
    assert rc.fg_regime == "neutral"


def test_signal_has_all_fields():
    dims = {"technical": DimensionScore(name="technical", score=65.0, detail="test", tier="full")}
    target = TargetLevels(
        entry_price=84000.0, target_price=87000.0, stop_loss=82000.0,
        risk_reward_ratio=1.5, predicted_move_pct=3.57, confidence="medium",
        timeframe_hours=48
    )
    sig = Signal(
        asset="BTC", composite=63.2, label="MODERATE BUY", direction="bullish",
        dimensions=dims, weights_used={"technical": 0.40, "market": 0.55, "derivatives": 0.05},
        regime=RegimeContext(regime="trending_up", fg_value=45, fg_regime="neutral", btc_pct_from_ma30=0.12),
        targets=target, momentum="stable", abstained=False
    )
    assert sig.composite == 63.2
    assert sig.targets.risk_reward_ratio == 1.5


def test_signal_to_dict():
    dims = {"technical": DimensionScore(name="technical", score=65.0, detail="test", tier="full")}
    sig = Signal(
        asset="BTC", composite=63.2, label="MODERATE BUY", direction="bullish",
        dimensions=dims, weights_used={"technical": 1.0},
        regime=RegimeContext(regime="trending_up", fg_value=45, fg_regime="neutral", btc_pct_from_ma30=0.12),
        targets=None, momentum="stable", abstained=False
    )
    d = sig.to_dict()
    assert d["asset"] == "BTC"
    assert d["composite"] == 63.2
    assert "technical" in d["dimensions"]
