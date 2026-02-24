from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional


def _get_backend() -> str:
    """Return 'postgres' if DATABASE_URL is set, else 'sqlite'."""
    return "postgres" if os.getenv("DATABASE_URL") else "sqlite"


def _pg_conn():
    """Return a psycopg2 connection using DATABASE_URL."""
    import psycopg2  # only imported when Postgres is used
    return psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=10)


def _classify_user_agent(ua: str) -> str:
    """Classify a user-agent string into a category."""
    ua_lower = ua.lower()
    # AI agents / LLM clients
    if "claude" in ua_lower or "anthropic" in ua_lower:
        return "claude"
    if "openai" in ua_lower or "chatgpt" in ua_lower or "gpt" in ua_lower:
        return "openai"
    if "gemini" in ua_lower or "google" in ua_lower and "bot" in ua_lower:
        return "gemini"
    if "langchain" in ua_lower:
        return "langchain"
    if "crewai" in ua_lower:
        return "crewai"
    if "mcp" in ua_lower:
        return "mcp_client"
    if "autogpt" in ua_lower or "auto-gpt" in ua_lower:
        return "autogpt"
    # Standard clients
    if "python" in ua_lower:
        return "python"
    if "node" in ua_lower or "axios" in ua_lower or "fetch" in ua_lower:
        return "node_js"
    if "curl" in ua_lower:
        return "curl"
    if "postman" in ua_lower:
        return "postman"
    # Browsers
    if "mozilla" in ua_lower or "chrome" in ua_lower or "safari" in ua_lower:
        return "browser"
    # Bots / crawlers
    if "bot" in ua_lower or "crawler" in ua_lower or "spider" in ua_lower:
        return "bot"
    return "other"


class Storage:
    """
    Dual-mode storage: Postgres when DATABASE_URL is set, SQLite otherwise.

    Same public API regardless of backend:
      save(), load_latest(), load_recent(), load_all_latest(),
      save_kv(), load_kv()
    """

    def __init__(self, db_path: str = "signals.db") -> None:
        self.backend = _get_backend()
        self.db_path = db_path  # only used for SQLite

    # ------------------------------------------------------------------ #
    #  Agent snapshot methods
    # ------------------------------------------------------------------ #

    def save(self, agent_name: str, data: Dict[str, Any]) -> None:
        table = self._table_name(agent_name)
        ts = str(data.get("timestamp") or datetime.now(timezone.utc).isoformat())
        payload = json.dumps(data, ensure_ascii=True)

        if self.backend == "postgres":
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"CREATE TABLE IF NOT EXISTS {table} ("
                        f"  id SERIAL PRIMARY KEY,"
                        f"  timestamp TEXT NOT NULL,"
                        f"  data_json TEXT NOT NULL"
                        f")"
                    )
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table} (timestamp)"
                    )
                    cur.execute(
                        f"INSERT INTO {table} (timestamp, data_json) VALUES (%s, %s)",
                        (ts, payload),
                    )
                conn.commit()
        else:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    f"  timestamp TEXT NOT NULL,"
                    f"  data_json TEXT NOT NULL"
                    f")"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table} (timestamp)"
                )
                conn.execute(
                    f"INSERT INTO {table} (timestamp, data_json) VALUES (?, ?)",
                    (ts, payload),
                )
                conn.commit()

    def load_latest(self, agent_name: str) -> Optional[Dict[str, Any]]:
        table = self._table_name(agent_name)
        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT data_json FROM {table} ORDER BY timestamp DESC, id DESC LIMIT 1"
                        )
                        row = cur.fetchone()
                return json.loads(row[0]) if row else None
            except Exception:
                return None
        else:
            if not self._sqlite_table_exists(table):
                return None
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    f"SELECT data_json FROM {table} ORDER BY timestamp DESC, id DESC LIMIT 1"
                ).fetchone()
            return json.loads(row[0]) if row else None

    def load_recent(self, agent_name: str, days: int) -> List[Dict[str, Any]]:
        table = self._table_name(agent_name)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT data_json FROM {table} WHERE timestamp >= %s "
                            f"ORDER BY timestamp DESC, id DESC",
                            (since,),
                        )
                        rows = cur.fetchall()
                return [json.loads(r[0]) for r in rows]
            except Exception:
                return []
        else:
            if not self._sqlite_table_exists(table):
                return []
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    f"SELECT data_json FROM {table} WHERE timestamp >= ? "
                    f"ORDER BY timestamp DESC, id DESC",
                    (since,),
                ).fetchall()
            return [json.loads(r[0]) for r in rows]

    def load_all_latest(self, agent_names: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        return {name: self.load_latest(name) for name in agent_names}

    def load_history(self, agent_name: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Load historical rows with pagination. Returns list of {id, timestamp, data}."""
        table = self._table_name(agent_name)

        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT id, timestamp, data_json FROM {table} "
                            f"ORDER BY timestamp DESC, id DESC LIMIT %s OFFSET %s",
                            (limit, offset),
                        )
                        rows = cur.fetchall()
                return [
                    {"id": r[0], "timestamp": r[1], "data": json.loads(r[2])}
                    for r in rows
                ]
            except Exception:
                return []
        else:
            if not self._sqlite_table_exists(table):
                return []
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    f"SELECT id, timestamp, data_json FROM {table} "
                    f"ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return [
                {"id": r[0], "timestamp": r[1], "data": json.loads(r[2])}
                for r in rows
            ]

    def count_rows(self, agent_name: str) -> int:
        """Count total rows for an agent table."""
        table = self._table_name(agent_name)

        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT COUNT(*) FROM {table}")
                        row = cur.fetchone()
                return row[0] if row else 0
            except Exception:
                return 0
        else:
            if not self._sqlite_table_exists(table):
                return 0
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            return row[0] if row else 0

    # ------------------------------------------------------------------ #
    #  Key-value store (whale flow snapshots, fusion history, etc.)
    # ------------------------------------------------------------------ #

    def save_kv(self, namespace: str, key: str, value: float) -> None:
        """Store a key-value pair with timestamp. Used for balance snapshots, etc."""
        table = f"kv_{re.sub(r'[^a-zA-Z0-9_]', '_', namespace.lower())}"
        now = datetime.now(timezone.utc).isoformat()

        if self.backend == "postgres":
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"CREATE TABLE IF NOT EXISTS {table} ("
                        f"  id SERIAL PRIMARY KEY,"
                        f"  key TEXT NOT NULL,"
                        f"  value DOUBLE PRECISION NOT NULL,"
                        f"  timestamp TEXT NOT NULL"
                        f")"
                    )
                    cur.execute(
                        f"INSERT INTO {table} (key, value, timestamp) VALUES (%s, %s, %s)",
                        (key, value, now),
                    )
                conn.commit()
        else:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    f"  key TEXT NOT NULL,"
                    f"  value REAL NOT NULL,"
                    f"  timestamp TEXT NOT NULL"
                    f")"
                )
                conn.execute(
                    f"INSERT INTO {table} (key, value, timestamp) VALUES (?, ?, ?)",
                    (key, value, now),
                )
                conn.commit()

    def load_kv(self, namespace: str, key: str) -> Optional[float]:
        """Load latest value for a key in a namespace."""
        table = f"kv_{re.sub(r'[^a-zA-Z0-9_]', '_', namespace.lower())}"

        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT value FROM {table} WHERE key = %s "
                            f"ORDER BY id DESC LIMIT 1",
                            (key,),
                        )
                        row = cur.fetchone()
                return float(row[0]) if row else None
            except Exception:
                return None
        else:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    row = conn.execute(
                        f"SELECT value FROM {table} WHERE key = ? "
                        f"ORDER BY id DESC LIMIT 1",
                        (key,),
                    ).fetchone()
                return float(row[0]) if row else None
            except Exception:
                return None

    # ------------------------------------------------------------------ #
    #  Key-value JSON store (LLM sentiment cache, etc.)
    # ------------------------------------------------------------------ #

    def save_kv_json(self, namespace: str, key: str, value: Dict) -> None:
        """Store a JSON-serializable dict as a key-value pair."""
        table = f"kvj_{re.sub(r'[^a-zA-Z0-9_]', '_', namespace.lower())}"
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(value, ensure_ascii=True)

        if self.backend == "postgres":
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"CREATE TABLE IF NOT EXISTS {table} ("
                        f"  id SERIAL PRIMARY KEY,"
                        f"  key TEXT NOT NULL,"
                        f"  value_json TEXT NOT NULL,"
                        f"  timestamp TEXT NOT NULL"
                        f")"
                    )
                    cur.execute(
                        f"INSERT INTO {table} (key, value_json, timestamp) VALUES (%s, %s, %s)",
                        (key, payload, now),
                    )
                conn.commit()
        else:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    f"  key TEXT NOT NULL,"
                    f"  value_json TEXT NOT NULL,"
                    f"  timestamp TEXT NOT NULL"
                    f")"
                )
                conn.execute(
                    f"INSERT INTO {table} (key, value_json, timestamp) VALUES (?, ?, ?)",
                    (key, payload, now),
                )
                conn.commit()

    def load_kv_json(self, namespace: str, key: str) -> Optional[Dict]:
        """Load latest JSON value for a key in a namespace."""
        table = f"kvj_{re.sub(r'[^a-zA-Z0-9_]', '_', namespace.lower())}"

        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT value_json FROM {table} WHERE key = %s "
                            f"ORDER BY id DESC LIMIT 1",
                            (key,),
                        )
                        row = cur.fetchone()
                return json.loads(row[0]) if row else None
            except Exception:
                return None
        else:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    row = conn.execute(
                        f"SELECT value_json FROM {table} WHERE key = ? "
                        f"ORDER BY id DESC LIMIT 1",
                        (key,),
                    ).fetchone()
                return json.loads(row[0]) if row else None
            except Exception:
                return None

    # ------------------------------------------------------------------ #
    #  Performance tracking tables
    # ------------------------------------------------------------------ #

    def save_performance_snapshot(self, asset: str, signal_score: float,
                                  signal_direction: str, price_at_signal: float,
                                  sources_count: int, detail: str) -> Optional[int]:
        """Save a performance snapshot. Returns the row id."""
        table = "performance_snapshots"
        now = datetime.now(timezone.utc).isoformat()

        if self.backend == "postgres":
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"CREATE TABLE IF NOT EXISTS {table} ("
                        f"  id SERIAL PRIMARY KEY,"
                        f"  timestamp TEXT NOT NULL,"
                        f"  asset TEXT NOT NULL,"
                        f"  signal_score DOUBLE PRECISION NOT NULL,"
                        f"  signal_direction TEXT NOT NULL,"
                        f"  price_at_signal DOUBLE PRECISION NOT NULL,"
                        f"  sources_count INTEGER NOT NULL,"
                        f"  detail TEXT,"
                        f"  evaluated_24h BOOLEAN DEFAULT FALSE,"
                        f"  evaluated_48h BOOLEAN DEFAULT FALSE,"
                        f"  evaluated_7d BOOLEAN DEFAULT FALSE"
                        f")"
                    )
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table} (timestamp)"
                    )
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_asset ON {table} (asset)"
                    )
                    cur.execute(
                        f"INSERT INTO {table} (timestamp, asset, signal_score, signal_direction, "
                        f"price_at_signal, sources_count, detail) "
                        f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                        (now, asset, signal_score, signal_direction, price_at_signal,
                         sources_count, detail),
                    )
                    row = cur.fetchone()
                conn.commit()
                return row[0] if row else None
        else:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    f"  timestamp TEXT NOT NULL,"
                    f"  asset TEXT NOT NULL,"
                    f"  signal_score REAL NOT NULL,"
                    f"  signal_direction TEXT NOT NULL,"
                    f"  price_at_signal REAL NOT NULL,"
                    f"  sources_count INTEGER NOT NULL,"
                    f"  detail TEXT,"
                    f"  evaluated_24h INTEGER DEFAULT 0,"
                    f"  evaluated_48h INTEGER DEFAULT 0,"
                    f"  evaluated_7d INTEGER DEFAULT 0"
                    f")"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table} (timestamp)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_asset ON {table} (asset)"
                )
                cur = conn.execute(
                    f"INSERT INTO {table} (timestamp, asset, signal_score, signal_direction, "
                    f"price_at_signal, sources_count, detail) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (now, asset, signal_score, signal_direction, price_at_signal,
                     sources_count, detail),
                )
                conn.commit()
                return cur.lastrowid

    def save_performance_accuracy(self, snapshot_id: int, window_hours: int,
                                   price_at_window: float, direction_correct: bool) -> None:
        """Save an accuracy evaluation for a snapshot."""
        table = "performance_accuracy"
        now = datetime.now(timezone.utc).isoformat()

        if self.backend == "postgres":
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"CREATE TABLE IF NOT EXISTS {table} ("
                        f"  id SERIAL PRIMARY KEY,"
                        f"  snapshot_id INTEGER NOT NULL,"
                        f"  window_hours INTEGER NOT NULL,"
                        f"  price_at_window DOUBLE PRECISION NOT NULL,"
                        f"  direction_correct BOOLEAN NOT NULL,"
                        f"  evaluated_at TEXT NOT NULL"
                        f")"
                    )
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_snap ON {table} (snapshot_id)"
                    )
                    cur.execute(
                        f"INSERT INTO {table} (snapshot_id, window_hours, price_at_window, "
                        f"direction_correct, evaluated_at) VALUES (%s, %s, %s, %s, %s)",
                        (snapshot_id, window_hours, price_at_window,
                         direction_correct, now),
                    )
                    # Mark snapshot as evaluated for this window
                    snap_table = "performance_snapshots"
                    col = f"evaluated_{window_hours}h" if window_hours != 168 else "evaluated_7d"
                    cur.execute(
                        f"UPDATE {snap_table} SET {col} = TRUE WHERE id = %s",
                        (snapshot_id,),
                    )
                conn.commit()
        else:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    f"  snapshot_id INTEGER NOT NULL,"
                    f"  window_hours INTEGER NOT NULL,"
                    f"  price_at_window REAL NOT NULL,"
                    f"  direction_correct INTEGER NOT NULL,"
                    f"  evaluated_at TEXT NOT NULL"
                    f")"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_snap ON {table} (snapshot_id)"
                )
                conn.execute(
                    f"INSERT INTO {table} (snapshot_id, window_hours, price_at_window, "
                    f"direction_correct, evaluated_at) VALUES (?, ?, ?, ?, ?)",
                    (snapshot_id, window_hours, price_at_window,
                     int(direction_correct), now),
                )
                snap_table = "performance_snapshots"
                col = f"evaluated_{window_hours}h" if window_hours != 168 else "evaluated_7d"
                conn.execute(
                    f"UPDATE {snap_table} SET {col} = 1 WHERE id = ?",
                    (snapshot_id,),
                )
                conn.commit()

    def load_unevaluated_snapshots(self, window_hours: int, min_age_hours: int) -> List[Dict[str, Any]]:
        """Load snapshots that are old enough but not yet evaluated for a given window."""
        table = "performance_snapshots"
        col = f"evaluated_{window_hours}h" if window_hours != 168 else "evaluated_7d"
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=min_age_hours)).isoformat()

        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT id, timestamp, asset, signal_score, signal_direction, "
                            f"price_at_signal FROM {table} "
                            f"WHERE {col} = FALSE AND timestamp <= %s "
                            f"ORDER BY timestamp ASC LIMIT 100",
                            (cutoff,),
                        )
                        rows = cur.fetchall()
                return [
                    {"id": r[0], "timestamp": r[1], "asset": r[2],
                     "signal_score": r[3], "signal_direction": r[4],
                     "price_at_signal": r[5]}
                    for r in rows
                ]
            except Exception:
                return []
        else:
            try:
                false_val = 0
                with sqlite3.connect(self.db_path) as conn:
                    rows = conn.execute(
                        f"SELECT id, timestamp, asset, signal_score, signal_direction, "
                        f"price_at_signal FROM {table} "
                        f"WHERE {col} = ? AND timestamp <= ? "
                        f"ORDER BY timestamp ASC LIMIT 100",
                        (false_val, cutoff),
                    ).fetchall()
                return [
                    {"id": r[0], "timestamp": r[1], "asset": r[2],
                     "signal_score": r[3], "signal_direction": r[4],
                     "price_at_signal": r[5]}
                    for r in rows
                ]
            except Exception:
                return []

    def load_accuracy_stats(self, days: int = 30) -> Dict[str, Any]:
        """Load aggregated accuracy stats for the reputation endpoint."""
        acc_table = "performance_accuracy"
        snap_table = "performance_snapshots"
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        result = {
            "total": 0, "hits": 0,
            "by_timeframe": {},
            "by_asset": {},
        }

        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        # Overall stats
                        cur.execute(
                            f"SELECT COUNT(*), SUM(CASE WHEN a.direction_correct THEN 1 ELSE 0 END) "
                            f"FROM {acc_table} a JOIN {snap_table} s ON a.snapshot_id = s.id "
                            f"WHERE s.timestamp >= %s",
                            (since,),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            result["total"] = row[0]
                            result["hits"] = int(row[1] or 0)

                        # By timeframe
                        cur.execute(
                            f"SELECT a.window_hours, COUNT(*), "
                            f"SUM(CASE WHEN a.direction_correct THEN 1 ELSE 0 END) "
                            f"FROM {acc_table} a JOIN {snap_table} s ON a.snapshot_id = s.id "
                            f"WHERE s.timestamp >= %s GROUP BY a.window_hours",
                            (since,),
                        )
                        for row in cur.fetchall():
                            wh = row[0]
                            label = "7d" if wh == 168 else f"{wh}h"
                            total = row[1]
                            hits = int(row[2] or 0)
                            result["by_timeframe"][label] = {
                                "accuracy": round(hits / total * 100, 1) if total > 0 else 0,
                                "hits": hits, "total": total,
                            }

                        # By asset
                        cur.execute(
                            f"SELECT s.asset, COUNT(*), "
                            f"SUM(CASE WHEN a.direction_correct THEN 1 ELSE 0 END) "
                            f"FROM {acc_table} a JOIN {snap_table} s ON a.snapshot_id = s.id "
                            f"WHERE s.timestamp >= %s GROUP BY s.asset",
                            (since,),
                        )
                        for row in cur.fetchall():
                            asset = row[0]
                            total = row[1]
                            hits = int(row[2] or 0)
                            result["by_asset"][asset] = round(hits / total * 100, 1) if total > 0 else 0

            except Exception:
                pass
        else:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    row = conn.execute(
                        f"SELECT COUNT(*), SUM(direction_correct) "
                        f"FROM {acc_table} a JOIN {snap_table} s ON a.snapshot_id = s.id "
                        f"WHERE s.timestamp >= ?",
                        (since,),
                    ).fetchone()
                    if row and row[0]:
                        result["total"] = row[0]
                        result["hits"] = int(row[1] or 0)

                    rows = conn.execute(
                        f"SELECT a.window_hours, COUNT(*), SUM(a.direction_correct) "
                        f"FROM {acc_table} a JOIN {snap_table} s ON a.snapshot_id = s.id "
                        f"WHERE s.timestamp >= ? GROUP BY a.window_hours",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        wh = row[0]
                        label = "7d" if wh == 168 else f"{wh}h"
                        total = row[1]
                        hits = int(row[2] or 0)
                        result["by_timeframe"][label] = {
                            "accuracy": round(hits / total * 100, 1) if total > 0 else 0,
                            "hits": hits, "total": total,
                        }

                    rows = conn.execute(
                        f"SELECT s.asset, COUNT(*), SUM(a.direction_correct) "
                        f"FROM {acc_table} a JOIN {snap_table} s ON a.snapshot_id = s.id "
                        f"WHERE s.timestamp >= ? GROUP BY s.asset",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        asset = row[0]
                        total = row[1]
                        hits = int(row[2] or 0)
                        result["by_asset"][asset] = round(hits / total * 100, 1) if total > 0 else 0
            except Exception:
                pass

        return result

    def count_snapshots(self, days: int = 30) -> int:
        """Count total snapshots in the last N days."""
        table = "performance_snapshots"
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE timestamp >= %s",
                            (since,),
                        )
                        row = cur.fetchone()
                return row[0] if row else 0
            except Exception:
                return 0
        else:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    row = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE timestamp >= ?",
                        (since,),
                    ).fetchone()
                return row[0] if row else 0
            except Exception:
                return 0

    # ------------------------------------------------------------------ #
    #  API usage tracking
    # ------------------------------------------------------------------ #

    def save_api_request(self, endpoint: str, method: str, user_agent: str,
                          status_code: int, duration_ms: float,
                          client_ip: str = "") -> None:
        """Log an API request for analytics."""
        table = "api_requests"
        now = datetime.now(timezone.utc).isoformat()

        if self.backend == "postgres":
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"CREATE TABLE IF NOT EXISTS {table} ("
                        f"  id SERIAL PRIMARY KEY,"
                        f"  timestamp TEXT NOT NULL,"
                        f"  endpoint TEXT NOT NULL,"
                        f"  method TEXT NOT NULL,"
                        f"  user_agent TEXT,"
                        f"  status_code INTEGER NOT NULL,"
                        f"  duration_ms DOUBLE PRECISION,"
                        f"  client_ip TEXT"
                        f")"
                    )
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table} (timestamp)"
                    )
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_ep ON {table} (endpoint)"
                    )
                    cur.execute(
                        f"INSERT INTO {table} (timestamp, endpoint, method, user_agent, "
                        f"status_code, duration_ms, client_ip) "
                        f"VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (now, endpoint, method, user_agent, status_code, duration_ms, client_ip),
                    )
                conn.commit()
        else:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    f"  timestamp TEXT NOT NULL,"
                    f"  endpoint TEXT NOT NULL,"
                    f"  method TEXT NOT NULL,"
                    f"  user_agent TEXT,"
                    f"  status_code INTEGER NOT NULL,"
                    f"  duration_ms REAL,"
                    f"  client_ip TEXT"
                    f")"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table} (timestamp)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_ep ON {table} (endpoint)"
                )
                conn.execute(
                    f"INSERT INTO {table} (timestamp, endpoint, method, user_agent, "
                    f"status_code, duration_ms, client_ip) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (now, endpoint, method, user_agent, status_code, duration_ms, client_ip),
                )
                conn.commit()

    def load_api_analytics(self, days: int = 7) -> Dict[str, Any]:
        """Load aggregated API usage analytics."""
        table = "api_requests"
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        result: Dict[str, Any] = {
            "total_requests": 0,
            "by_endpoint": {},
            "by_user_agent_type": {},
            "unique_ips": 0,
            "requests_per_day": {},
            "avg_duration_ms": 0,
            "top_user_agents": [],
        }

        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        # Total requests
                        cur.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE timestamp >= %s",
                            (since,),
                        )
                        row = cur.fetchone()
                        result["total_requests"] = row[0] if row else 0

                        # By endpoint
                        cur.execute(
                            f"SELECT endpoint, COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s GROUP BY endpoint ORDER BY COUNT(*) DESC",
                            (since,),
                        )
                        for row in cur.fetchall():
                            result["by_endpoint"][row[0]] = row[1]

                        # By user-agent type (classify)
                        cur.execute(
                            f"SELECT user_agent, COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s GROUP BY user_agent ORDER BY COUNT(*) DESC LIMIT 50",
                            (since,),
                        )
                        ua_counts: Dict[str, int] = {}
                        raw_agents: list = []
                        for row in cur.fetchall():
                            ua = row[0] or "unknown"
                            count = row[1]
                            ua_type = _classify_user_agent(ua)
                            ua_counts[ua_type] = ua_counts.get(ua_type, 0) + count
                            raw_agents.append({"user_agent": ua, "requests": count, "type": ua_type})
                        result["by_user_agent_type"] = ua_counts
                        result["top_user_agents"] = raw_agents[:20]

                        # Unique IPs
                        cur.execute(
                            f"SELECT COUNT(DISTINCT client_ip) FROM {table} "
                            f"WHERE timestamp >= %s AND client_ip != ''",
                            (since,),
                        )
                        row = cur.fetchone()
                        result["unique_ips"] = row[0] if row else 0

                        # Requests per day
                        cur.execute(
                            f"SELECT LEFT(timestamp, 10) as day, COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s GROUP BY LEFT(timestamp, 10) ORDER BY day",
                            (since,),
                        )
                        for row in cur.fetchall():
                            result["requests_per_day"][row[0]] = row[1]

                        # Average duration
                        cur.execute(
                            f"SELECT AVG(duration_ms) FROM {table} "
                            f"WHERE timestamp >= %s AND duration_ms > 0",
                            (since,),
                        )
                        row = cur.fetchone()
                        result["avg_duration_ms"] = round(float(row[0]), 1) if row and row[0] else 0

            except Exception:
                pass
        else:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    row = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE timestamp >= ?",
                        (since,),
                    ).fetchone()
                    result["total_requests"] = row[0] if row else 0

                    rows = conn.execute(
                        f"SELECT endpoint, COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? GROUP BY endpoint ORDER BY COUNT(*) DESC",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        result["by_endpoint"][row[0]] = row[1]

                    rows = conn.execute(
                        f"SELECT user_agent, COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? GROUP BY user_agent ORDER BY COUNT(*) DESC LIMIT 50",
                        (since,),
                    ).fetchall()
                    ua_counts = {}
                    raw_agents = []
                    for row in rows:
                        ua = row[0] or "unknown"
                        count = row[1]
                        ua_type = _classify_user_agent(ua)
                        ua_counts[ua_type] = ua_counts.get(ua_type, 0) + count
                        raw_agents.append({"user_agent": ua, "requests": count, "type": ua_type})
                    result["by_user_agent_type"] = ua_counts
                    result["top_user_agents"] = raw_agents[:20]

                    row = conn.execute(
                        f"SELECT COUNT(DISTINCT client_ip) FROM {table} "
                        f"WHERE timestamp >= ? AND client_ip != ''",
                        (since,),
                    ).fetchone()
                    result["unique_ips"] = row[0] if row else 0

                    rows = conn.execute(
                        f"SELECT SUBSTR(timestamp, 1, 10) as day, COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? GROUP BY SUBSTR(timestamp, 1, 10) ORDER BY day",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        result["requests_per_day"][row[0]] = row[1]

                    row = conn.execute(
                        f"SELECT AVG(duration_ms) FROM {table} "
                        f"WHERE timestamp >= ? AND duration_ms > 0",
                        (since,),
                    ).fetchone()
                    result["avg_duration_ms"] = round(float(row[0]), 1) if row and row[0] else 0
            except Exception:
                pass

        return result

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    def _table_name(self, agent_name: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", agent_name.strip().lower())
        if not safe:
            raise ValueError("agent_name must contain at least one alphanumeric character")
        return f"agent_{safe}"

    def _sqlite_table_exists(self, table: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        return row is not None


# Backward-compatible alias
SQLiteStorage = Storage
