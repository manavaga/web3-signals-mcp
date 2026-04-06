from tools.indicators import compute_technical_indicators


def test_cleaned_indicators_exclude_dead_weight():
    """Dead indicators removed from output."""
    candles = [{"open": 100, "high": 105, "low": 95, "close": 100 + i * 0.1, "volume": 1000}
               for i in range(60)]
    result = compute_technical_indicators(candles)
    for dead in ("bb_position", "stoch_rsi", "roc_1d", "roc_30d", "mfi", "bb_squeeze"):
        assert dead not in result, f"{dead} should have been removed"
    for alive in ("rsi_14", "macd_histogram", "bb_bandwidth", "obv_slope",
                   "roc_7d", "squeeze_on", "squeeze_momentum", "macd_zscore"):
        assert alive in result, f"{alive} should still exist"


def test_no_duplicate_bb_keys():
    """bb_upper and bb_lower should appear exactly once."""
    candles = [{"open": 100, "high": 105, "low": 95, "close": 100 + i * 0.1, "volume": 1000}
               for i in range(60)]
    result = compute_technical_indicators(candles)
    assert "bb_upper" in result
    assert "bb_lower" in result


def test_funding_accel_computation():
    from tools.indicators import calc_funding_accel
    rates = [0.001, 0.002, 0.005, 0.003, 0.001]
    result = calc_funding_accel(rates)
    assert len(result) == 4
    assert abs(result[0] - 0.001) < 1e-6
    assert abs(result[1] - 0.003) < 1e-6
    assert abs(result[2] - (-0.002)) < 1e-6

def test_funding_accel_edge_cases():
    from tools.indicators import calc_funding_accel
    assert calc_funding_accel([]) == []
    assert calc_funding_accel([0.001]) == []

def test_oi_accel_computation():
    from tools.indicators import calc_oi_accel
    oi_changes = [5.0, 8.0, 3.0, -2.0]
    result = calc_oi_accel(oi_changes)
    assert len(result) == 3
    assert abs(result[0] - 3.0) < 1e-6
    assert abs(result[1] - (-5.0)) < 1e-6

def test_vol_price_divergence():
    from tools.indicators import calc_vol_price_divergence
    # Price up, volume down = bearish divergence
    price_changes = [1.0, 2.0, 3.0, 4.0, 5.0]
    volumes = [100, 80, 60, 40, 20]
    div = calc_vol_price_divergence(price_changes, volumes)
    assert div < -0.5

def test_vol_price_divergence_edge_cases():
    from tools.indicators import calc_vol_price_divergence
    assert calc_vol_price_divergence([], [], window=5) == 0.0
    assert calc_vol_price_divergence([1, 2, 3], [1, 2, 3], window=5) == 0.0

def test_liq_density():
    from tools.historical_fetcher import calc_liq_density
    liqs = [
        {"price": 100.0, "qty": 10.0, "side": "SELL", "time": 0},
        {"price": 101.0, "qty": 5.0, "side": "BUY", "time": 0},
        {"price": 150.0, "qty": 20.0, "side": "SELL", "time": 0},
    ]
    density = calc_liq_density(liqs, current_price=100.0, range_pct=2.0)
    # 100 and 101 are within 2% of 100. Total nearby=15, total=35
    assert abs(density - 15.0/35.0) < 0.01

def test_liq_density_empty():
    from tools.historical_fetcher import calc_liq_density
    assert calc_liq_density([], 100.0) == 0.0
    assert calc_liq_density([{"price": 100, "qty": 5, "side": "BUY", "time": 0}], 0) == 0.0
