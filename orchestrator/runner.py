# orchestrator/runner.py
"""Scheduler — runs agents on cadence, fusion every 12h, evaluates signals.

After fusion, evaluates old signals (48h+) against actual prices,
computes IC per dimension, and proposes weight updates (shadow mode).
"""
from __future__ import annotations
import argparse
import logging
import os
import time
from datetime import datetime, timezone

from scoring.config import load_config, load_assets
from scoring.pipeline import fuse_signals
from storage.db import Storage

logger = logging.getLogger(__name__)

SIGNAL_CADENCE_HOURS = int(os.getenv("SIGNAL_CADENCE_HOURS", "12"))
TRADE_EVAL_INTERVAL_MIN = int(os.getenv("TRADE_EVAL_INTERVAL_MIN", "15"))


def _load_agents(config, assets_cfg, storage=None):
    symbols = {a: assets_cfg.get(a).binance_symbol for a in assets_cfg.enabled_assets()}
    agents = []

    from agents.technical import TechnicalAgent
    agents.append(("technical_agent", TechnicalAgent(config.agents.technical.model_dump(), symbols),
                   config.agents.technical.cadence_minutes))

    try:
        from agents.derivatives import DerivativesAgent
        agents.append(("derivatives_agent", DerivativesAgent(config.agents.derivatives.model_dump(), symbols, storage=storage),
                       config.agents.derivatives.cadence_minutes))
    except ImportError:
        pass

    try:
        from agents.market import MarketAgent
        coingecko_ids = {a: assets_cfg.get(a).coingecko_id for a in assets_cfg.enabled_assets()}
        agents.append(("market_agent", MarketAgent(config.agents.market.model_dump(), symbols, coingecko_ids, storage=storage),
                       config.agents.market.cadence_minutes))
    except ImportError:
        pass

    return agents


def run_cycle(config, assets_cfg, storage, agents, last_runs, last_fusion, force=False):
    now = time.time()

    for name, agent, cadence_min in agents:
        last = last_runs.get(name, 0)
        if force or (now - last) >= cadence_min * 60:
            if not agent.circuit_breaker.allow_request():
                logger.warning(f"Circuit breaker open for {name}, skipping")
                continue
            logger.info(f"Running {name}...")
            result = agent.execute()
            storage.save(name, result.get("data", {}))
            last_runs[name] = now
            logger.info(f"{name}: {result['status']} ({result['meta']['duration_ms']}ms)")

    if force or (now - last_fusion) >= SIGNAL_CADENCE_HOURS * 3600:
        logger.info("Running signal fusion...")
        agent_names = [n for n, _, _ in agents]
        raw = storage.load_all_latest(agent_names)

        agent_data = {}
        for name in ["technical", "derivatives", "market"]:
            full_name = f"{name}_agent"
            agent_data[name] = raw.get(full_name) or {}

        signals = fuse_signals(agent_data, config, assets_cfg)

        fusion_data = {asset: sig.to_dict() for asset, sig in signals.items()}
        storage.save("signal_fusion", fusion_data)

        for asset, sig in signals.items():
            entry_price = sig.targets.entry_price if sig.targets else 0
            if entry_price > 0:
                storage.save_performance_snapshot(
                    asset=asset, signal_score=sig.composite,
                    signal_direction=sig.direction,
                    price_at_signal=entry_price,
                    sources_count=sum(1 for d in sig.dimensions.values() if d.tier != "none"),
                    detail=sig.label,
                )

        logger.info(f"Fusion complete: {len(signals)} signals")

        # --- Record trades for directional signals with targets ---
        _record_trades(storage, signals)

        # --- Learning layer: evaluate old signals, compute IC, propose weights ---
        _run_evaluation_cycle(config, assets_cfg, storage, signals)

        last_fusion = now

    # --- Evaluate open trades EVERY cycle (not just on fusion) ---
    _evaluate_open_trades(storage)

    return last_fusion


def _record_trades(storage, signals):
    """Record directional signals as trades for P&L tracking."""
    try:
        for asset, sig in signals.items():
            if sig.abstained or sig.direction == "neutral" or not sig.targets:
                continue

            t = sig.targets
            if t.entry_price <= 0:
                continue

            trade_id = storage.save_trade(
                asset=asset,
                direction=sig.direction,
                composite_score=sig.composite,
                entry_price=t.entry_price,
                target_price=t.target_price,
                stop_loss=t.stop_loss,
                risk_reward_ratio=t.risk_reward_ratio,
                confidence=t.confidence,
                regime=sig.regime.regime,
                source="live",
            )
            logger.info(f"Trade recorded: {asset} {sig.direction} "
                       f"entry=${t.entry_price} tp=${t.target_price} sl=${t.stop_loss} "
                       f"(trade #{trade_id})")
    except Exception as e:
        logger.error(f"Trade recording error (non-fatal): {e}")


def _evaluate_open_trades(storage):
    """Check open trades against current prices for TP/SL hits."""
    try:
        open_trades = storage.get_open_trades()
        if not open_trades:
            return

        for trade in open_trades:
            asset = trade["asset"]
            current_price = _get_current_price(asset, storage)
            if current_price is None:
                continue

            direction = trade["direction"]
            tp = trade["target_price"]
            sl = trade["stop_loss"]
            entry = trade["entry_price"]
            entry_time = trade.get("entry_time", "")

            # Check age — if > 48h, expire the trade
            try:
                entry_dt = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
            except Exception:
                age_hours = 0

            outcome = None
            exit_price = current_price

            if direction == "bullish":
                if current_price >= tp:
                    outcome = "tp_hit"
                    exit_price = tp
                elif current_price <= sl:
                    outcome = "sl_hit"
                    exit_price = sl
                elif age_hours >= 48:
                    outcome = "expired_win" if current_price > entry else "expired_loss"
            else:  # bearish
                if current_price <= tp:
                    outcome = "tp_hit"
                    exit_price = tp
                elif current_price >= sl:
                    outcome = "sl_hit"
                    exit_price = sl
                elif age_hours >= 48:
                    outcome = "expired_win" if current_price < entry else "expired_loss"

            if outcome:
                if direction == "bullish":
                    pnl_pct = (exit_price - entry) / entry * 100
                else:
                    pnl_pct = (entry - exit_price) / entry * 100

                pos_size = trade.get("position_size_usd", 0) or 0
                pnl_usd = pos_size * (pnl_pct / 100) if pos_size > 0 else 0

                storage.close_trade(
                    trade_id=trade["id"],
                    exit_price=round(exit_price, 2),
                    outcome=outcome,
                    pnl_pct=round(pnl_pct, 2),
                    pnl_usd=round(pnl_usd, 2),
                )
                logger.info(f"Trade closed: {asset} {direction} → {outcome} "
                           f"PnL: {pnl_pct:+.2f}%")

    except Exception as e:
        logger.error(f"Trade evaluation error (non-fatal): {e}")


def _run_evaluation_cycle(config, assets_cfg, storage, current_signals):
    """Evaluate 48h-old signals, compute IC, propose weight updates."""
    try:
        from learning.evaluation import gradient_score, compute_cwa, detect_drift
        from learning.optimizer import compute_ic, propose_weight_update

        # 1. Evaluate unevaluated snapshots (48h old)
        for window in config.evaluation.windows_hours:
            unevaluated = storage.load_unevaluated_snapshots(window_hours=window)
            if not unevaluated:
                continue

            logger.info(f"Evaluating {len(unevaluated)} signals for {window}h window")
            for snap in unevaluated:
                current_price = _get_current_price(snap["asset"], storage)
                if current_price is None:
                    continue

                pct_change = ((current_price - snap["price_at_signal"]) / snap["price_at_signal"]) * 100
                asset_cfg = assets_cfg.get(snap["asset"])
                score = gradient_score(
                    direction=snap["signal_direction"],
                    pct_change=pct_change,
                    noise_pct=asset_cfg.noise_threshold_pct,
                    strong_pct=asset_cfg.strong_threshold_pct,
                    thresholds=config.evaluation.gradient_thresholds.model_dump(),
                )
                storage.save_performance_accuracy(
                    snapshot_id=snap["id"],
                    window_hours=window,
                    price_at_window=current_price,
                    gradient_score=score,
                    pct_change=pct_change,
                )
                logger.debug(f"Evaluated {snap['asset']} snap#{snap['id']}: "
                             f"pct={pct_change:.2f}%, score={score}")

        # 2. Save dimension scores for IC computation
        for asset, sig in current_signals.items():
            dim_scores = {name: ds.score for name, ds in sig.dimensions.items()}
            # Use a placeholder snapshot_id of 0 — will be linked later
            storage.save_dimension_scores(
                snapshot_id=0,
                dimension_scores=dim_scores,
                config_version="1.0",
                regime=sig.regime.regime,
            )

        # 3. Compute IC + propose weight updates (shadow mode)
        if config.learning.shadow_mode:
            _run_shadow_optimizer(config, storage)

    except Exception as e:
        logger.error(f"Learning cycle error (non-fatal): {e}")


def _get_current_price(asset: str, storage) -> float | None:
    """Get current price — tries live Binance API first, falls back to stored data."""
    # Try live Binance price first (most accurate for trade evaluation)
    try:
        import urllib.request, json as _json
        from scoring.config import load_assets as _load_assets
        assets_cfg = _load_assets()
        symbol = assets_cfg.get(asset).binance_symbol
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = _json.loads(resp.read())
            price = float(data["price"])
            if price > 0:
                return price
    except Exception as e:
        logger.debug(f"Live price fetch failed for {asset}: {e}")

    # Fallback to stored agent data
    try:
        data = storage.load_latest("technical_agent")
        if data and asset in data:
            asset_data = data[asset]
            if isinstance(asset_data, dict):
                return asset_data.get("close") or asset_data.get("price")
    except Exception:
        pass
    try:
        data = storage.load_latest("market_agent")
        if data and asset in data:
            asset_data = data[asset]
            if isinstance(asset_data, dict):
                return asset_data.get("price") or asset_data.get("current_price")
    except Exception:
        pass
    return None


def _run_shadow_optimizer(config, storage):
    """Compute IC per dimension, propose weight updates, log but don't apply."""
    from learning.optimizer import compute_ic, propose_weight_update

    try:
        # Load recent dimension scores and accuracy results
        conn = storage._connect()
        try:
            cur = conn.cursor()
            ph = storage._ph()
            cur.execute(
                """SELECT ic.dimension_scores, pa.gradient_score, ic.regime
                   FROM ic_dimension_scores ic
                   JOIN performance_accuracy pa ON ic.snapshot_id = pa.snapshot_id
                   WHERE pa.window_hours = 48
                   ORDER BY ic.id DESC LIMIT 100"""
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        if len(rows) < config.learning.ic_min_observations:
            logger.info(f"Shadow optimizer: {len(rows)} observations, need {config.learning.ic_min_observations}")
            return

        import json
        dim_scores = [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows]
        outcomes = [r[1] for r in rows]

        ics = compute_ic(dim_scores, outcomes)
        if ics:
            logger.info(f"Shadow IC: {ics}")
            proposed = propose_weight_update(
                current_weights=config.scoring.weights_default,
                ics=ics,
                step_size=config.learning.weight_step_size,
            )
            logger.info(f"Shadow proposed weights: {proposed}")
            # Save proposed weights for review (shadow mode — never auto-apply)
            storage.save_kv_json("learning", "shadow_proposed_weights", {
                "proposed": proposed,
                "ics": ics,
                "observations": len(rows),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    except Exception as e:
        logger.error(f"Shadow optimizer error: {e}")


def main():
    parser = argparse.ArgumentParser(description="Web3 Signals Orchestrator")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=3600, help="Sleep between cycles (seconds)")
    parser.add_argument("--db", default="signals.db", help="SQLite path")
    args = parser.parse_args()

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    config = load_config()
    assets_cfg = load_assets()
    storage = Storage(db_path=args.db)

    agents = _load_agents(config, assets_cfg, storage=storage)
    last_runs = {}
    last_fusion = 0.0

    if args.once:
        run_cycle(config, assets_cfg, storage, agents, last_runs, last_fusion, force=True)
        return

    last_trade_eval = 0.0
    trade_eval_secs = TRADE_EVAL_INTERVAL_MIN * 60

    while True:
        try:
            last_fusion = run_cycle(config, assets_cfg, storage, agents, last_runs, last_fusion)
        except Exception as e:
            logger.error(f"Cycle error: {e}")

        # Evaluate open trades on a faster cadence (default 15 min)
        now = time.time()
        if (now - last_trade_eval) >= trade_eval_secs:
            try:
                _evaluate_open_trades(storage)
                last_trade_eval = now
            except Exception as e:
                logger.error(f"Trade eval error: {e}")

        time.sleep(min(args.interval, trade_eval_secs))


if __name__ == "__main__":
    main()
