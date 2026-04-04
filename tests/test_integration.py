# tests/test_integration.py
"""End-to-end: fixture data -> pipeline -> valid signals."""
import json
import os
from scoring.config import load_config, load_assets
from scoring.pipeline import fuse_signals
from scoring.types import Signal


def test_full_pipeline_from_fixture():
    base = os.path.dirname(__file__)
    root = os.path.join(base, "..")

    config = load_config(os.path.join(root, "config.yaml"))
    assets = load_assets(os.path.join(root, "assets.yaml"))

    with open(os.path.join(base, "fixtures", "agent_snapshot.json")) as f:
        agent_data = json.load(f)

    signals = fuse_signals(agent_data, config, assets)

    assert len(signals) > 0
    for asset, sig in signals.items():
        assert isinstance(sig, Signal)
        assert 0 <= sig.composite <= 100
        assert sig.label in ["STRONG BUY", "MODERATE BUY", "NEUTRAL",
                              "MODERATE SELL", "STRONG SELL", "INSUFFICIENT EDGE"]
        assert sig.direction in ["bullish", "bearish", "neutral"]

        # Weights should sum to ~1.0 for assets with data, or 0 for no-data assets
        total_w = sum(sig.weights_used.values())
        has_data = any(d.tier != "none" for d in sig.dimensions.values())
        if has_data:
            assert abs(total_w - 1.0) < 0.01, f"{asset} weights sum to {total_w}"

        # Directional non-abstained signals with ATR data should have targets
        if not sig.abstained and sig.direction != "neutral":
            if sig.targets:
                assert sig.targets.risk_reward_ratio >= 0
                if sig.direction == "bullish":
                    assert sig.targets.target_price > sig.targets.entry_price
                    assert sig.targets.stop_loss < sig.targets.entry_price


def test_serialization_roundtrip():
    base = os.path.dirname(__file__)
    root = os.path.join(base, "..")

    config = load_config(os.path.join(root, "config.yaml"))
    assets = load_assets(os.path.join(root, "assets.yaml"))

    with open(os.path.join(base, "fixtures", "agent_snapshot.json")) as f:
        agent_data = json.load(f)

    signals = fuse_signals(agent_data, config, assets)

    for asset, sig in signals.items():
        d = sig.to_dict()
        assert isinstance(d, dict)
        j = json.dumps(d)  # must be JSON-serializable
        parsed = json.loads(j)
        assert parsed["asset"] == asset
        assert parsed["composite"] == sig.composite
