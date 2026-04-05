"""Historical data fetcher for backtesting.

Fetches 180 days of klines + macro data and stores in SQLite + Postgres.

Usage:
    python3 -m tools.historical_fetcher --days 180
    python3 -m tools.historical_fetcher --days 30 --assets BTC,ETH
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import yaml

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = str(Path(__file__).resolve().parent.parent / "backtest_data.db")

MACRO_TICKERS = [
    ("SPY", "sp500"),
    ("DX-Y.NYB", "dxy"),
    ("QQQ", "nasdaq"),
    ("^VIX", "vix"),
]


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_klines(symbol: str, interval: str = "1d", days: int = 180) -> list[dict]:
    """Fetch daily OHLCV from Binance. Returns list of candle dicts."""
    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
    all_candles: list[dict] = []

    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "limit": 1000,
        }
        resp = requests.get(
            "https://api.binance.com/api/v3/klines", params=params, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break

        for c in data:
            all_candles.append({
                "timestamp": c[0],  # Open time in ms
                "date": datetime.utcfromtimestamp(c[0] / 1000).strftime("%Y-%m-%d"),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            })
        start_ms = data[-1][0] + 1  # Next page
        time.sleep(0.1)  # Rate limit

    return all_candles


def fetch_macro(days: int = 180) -> dict[str, list[dict]]:
    """Fetch S&P, DXY, NASDAQ, VIX historical via yfinance."""
    if yf is None:
        print("  Warning: yfinance not installed, skipping macro data")
        return {key: [] for _, key in MACRO_TICKERS}

    result: dict[str, list[dict]] = {}
    for ticker, key in MACRO_TICKERS:
        try:
            data = yf.download(ticker, period=f"{days + 10}d", interval="1d", progress=False)
            entries: list[dict] = []
            if data.empty:
                result[key] = []
                continue
            # yfinance can return MultiIndex columns — flatten
            if hasattr(data.columns, "nlevels") and data.columns.nlevels > 1:
                data.columns = data.columns.get_level_values(0)
            for idx in range(len(data)):
                row_date = str(data.index[idx].date())
                open_val = float(data["Open"].iloc[idx])
                close_val = float(data["Close"].iloc[idx])
                change_pct = float((close_val - open_val) / open_val * 100) if open_val != 0 else 0.0
                entries.append({
                    "date": row_date,
                    "close": close_val,
                    "change_pct": change_pct,
                })
            result[key] = entries
        except Exception as e:
            print(f"  Warning: Failed to fetch {ticker}: {e}")
            result[key] = []
    return result


def fetch_fear_greed(days: int = 180) -> list[dict]:
    """Fetch F&G index history from alternative.me."""
    try:
        resp = requests.get(
            f"https://api.alternative.me/fng/?limit={days}", timeout=10
        )
        return [
            {
                "date": datetime.utcfromtimestamp(int(d["timestamp"])).strftime("%Y-%m-%d"),
                "value": int(d["value"]),
            }
            for d in resp.json()["data"]
        ]
    except Exception as e:
        print(f"  Warning: Failed to fetch F&G: {e}")
        return []


# ---------------------------------------------------------------------------
# SQLite storage
# ---------------------------------------------------------------------------

def init_sqlite(db_path: str = DB_PATH) -> None:
    """Create tables if they don't exist."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS klines (
        symbol TEXT,
        date TEXT,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        timestamp INTEGER,
        PRIMARY KEY (symbol, date)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS macro (
        source TEXT,
        date TEXT,
        close REAL,
        change_pct REAL,
        PRIMARY KEY (source, date)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS fear_greed (
        date TEXT PRIMARY KEY,
        value INTEGER
    )""")
    conn.commit()
    conn.close()


def store_klines_sqlite(db_path: str, symbol: str, candles: list[dict]) -> int:
    """Store klines via INSERT OR REPLACE. Returns row count."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for c in candles:
        cur.execute(
            "INSERT OR REPLACE INTO klines (symbol, date, open, high, low, close, volume, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, c["date"], c["open"], c["high"], c["low"], c["close"], c["volume"], c["timestamp"]),
        )
    conn.commit()
    conn.close()
    return len(candles)


def store_macro_sqlite(db_path: str, source: str, entries: list[dict]) -> int:
    """Store macro data via INSERT OR REPLACE. Returns row count."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for e in entries:
        cur.execute(
            "INSERT OR REPLACE INTO macro (source, date, close, change_pct) VALUES (?, ?, ?, ?)",
            (source, e["date"], e["close"], e["change_pct"]),
        )
    conn.commit()
    conn.close()
    return len(entries)


def store_fear_greed_sqlite(db_path: str, entries: list[dict]) -> int:
    """Store F&G data via INSERT OR REPLACE. Returns row count."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for e in entries:
        cur.execute(
            "INSERT OR REPLACE INTO fear_greed (date, value) VALUES (?, ?)",
            (e["date"], e["value"]),
        )
    conn.commit()
    conn.close()
    return len(entries)


# ---------------------------------------------------------------------------
# Postgres storage (optional — only when DATABASE_URL is set)
# ---------------------------------------------------------------------------

def _pg_connect():
    """Connect to Postgres via DATABASE_URL."""
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


def init_postgres() -> None:
    """Create tables in Postgres if they don't exist."""
    conn = _pg_connect()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS klines (
        symbol TEXT,
        date TEXT,
        open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
        close DOUBLE PRECISION, volume DOUBLE PRECISION,
        timestamp BIGINT,
        PRIMARY KEY (symbol, date)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS macro (
        source TEXT,
        date TEXT,
        close DOUBLE PRECISION,
        change_pct DOUBLE PRECISION,
        PRIMARY KEY (source, date)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS fear_greed (
        date TEXT PRIMARY KEY,
        value INTEGER
    )""")
    conn.commit()
    conn.close()


def store_klines_postgres(symbol: str, candles: list[dict]) -> int:
    conn = _pg_connect()
    cur = conn.cursor()
    for c in candles:
        cur.execute(
            "INSERT INTO klines (symbol, date, open, high, low, close, volume, timestamp) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (symbol, date) DO UPDATE SET "
            "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
            "close=EXCLUDED.close, volume=EXCLUDED.volume, timestamp=EXCLUDED.timestamp",
            (symbol, c["date"], c["open"], c["high"], c["low"], c["close"], c["volume"], c["timestamp"]),
        )
    conn.commit()
    conn.close()
    return len(candles)


def store_macro_postgres(source: str, entries: list[dict]) -> int:
    conn = _pg_connect()
    cur = conn.cursor()
    for e in entries:
        cur.execute(
            "INSERT INTO macro (source, date, close, change_pct) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (source, date) DO UPDATE SET close=EXCLUDED.close, change_pct=EXCLUDED.change_pct",
            (source, e["date"], e["close"], e["change_pct"]),
        )
    conn.commit()
    conn.close()
    return len(entries)


def store_fear_greed_postgres(entries: list[dict]) -> int:
    conn = _pg_connect()
    cur = conn.cursor()
    for e in entries:
        cur.execute(
            "INSERT INTO fear_greed (date, value) VALUES (%s, %s) "
            "ON CONFLICT (date) DO UPDATE SET value=EXCLUDED.value",
            (e["date"], e["value"]),
        )
    conn.commit()
    conn.close()
    return len(entries)


# ---------------------------------------------------------------------------
# Asset config loader
# ---------------------------------------------------------------------------

def load_enabled_assets(filter_assets: Optional[list[str]] = None) -> dict[str, str]:
    """Load enabled assets from assets.yaml. Returns {name: binance_symbol}."""
    assets_path = Path(__file__).resolve().parent.parent / "assets.yaml"
    with open(assets_path) as f:
        cfg = yaml.safe_load(f)

    result: dict[str, str] = {}
    for name, info in cfg.get("assets", {}).items():
        if not info.get("enabled", False):
            continue
        if filter_assets and name not in filter_assets:
            continue
        result[name] = info["binance_symbol"]
    return result


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch historical data for backtesting")
    parser.add_argument("--days", type=int, default=180, help="Number of days to fetch (default: 180)")
    parser.add_argument("--assets", type=str, default=None, help="Comma-separated asset names (default: all enabled)")
    parser.add_argument("--db", type=str, default=DB_PATH, help="SQLite database path")
    args = parser.parse_args()

    filter_assets = [a.strip() for a in args.assets.split(",")] if args.assets else None
    assets = load_enabled_assets(filter_assets)

    if not assets:
        print("No enabled assets found. Check assets.yaml.")
        return

    use_postgres = bool(os.getenv("DATABASE_URL"))
    db_path = args.db

    print(f"Fetching historical data ({args.days} days)...")

    # Init storage
    init_sqlite(db_path)
    if use_postgres:
        init_postgres()

    # 1. Klines
    total_klines = 0
    for i, (name, symbol) in enumerate(assets.items(), 1):
        try:
            candles = fetch_klines(symbol, "1d", args.days)
            n = store_klines_sqlite(db_path, symbol, candles)
            if use_postgres:
                store_klines_postgres(symbol, candles)
            total_klines += n
            print(f"  [{i}/{len(assets)}] {symbol}: {n} candles fetched")
        except Exception as e:
            print(f"  [{i}/{len(assets)}] {symbol}: ERROR — {e}")

    # 2. Macro
    total_macro = 0
    macro_data = fetch_macro(args.days)
    macro_parts = []
    for source, entries in macro_data.items():
        if entries:
            store_macro_sqlite(db_path, source, entries)
            if use_postgres:
                store_macro_postgres(source, entries)
            total_macro += len(entries)
            ticker_name = {"sp500": "SPY", "dxy": "DXY", "nasdaq": "QQQ", "vix": "VIX"}.get(source, source)
            macro_parts.append(f"{ticker_name} ({len(entries)} days)")
    if macro_parts:
        print(f"  Macro data: {', '.join(macro_parts)}")

    # 3. Fear & Greed
    fg_entries = fetch_fear_greed(args.days)
    total_fg = 0
    if fg_entries:
        total_fg = store_fear_greed_sqlite(db_path, fg_entries)
        if use_postgres:
            store_fear_greed_postgres(fg_entries)
    print(f"  Fear & Greed: {total_fg} days")

    # Summary
    targets = [f"backtest_data.db (SQLite)"]
    if use_postgres:
        targets.append("Postgres")
    print(f"  Stored in: {' + '.join(targets)}")
    print(f"  Total: {total_klines} kline rows, {total_macro} macro rows, {total_fg} F&G rows")


if __name__ == "__main__":
    main()
