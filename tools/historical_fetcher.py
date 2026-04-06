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


def fetch_derivatives_history(symbol: str, days: int = 180) -> list[dict]:
    """Fetch historical derivatives data from Binance Futures.

    Combines funding rate, L/S ratio, taker ratio, and OI into daily records.
    All endpoints are free, no API key needed.
    """
    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
    base = "https://fapi.binance.com"
    result_by_date: dict[str, dict] = {}

    # 1. Funding rate history (8h intervals → aggregate to daily average)
    try:
        all_funding: list[dict] = []
        fetch_start = start_ms
        while fetch_start < end_ms:
            resp = requests.get(
                f"{base}/fapi/v1/fundingRate",
                params={"symbol": symbol, "startTime": fetch_start, "limit": 1000},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            all_funding.extend(data)
            fetch_start = data[-1]["fundingTime"] + 1
            time.sleep(0.1)

        for entry in all_funding:
            date = datetime.utcfromtimestamp(entry["fundingTime"] / 1000).strftime("%Y-%m-%d")
            if date not in result_by_date:
                result_by_date[date] = {"funding_rates": [], "symbol": symbol}
            result_by_date[date]["funding_rates"].append(float(entry["fundingRate"]))
    except Exception as e:
        print(f"    Warning: Funding rate fetch failed for {symbol}: {e}")

    # 2. L/S ratio (daily)
    try:
        resp = requests.get(
            f"{base}/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": "1d", "limit": min(days, 500)},
            timeout=15,
        )
        resp.raise_for_status()
        for entry in resp.json():
            date = datetime.utcfromtimestamp(entry["timestamp"] / 1000).strftime("%Y-%m-%d")
            if date not in result_by_date:
                result_by_date[date] = {"funding_rates": [], "symbol": symbol}
            result_by_date[date]["long_short_ratio"] = float(entry["longShortRatio"])
        time.sleep(0.1)
    except Exception as e:
        print(f"    Warning: L/S ratio fetch failed for {symbol}: {e}")

    # 3. Taker buy/sell ratio (daily)
    try:
        resp = requests.get(
            f"{base}/futures/data/takerlongshortRatio",
            params={"symbol": symbol, "period": "1d", "limit": min(days, 500)},
            timeout=15,
        )
        resp.raise_for_status()
        for entry in resp.json():
            date = datetime.utcfromtimestamp(entry["timestamp"] / 1000).strftime("%Y-%m-%d")
            if date not in result_by_date:
                result_by_date[date] = {"funding_rates": [], "symbol": symbol}
            result_by_date[date]["taker_buy_sell_ratio"] = float(entry["buySellRatio"])
        time.sleep(0.1)
    except Exception as e:
        print(f"    Warning: Taker ratio fetch failed for {symbol}: {e}")

    # 4. OI history (daily)
    try:
        resp = requests.get(
            f"{base}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": "1d", "limit": min(days, 500)},
            timeout=15,
        )
        resp.raise_for_status()
        for entry in resp.json():
            date = datetime.utcfromtimestamp(entry["timestamp"] / 1000).strftime("%Y-%m-%d")
            if date not in result_by_date:
                result_by_date[date] = {"funding_rates": [], "symbol": symbol}
            result_by_date[date]["open_interest"] = float(entry["sumOpenInterest"])
        time.sleep(0.1)
    except Exception as e:
        print(f"    Warning: OI fetch failed for {symbol}: {e}")

    # Flatten to list with daily averages
    records = []
    sorted_dates = sorted(result_by_date.keys())
    prev_oi = None
    for date in sorted_dates:
        d = result_by_date[date]
        funding_rates = d.get("funding_rates", [])
        avg_funding = sum(funding_rates) / len(funding_rates) if funding_rates else 0.0

        oi = d.get("open_interest", 0.0)
        oi_change_pct = 0.0
        if prev_oi and prev_oi > 0 and oi > 0:
            oi_change_pct = (oi - prev_oi) / prev_oi * 100
        prev_oi = oi if oi > 0 else prev_oi

        records.append({
            "symbol": symbol,
            "date": date,
            "funding_rate": avg_funding,
            "long_short_ratio": d.get("long_short_ratio", 0.0),
            "taker_buy_sell_ratio": d.get("taker_buy_sell_ratio", 0.0),
            "open_interest": oi,
            "oi_change_pct": oi_change_pct,
        })

    return records


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


def fetch_liquidations(symbol: str, hours: int = 24) -> list[dict]:
    """Fetch recent force liquidation orders from Binance Futures.
    Endpoint: /fapi/v1/forceOrders (no API key needed).
    """
    try:
        resp = requests.get(
            "https://fapi.binance.com/fapi/v1/forceOrders",
            params={"symbol": symbol, "limit": 100},
            timeout=10,
        )
        resp.raise_for_status()
        return [
            {
                "price": float(order["price"]),
                "qty": float(order["origQty"]),
                "side": order["side"],
                "time": order["time"],
            }
            for order in resp.json()
        ]
    except Exception as e:
        print(f"  Warning: Liquidation fetch failed for {symbol}: {e}")
        return []


def calc_liq_density(liquidations: list[dict], current_price: float, range_pct: float = 2.0) -> float:
    """Count liquidation volume near current price.
    High density near price = stop-hunt magnet zone.
    """
    if not liquidations or current_price <= 0:
        return 0.0
    range_abs = current_price * range_pct / 100
    nearby_vol = sum(
        l["qty"] for l in liquidations
        if abs(l["price"] - current_price) <= range_abs
    )
    total_vol = sum(l["qty"] for l in liquidations)
    return nearby_vol / total_vol if total_vol > 0 else 0.0


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
    cur.execute("""CREATE TABLE IF NOT EXISTS derivatives (
        symbol TEXT,
        date TEXT,
        funding_rate REAL,
        long_short_ratio REAL,
        taker_buy_sell_ratio REAL,
        open_interest REAL,
        oi_change_pct REAL,
        PRIMARY KEY (symbol, date)
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


def store_derivatives_sqlite(db_path: str, records: list[dict]) -> int:
    """Store derivatives data via INSERT OR REPLACE. Returns row count."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for r in records:
        cur.execute(
            "INSERT OR REPLACE INTO derivatives "
            "(symbol, date, funding_rate, long_short_ratio, taker_buy_sell_ratio, "
            "open_interest, oi_change_pct) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r["symbol"], r["date"], r["funding_rate"], r["long_short_ratio"],
             r["taker_buy_sell_ratio"], r["open_interest"], r["oi_change_pct"]),
        )
    conn.commit()
    conn.close()
    return len(records)


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

    # 2. Derivatives (from Binance Futures)
    total_deriv = 0
    for i, (name, symbol) in enumerate(assets.items(), 1):
        try:
            records = fetch_derivatives_history(symbol, args.days)
            if records:
                n = store_derivatives_sqlite(db_path, records)
                total_deriv += n
                print(f"  [{i}/{len(assets)}] {symbol}: {n} derivatives days fetched")
            else:
                print(f"  [{i}/{len(assets)}] {symbol}: No derivatives data")
        except Exception as e:
            print(f"  [{i}/{len(assets)}] {symbol}: Derivatives ERROR — {e}")
        time.sleep(0.2)  # Rate limit

    # 3. Macro
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

    # 4. Fear & Greed
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
    print(f"  Total: {total_klines} kline rows, {total_deriv} derivatives rows, "
          f"{total_macro} macro rows, {total_fg} F&G rows")


if __name__ == "__main__":
    main()
