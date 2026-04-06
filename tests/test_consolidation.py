def test_trade_simulator_no_duplicate_target_calc():
    """trade_simulator should not have its own calculate_trade_targets."""
    import inspect
    import tools.trade_simulator as ts
    members = [name for name, _ in inspect.getmembers(ts, inspect.isfunction)]
    assert "calculate_trade_targets" not in members, \
        "calculate_trade_targets should be removed — use modifiers.calculate_targets"
