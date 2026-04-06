from risk.manager import RiskManager


def test_confidence_based_sizing():
    rm = RiskManager(learned_params={
        "risk": {"base_position_pct": 10.0, "min_position_pct": 3.0, "max_position_pct": 20.0}
    })
    size_high = rm.size_position("BTC", "bearish", confidence=0.9)
    size_low = rm.size_position("BTC", "bearish", confidence=0.3)
    assert size_high > size_low
    assert 3.0 <= size_high <= 20.0
    assert 3.0 <= size_low <= 20.0


def test_sizing_at_extremes():
    rm = RiskManager(learned_params={
        "risk": {"base_position_pct": 10.0, "min_position_pct": 3.0, "max_position_pct": 20.0}
    })
    assert rm.size_position("BTC", "bullish", confidence=0.0) == 3.0
    assert rm.size_position("BTC", "bullish", confidence=1.0) == 20.0


def test_daily_loss_cap():
    rm = RiskManager(learned_params={"risk": {"daily_loss_cap_pct": -5.0}})
    assert rm.check_daily_loss_cap(-3.0) is True
    assert rm.check_daily_loss_cap(-5.0) is False
    assert rm.check_daily_loss_cap(-7.0) is False


def test_max_open_trades():
    rm = RiskManager(learned_params={"risk": {"max_open_trades": 3}})
    assert rm.check_max_open_trades(2) is True
    assert rm.check_max_open_trades(3) is False
    assert rm.check_max_open_trades(5) is False


def test_correlation_filter_blocks_overcrowding():
    rm = RiskManager(learned_params={"risk": {"max_correlated_trades": 2}})
    open_trades = [
        {"asset": "BTC", "direction": "bearish"},
        {"asset": "ETH", "direction": "bearish"},
    ]
    # BTC and ETH are in same group (large_cap), already 2 bearish
    assert rm.check_correlation_filter("ETH", "bearish", open_trades) is False


def test_correlation_filter_allows_different_direction():
    rm = RiskManager(learned_params={"risk": {"max_correlated_trades": 2}})
    open_trades = [
        {"asset": "BTC", "direction": "bearish"},
        {"asset": "ETH", "direction": "bearish"},
    ]
    # Bullish trade in same group is fine
    assert rm.check_correlation_filter("BTC", "bullish", open_trades) is True


def test_correlation_filter_allows_different_group():
    rm = RiskManager(learned_params={"risk": {"max_correlated_trades": 2}})
    open_trades = [
        {"asset": "BTC", "direction": "bearish"},
        {"asset": "ETH", "direction": "bearish"},
    ]
    # SOL is in l1_alts group, not large_cap
    assert rm.check_correlation_filter("SOL", "bearish", open_trades) is True


def test_default_params():
    rm = RiskManager(learned_params={})
    size = rm.size_position("BTC", "bearish", confidence=0.5)
    assert 1.0 <= size <= 25.0
