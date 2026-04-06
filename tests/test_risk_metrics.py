from tools.trade_simulator import compute_risk_metrics, apply_fee_model

def test_sharpe_ratio_positive_edge():
    trades = [
        {"pnl_pct": 2.0, "date": "2026-01-01"},
        {"pnl_pct": -1.0, "date": "2026-01-02"},
        {"pnl_pct": 3.0, "date": "2026-01-03"},
        {"pnl_pct": 1.5, "date": "2026-01-04"},
        {"pnl_pct": -0.5, "date": "2026-01-05"},
    ]
    metrics = compute_risk_metrics(trades)
    assert metrics["sharpe_ratio"] > 0
    assert metrics["sortino_ratio"] > 0
    assert metrics["calmar_ratio"] > 0

def test_sharpe_all_losses():
    trades = [{"pnl_pct": -2.0, "date": f"2026-01-{i:02d}"} for i in range(1, 11)]
    metrics = compute_risk_metrics(trades)
    assert metrics["sharpe_ratio"] < 0

def test_monte_carlo_p_value():
    trades = [{"pnl_pct": 2.0, "date": f"2026-01-{i:02d}"} for i in range(1, 21)]
    metrics = compute_risk_metrics(trades, monte_carlo_n=100)
    assert 0 <= metrics["monte_carlo"]["p_value"] <= 1
    assert "pnl_5th" in metrics["monte_carlo"]
    assert "pnl_95th" in metrics["monte_carlo"]

def test_max_drawdown_duration():
    trades = [
        {"pnl_pct": 5.0, "date": "2026-01-01"},
        {"pnl_pct": -2.0, "date": "2026-01-02"},
        {"pnl_pct": -1.0, "date": "2026-01-03"},
        {"pnl_pct": -1.0, "date": "2026-01-04"},
        {"pnl_pct": 5.0, "date": "2026-01-05"},
    ]
    metrics = compute_risk_metrics(trades)
    assert metrics["max_dd_duration_days"] >= 2

def test_regime_split():
    trades = [
        {"pnl_pct": 3.0, "date": "2026-01-01", "regime": "trending_down"},
        {"pnl_pct": -1.0, "date": "2026-01-02", "regime": "ranging"},
        {"pnl_pct": 2.0, "date": "2026-01-03", "regime": "trending_down"},
    ]
    metrics = compute_risk_metrics(trades)
    assert "trending_down" in metrics["regime_split"]
    assert "ranging" in metrics["regime_split"]
    assert metrics["regime_split"]["trending_down"]["win_rate"] == 1.0

def test_empty_trades():
    metrics = compute_risk_metrics([])
    assert metrics["sharpe_ratio"] == 0
    assert metrics["monte_carlo"]["p_value"] == 1.0

def test_fee_adjusted_pnl():
    fee_cfg = {"base_fee_pct": 0.1, "spread_pct": 0.05, "slippage_multiplier": 1.0}
    pnl = apply_fee_model(2.0, fee_cfg)
    # 2 legs * (0.1 + 0.05) = 0.30 total cost
    assert abs(pnl - 1.70) < 0.01

def test_fee_model_makes_marginal_trades_negative():
    fee_cfg = {"base_fee_pct": 0.1, "spread_pct": 0.05, "slippage_multiplier": 1.0}
    pnl = apply_fee_model(0.2, fee_cfg)
    assert pnl < 0

def test_fee_model_default_config():
    pnl = apply_fee_model(5.0, {})
    # Default: 2 * (0.10 + 0.05) = 0.30
    assert abs(pnl - 4.70) < 0.01
