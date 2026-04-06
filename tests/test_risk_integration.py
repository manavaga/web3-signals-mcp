from tools.learned_params import LearnedState, AssetLearnedParams, DirectionParams, _learn_risk_params

def test_learned_state_has_risk_params():
    state = LearnedState()
    assert hasattr(state, "risk_params")
    assert isinstance(state.risk_params, dict)

def test_learn_risk_params_from_assets():
    assets = {
        "BTC": AssetLearnedParams(
            asset="BTC",
            daily_volatility_pct=2.5,
            bullish=DirectionParams(expected_value=-0.5),
            bearish=DirectionParams(expected_value=0.8),
        ),
        "ETH": AssetLearnedParams(
            asset="ETH",
            daily_volatility_pct=3.5,
            bullish=DirectionParams(expected_value=-0.3),
            bearish=DirectionParams(expected_value=1.2),
        ),
    }
    risk = _learn_risk_params(assets)
    assert risk["base_position_pct"] > 0
    assert risk["min_position_pct"] < risk["base_position_pct"]
    assert risk["max_position_pct"] > risk["base_position_pct"]
    assert risk["daily_loss_cap_pct"] < 0
    assert risk["max_open_trades"] >= 2

def test_learn_risk_params_empty():
    assert _learn_risk_params({}) == {}

def test_risk_params_persistence():
    from tools.learned_params import save_learned_state, load_learned_state
    from pathlib import Path
    import tempfile

    state = LearnedState(risk_params={"base_position_pct": 8.5, "max_open_trades": 4})
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)
    save_learned_state(state, path)
    loaded = load_learned_state(path)
    assert loaded.risk_params["base_position_pct"] == 8.5
    assert loaded.risk_params["max_open_trades"] == 4
    path.unlink()
