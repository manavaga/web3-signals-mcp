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
