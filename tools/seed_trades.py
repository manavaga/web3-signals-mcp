# tools/seed_trades.py
"""Seed the trades table with backtest results for dashboard display.

Imports trade simulation results into the live database so the
Trades & P&L dashboard tab has data from day one.

Usage:
    python3 -m tools.seed_trades --json /tmp/trades_learned.json
    python3 -m tools.seed_trades --simulate --days 180
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from storage.db import Storage

logger = logging.getLogger(__name__)


def seed_from_json(json_path: str, db_path: str = "signals.db",
                   position_size: float = 1000.0) -> int:
    """Seed trades from exported JSON trade log.

    Args:
        json_path: Path to trades JSON (from trade_simulator --export).
        db_path: Path to signals database.
        position_size: USD per trade for P&L calculation.

    Returns: Number of trades seeded.
    """
    with open(json_path) as f:
        trades = json.load(f)

    storage = Storage(db_path=db_path)
    count = 0

    for t in trades:
        pnl_usd = position_size * (t["pnl_pct"] / 100)

        trade_id = storage.save_trade(
            asset=t["asset"],
            direction=t["direction"],
            composite_score=t.get("composite", 50),
            entry_price=t["entry"],
            target_price=t["target"],
            stop_loss=t["stop_loss"],
            risk_reward_ratio=t.get("rr", 0),
            confidence=t.get("confidence", ""),
            regime="",
            position_size_usd=position_size,
            source="backtest",
        )

        storage.close_trade(
            trade_id=trade_id,
            exit_price=t.get("exit_price", t["entry"]),
            outcome=t.get("outcome", "unknown"),
            pnl_pct=round(t["pnl_pct"], 2),
            pnl_usd=round(pnl_usd, 2),
        )
        count += 1

    return count


def main():
    parser = argparse.ArgumentParser(description="Seed trades table")
    parser.add_argument("--json", type=str, required=True, help="Path to trades JSON")
    parser.add_argument("--db", type=str, default="signals.db", help="Database path")
    parser.add_argument("--position-size", type=float, default=1000.0,
                        help="USD per trade (default: $1000)")
    args = parser.parse_args()

    logging.basicConfig(level="INFO")

    count = seed_from_json(args.json, db_path=args.db,
                           position_size=args.position_size)
    print(f"Seeded {count} trades into {args.db}")


if __name__ == "__main__":
    main()
