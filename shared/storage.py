from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional
import logging
import traceback

logger = logging.getLogger("web3signals.storage")


# ------------------------------------------------------------------ #
#  Ranking / correlation helpers for IC computation (no scipy needed)
# ------------------------------------------------------------------ #

def _rank_array(values: list) -> list:
    """Assign average ranks to values (handles ties). Equivalent to scipy.stats.rankdata."""
    n = len(values)
    indexed = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n - 1 and values[indexed[j + 1]] == values[indexed[j]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def _pearson(x: list, y: list) -> Optional[float]:
    """Pearson correlation between two lists. Returns None if degenerate."""
    n = len(x)
    if n < 3:
        return None
    mx = sum(x) / n
    my = sum(y) / n
    sx = sum((xi - mx) ** 2 for xi in x)
    sy = sum((yi - my) ** 2 for yi in y)
    if sx < 1e-12 or sy < 1e-12:
        return None
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    return sxy / (sx * sy) ** 0.5


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
            except Exception as exc:
                logger.warning("load_latest(%s) failed: %s", agent_name, exc)
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
            except Exception as exc:
                logger.warning("load_kv(%s, %s) pg failed: %s", namespace, key, exc)
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
            except Exception as exc:
                logger.warning("load_kv(%s, %s) sqlite failed: %s", namespace, key, exc)
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
            except Exception as exc:
                logger.warning("load_kv_json(%s, %s) pg failed: %s", namespace, key, exc)
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
            except Exception as exc:
                logger.warning("load_kv_json(%s, %s) sqlite failed: %s", namespace, key, exc)
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
                                   price_at_window: float,
                                   gradient_score: Optional[float],
                                   pct_change: Optional[float] = None) -> None:
        """Save a gradient accuracy evaluation for a snapshot.

        gradient_score can be:
          0.0-1.0 — directional signal scored (1.0 = perfect, 0.0 = wrong)
          None — neutral signal, skipped (not counted in accuracy)
        """
        if gradient_score is not None:
            gradient_score = max(0.0, min(1.0, float(gradient_score)))
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
                        f"  gradient_score DOUBLE PRECISION,"
                        f"  pct_change DOUBLE PRECISION,"
                        f"  evaluated_at TEXT NOT NULL"
                        f")"
                    )
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_snap ON {table} (snapshot_id)"
                    )
                    cur.execute(
                        f"INSERT INTO {table} (snapshot_id, window_hours, price_at_window, "
                        f"gradient_score, pct_change, evaluated_at) VALUES (%s, %s, %s, %s, %s, %s)",
                        (snapshot_id, window_hours, price_at_window,
                         gradient_score, pct_change, now),
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
                    f"  gradient_score REAL,"
                    f"  pct_change REAL,"
                    f"  evaluated_at TEXT NOT NULL"
                    f")"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_snap ON {table} (snapshot_id)"
                )
                conn.execute(
                    f"INSERT INTO {table} (snapshot_id, window_hours, price_at_window, "
                    f"gradient_score, pct_change, evaluated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (snapshot_id, window_hours, price_at_window,
                     gradient_score, pct_change, now),
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
            except Exception as exc:
                logger.warning("load_unevaluated_snapshots pg failed: %s", exc)
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
            except Exception as exc:
                logger.warning("load_unevaluated_snapshots sqlite failed: %s", exc)
                return []

    def load_accuracy_stats(self, days: int = 30) -> Dict[str, Any]:
        """Load aggregated gradient accuracy stats for the reputation endpoint.

        Uses gradient scoring (0.0-1.0) instead of binary hit/miss.
        Accuracy = AVG(gradient_score) × 100.
        Neutral signals (gradient_score IS NULL) tracked separately.
        """
        acc_table = "performance_accuracy"
        snap_table = "performance_snapshots"
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Filter: only rows where gradient_score IS NOT NULL (directional signals)
        directional_filter = "AND a.gradient_score IS NOT NULL"

        result: Dict[str, Any] = {
            "total": 0, "avg_gradient": 0.0, "neutral_skipped": 0,
            "by_timeframe": {},
            "by_asset": {},
        }

        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        # Count neutrals skipped
                        cur.execute(
                            f"SELECT COUNT(*) FROM {acc_table} a "
                            f"JOIN {snap_table} s ON a.snapshot_id = s.id "
                            f"WHERE s.timestamp >= %s AND a.gradient_score IS NULL",
                            (since,),
                        )
                        row = cur.fetchone()
                        result["neutral_skipped"] = row[0] if row else 0

                        # Overall gradient accuracy (directional only)
                        cur.execute(
                            f"SELECT COUNT(*), AVG(a.gradient_score), "
                            f"AVG(ABS(a.pct_change)) "
                            f"FROM {acc_table} a JOIN {snap_table} s ON a.snapshot_id = s.id "
                            f"WHERE s.timestamp >= %s {directional_filter}",
                            (since,),
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            result["total"] = row[0]
                            result["avg_gradient"] = round(float(row[1] or 0), 3)
                            result["avg_abs_pct_change"] = round(float(row[2] or 0), 2)

                        # By timeframe
                        cur.execute(
                            f"SELECT a.window_hours, COUNT(*), AVG(a.gradient_score), "
                            f"AVG(ABS(a.pct_change)) "
                            f"FROM {acc_table} a JOIN {snap_table} s ON a.snapshot_id = s.id "
                            f"WHERE s.timestamp >= %s {directional_filter} "
                            f"GROUP BY a.window_hours",
                            (since,),
                        )
                        for row in cur.fetchall():
                            wh = row[0]
                            label = "7d" if wh == 168 else f"{wh}h"
                            total = row[1]
                            avg_grad = round(float(row[2] or 0), 3)
                            avg_pct = round(float(row[3] or 0), 2)
                            result["by_timeframe"][label] = {
                                "accuracy": round(avg_grad * 100, 1),
                                "avg_gradient": avg_grad,
                                "avg_abs_pct_change": avg_pct,
                                "total": total,
                            }

                        # By asset
                        cur.execute(
                            f"SELECT s.asset, COUNT(*), AVG(a.gradient_score), "
                            f"AVG(ABS(a.pct_change)) "
                            f"FROM {acc_table} a JOIN {snap_table} s ON a.snapshot_id = s.id "
                            f"WHERE s.timestamp >= %s {directional_filter} "
                            f"GROUP BY s.asset",
                            (since,),
                        )
                        for row in cur.fetchall():
                            asset = row[0]
                            avg_grad = round(float(row[2] or 0), 3)
                            result["by_asset"][asset] = round(avg_grad * 100, 1)

            except Exception:
                pass
        else:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    # Count neutrals skipped
                    row = conn.execute(
                        f"SELECT COUNT(*) FROM {acc_table} a "
                        f"JOIN {snap_table} s ON a.snapshot_id = s.id "
                        f"WHERE s.timestamp >= ? AND a.gradient_score IS NULL",
                        (since,),
                    ).fetchone()
                    result["neutral_skipped"] = row[0] if row else 0

                    # Overall gradient accuracy
                    row = conn.execute(
                        f"SELECT COUNT(*), AVG(a.gradient_score), "
                        f"AVG(ABS(a.pct_change)) "
                        f"FROM {acc_table} a JOIN {snap_table} s ON a.snapshot_id = s.id "
                        f"WHERE s.timestamp >= ? {directional_filter}",
                        (since,),
                    ).fetchone()
                    if row and row[0]:
                        result["total"] = row[0]
                        result["avg_gradient"] = round(float(row[1] or 0), 3)
                        result["avg_abs_pct_change"] = round(float(row[2] or 0), 2)

                    # By timeframe
                    rows = conn.execute(
                        f"SELECT a.window_hours, COUNT(*), AVG(a.gradient_score), "
                        f"AVG(ABS(a.pct_change)) "
                        f"FROM {acc_table} a JOIN {snap_table} s ON a.snapshot_id = s.id "
                        f"WHERE s.timestamp >= ? {directional_filter} "
                        f"GROUP BY a.window_hours",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        wh = row[0]
                        label = "7d" if wh == 168 else f"{wh}h"
                        total = row[1]
                        avg_grad = round(float(row[2] or 0), 3)
                        avg_pct = round(float(row[3] or 0), 2)
                        result["by_timeframe"][label] = {
                            "accuracy": round(avg_grad * 100, 1),
                            "avg_gradient": avg_grad,
                            "avg_abs_pct_change": avg_pct,
                            "total": total,
                        }

                    # By asset
                    rows = conn.execute(
                        f"SELECT s.asset, COUNT(*), AVG(a.gradient_score), "
                        f"AVG(ABS(a.pct_change)) "
                        f"FROM {acc_table} a JOIN {snap_table} s ON a.snapshot_id = s.id "
                        f"WHERE s.timestamp >= ? {directional_filter} "
                        f"GROUP BY s.asset",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        asset = row[0]
                        avg_grad = round(float(row[2] or 0), 3)
                        result["by_asset"][asset] = round(avg_grad * 100, 1)
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
    #  IC (Information Coefficient) tracking
    # ------------------------------------------------------------------ #

    def save_dimension_scores(self, snapshot_id: int, dimension_scores: Dict[str, float],
                               config_version: str = "", regime: str = "") -> None:
        """Store per-dimension numeric scores alongside a performance snapshot.

        dimension_scores: e.g. {"whale": 42.5, "technical": 65.0, ...}
        """
        table = "ic_dimension_scores"
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(dimension_scores, ensure_ascii=True)

        if self.backend == "postgres":
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"CREATE TABLE IF NOT EXISTS {table} ("
                        f"  id SERIAL PRIMARY KEY,"
                        f"  snapshot_id INTEGER NOT NULL,"
                        f"  dimension_scores TEXT NOT NULL,"
                        f"  config_version TEXT DEFAULT '',"
                        f"  regime TEXT DEFAULT '',"
                        f"  timestamp TEXT NOT NULL"
                        f")"
                    )
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_snap ON {table} (snapshot_id)"
                    )
                    cur.execute(
                        f"INSERT INTO {table} (snapshot_id, dimension_scores, config_version, "
                        f"regime, timestamp) VALUES (%s, %s, %s, %s, %s)",
                        (snapshot_id, payload, config_version, regime, now),
                    )
                conn.commit()
        else:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    f"  snapshot_id INTEGER NOT NULL,"
                    f"  dimension_scores TEXT NOT NULL,"
                    f"  config_version TEXT DEFAULT '',"
                    f"  regime TEXT DEFAULT '',"
                    f"  timestamp TEXT NOT NULL"
                    f")"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_snap ON {table} (snapshot_id)"
                )
                conn.execute(
                    f"INSERT INTO {table} (snapshot_id, dimension_scores, config_version, "
                    f"regime, timestamp) VALUES (?, ?, ?, ?, ?)",
                    (snapshot_id, payload, config_version, regime, now),
                )
                conn.commit()

    def compute_ic(self, window_hours: int = 24, days: int = 30) -> Dict[str, Any]:
        """Compute Information Coefficient (Spearman rank correlation) per dimension.

        For each evaluation point:
          - Load dimension scores at signal time (from ic_dimension_scores)
          - Load actual return (pct_change from performance_accuracy)
          - Compute Spearman rank correlation across all assets at that point

        Returns IC per dimension, per regime, and overall.
        """
        snap_table = "performance_snapshots"
        acc_table = "performance_accuracy"
        dim_table = "ic_dimension_scores"
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Query: join snapshots + accuracy + dimension_scores
        # Group by timestamp (rounded to nearest snapshot batch) to get cross-asset slices
        query = (
            f"SELECT s.id, s.asset, s.timestamp, s.signal_score, "
            f"a.pct_change, d.dimension_scores, d.config_version, d.regime "
            f"FROM {acc_table} a "
            f"JOIN {snap_table} s ON a.snapshot_id = s.id "
            f"JOIN {dim_table} d ON d.snapshot_id = s.id "
            f"WHERE s.timestamp >= {{since}} "
            f"AND a.window_hours = {{window}} "
            f"AND a.pct_change IS NOT NULL "
            f"ORDER BY s.timestamp ASC"
        )

        rows = []
        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT s.id, s.asset, s.timestamp, s.signal_score, "
                            f"a.pct_change, d.dimension_scores, d.config_version, d.regime "
                            f"FROM {acc_table} a "
                            f"JOIN {snap_table} s ON a.snapshot_id = s.id "
                            f"JOIN {dim_table} d ON d.snapshot_id = s.id "
                            f"WHERE s.timestamp >= %s "
                            f"AND a.window_hours = %s "
                            f"AND a.pct_change IS NOT NULL "
                            f"ORDER BY s.timestamp ASC",
                            (since, window_hours),
                        )
                        rows = cur.fetchall()
            except Exception as e:
                logger.warning("compute_ic postgres error: %s", e)
                return {"error": str(e), "dimensions": {}, "by_regime": {}, "overall_ic": None}
        else:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    rows = conn.execute(
                        f"SELECT s.id, s.asset, s.timestamp, s.signal_score, "
                        f"a.pct_change, d.dimension_scores, d.config_version, d.regime "
                        f"FROM {acc_table} a "
                        f"JOIN {snap_table} s ON a.snapshot_id = s.id "
                        f"JOIN {dim_table} d ON d.snapshot_id = s.id "
                        f"WHERE s.timestamp >= ? "
                        f"AND a.window_hours = ? "
                        f"AND a.pct_change IS NOT NULL "
                        f"ORDER BY s.timestamp ASC",
                        (since, window_hours),
                    ).fetchall()
            except Exception as e:
                logger.warning("compute_ic sqlite error: %s", e)
                return {"error": str(e), "dimensions": {}, "by_regime": {}, "overall_ic": None}

        if not rows:
            return {"dimensions": {}, "by_regime": {}, "overall_ic": None,
                    "total_observations": 0, "window_hours": window_hours}

        # Parse rows into structured data
        # Group by timestamp (snapshot batch) to compute cross-asset IC per slice
        from collections import defaultdict

        # Each row: (id, asset, timestamp, signal_score, pct_change, dim_scores_json, config_ver, regime)
        # Group by timestamp to get "slices" of cross-asset data
        slices = defaultdict(list)
        for r in rows:
            ts = r[2][:16]  # Truncate to minute precision for grouping
            dim_scores = json.loads(r[5]) if isinstance(r[5], str) else r[5]
            slices[ts].append({
                "asset": r[1],
                "composite": r[3],
                "pct_change": r[4],
                "dimensions": dim_scores,
                "config_version": r[6],
                "regime": r[7],
            })

        # Compute Spearman rank correlation per dimension
        # IC = rank_correlation(dimension_score, future_return) across assets
        all_dimensions = set()
        for ts_data in slices.values():
            for obs in ts_data:
                all_dimensions.update(obs["dimensions"].keys())

        # Collect per-dimension score-return pairs (across all slices)
        dim_pairs: Dict[str, list] = {d: [] for d in all_dimensions}
        dim_pairs["composite"] = []
        regime_dim_pairs: Dict[str, Dict[str, list]] = defaultdict(lambda: defaultdict(list))

        for ts, observations in slices.items():
            if len(observations) < 3:  # Need at least 3 assets for meaningful correlation
                continue
            returns = [obs["pct_change"] for obs in observations]
            for dim in all_dimensions:
                # Only include assets that have real data for this dimension
                # (skip missing dimensions instead of injecting synthetic 50.0)
                dim_obs = [(obs["dimensions"][dim], obs["pct_change"])
                           for obs in observations if dim in obs["dimensions"]]
                if len(dim_obs) >= 3:
                    dim_scores = [d[0] for d in dim_obs]
                    dim_returns = [d[1] for d in dim_obs]
                    dim_pairs[dim].append((dim_scores, dim_returns))
            # Composite IC (always available)
            composites = [obs["composite"] for obs in observations]
            dim_pairs["composite"].append((composites, returns))
            # Per-regime tracking
            regime = observations[0]["regime"] if observations else ""
            for dim in all_dimensions:
                dim_obs = [(obs["dimensions"][dim], obs["pct_change"])
                           for obs in observations if dim in obs["dimensions"]]
                if len(dim_obs) >= 3:
                    dim_scores = [d[0] for d in dim_obs]
                    dim_returns = [d[1] for d in dim_obs]
                    regime_dim_pairs[regime][dim].append((dim_scores, dim_returns))
            regime_dim_pairs[regime]["composite"].append((composites, returns))

        def _spearman_ic(pairs: list) -> Optional[float]:
            """Compute average Spearman IC across multiple cross-sectional slices."""
            if not pairs:
                return None
            ics = []
            for scores, returns in pairs:
                n = len(scores)
                if n < 3:
                    continue
                # Rank both arrays
                score_ranks = _rank_array(scores)
                return_ranks = _rank_array(returns)
                # Spearman = Pearson of ranks
                ic = _pearson(score_ranks, return_ranks)
                if ic is not None:
                    ics.append(ic)
            return round(sum(ics) / len(ics), 4) if ics else None

        # Compute IC per dimension
        result_dims = {}
        for dim in sorted(all_dimensions | {"composite"}):
            ic = _spearman_ic(dim_pairs.get(dim, []))
            n_slices = len(dim_pairs.get(dim, []))
            result_dims[dim] = {"ic": ic, "slices": n_slices}

            # IC Information Ratio (ICIR) = mean(IC) / std(IC)
            if dim_pairs.get(dim):
                slice_ics = []
                for scores, returns in dim_pairs[dim]:
                    if len(scores) >= 3:
                        sr = _rank_array(scores)
                        rr = _rank_array(returns)
                        ic_val = _pearson(sr, rr)
                        if ic_val is not None:
                            slice_ics.append(ic_val)
                # Report actual slices used for ICIR (not total)
                result_dims[dim]["slices_used"] = len(slice_ics)
                if len(slice_ics) >= 2:
                    mean_ic = sum(slice_ics) / len(slice_ics)
                    std_ic = (sum((x - mean_ic) ** 2 for x in slice_ics) / len(slice_ics)) ** 0.5
                    icir = round(mean_ic / std_ic, 3) if std_ic > 0.001 else None
                    result_dims[dim]["icir"] = icir

        # Compute IC per regime per dimension
        result_regimes: Dict[str, Dict] = {}
        for regime, dim_data in regime_dim_pairs.items():
            result_regimes[regime] = {}
            for dim in sorted(dim_data.keys()):
                ic = _spearman_ic(dim_data[dim])
                n_slices = len(dim_data[dim])
                result_regimes[regime][dim] = {"ic": ic, "slices": n_slices}

        return {
            "dimensions": result_dims,
            "by_regime": result_regimes,
            "overall_ic": result_dims.get("composite", {}).get("ic"),
            "total_observations": len(rows),
            "total_slices": len(slices),
            "window_hours": window_hours,
            "days": days,
        }

    def reset_accuracy_data(self) -> Dict[str, int]:
        """Drop and recreate accuracy table, reset snapshot evaluated flags.

        Used when methodology/schema changes make old accuracy data invalid.
        Drops the old table entirely (handles column changes like
        direction_correct → gradient_score) and lets save_performance_accuracy
        recreate it with the new schema on next evaluation.
        """
        acc_table = "performance_accuracy"
        snap_table = "performance_snapshots"
        deleted = 0
        reset = 0

        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        try:
                            cur.execute(f"SELECT COUNT(*) FROM {acc_table}")
                            row = cur.fetchone()
                            deleted = row[0] if row else 0
                        except Exception:
                            deleted = 0
                            conn.rollback()

                        cur.execute(f"DROP TABLE IF EXISTS {acc_table}")
                        cur.execute(
                            f"UPDATE {snap_table} SET "
                            f"evaluated_24h = FALSE, evaluated_48h = FALSE, evaluated_7d = FALSE"
                        )
                        cur.execute(f"SELECT COUNT(*) FROM {snap_table}")
                        row = cur.fetchone()
                        reset = row[0] if row else 0
                    conn.commit()
            except Exception:
                pass
        else:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    try:
                        row = conn.execute(f"SELECT COUNT(*) FROM {acc_table}").fetchone()
                        deleted = row[0] if row else 0
                    except Exception:
                        deleted = 0

                    conn.execute(f"DROP TABLE IF EXISTS {acc_table}")
                    conn.execute(
                        f"UPDATE {snap_table} SET "
                        f"evaluated_24h = 0, evaluated_48h = 0, evaluated_7d = 0"
                    )
                    row = conn.execute(f"SELECT COUNT(*) FROM {snap_table}").fetchone()
                    reset = row[0] if row else 0
                    conn.commit()
            except Exception:
                pass

        return {"accuracy_rows_deleted": deleted, "snapshots_reset": reset}

    # ------------------------------------------------------------------ #
    #  API usage tracking
    # ------------------------------------------------------------------ #

    def save_api_request(self, endpoint: str, method: str, user_agent: str,
                          status_code: int, duration_ms: float,
                          client_ip: str = "",
                          payment_status: str | None = None,
                          request_source: str = "unknown",
                          referer: str = "",
                          origin: str = "",
                          referer_source: str = "",
                          client_fingerprint: str = "") -> None:
        """Log an API request for analytics.

        payment_status: None (not a paid route), 'payment_required' (402),
                        'paid' (200 with payment header), 'payment_failed',
                        'free' (paid route served free — gate disabled).
        request_source: 'internal', 'external', or 'unknown'.
        """
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
                        f"  client_ip TEXT,"
                        f"  payment_status TEXT"
                        f")"
                    )
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table} (timestamp)"
                    )
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_ep ON {table} (endpoint)"
                    )
                    # Migrate: add columns if they don't exist
                    # Use IF NOT EXISTS to avoid aborting the PG transaction
                    for col in ["payment_status TEXT", "request_source TEXT DEFAULT 'unknown'",
                                "referer TEXT DEFAULT ''", "origin TEXT DEFAULT ''",
                                "referer_source TEXT DEFAULT ''", "client_fingerprint TEXT DEFAULT ''"]:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col}")
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table}_source ON {table} (request_source)"
                    )
                    cur.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_api_requests_refsrc ON {table} (referer_source)"
                    )
                    cur.execute(
                        f"INSERT INTO {table} (timestamp, endpoint, method, user_agent, "
                        f"status_code, duration_ms, client_ip, payment_status, "
                        f"request_source, referer, origin, referer_source, client_fingerprint) "
                        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (now, endpoint, method, user_agent, status_code, duration_ms,
                         client_ip, payment_status, request_source, referer, origin,
                         referer_source, client_fingerprint),
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
                    f"  client_ip TEXT,"
                    f"  payment_status TEXT"
                    f")"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table} (timestamp)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_ep ON {table} (endpoint)"
                )
                # Migrate: add columns if they don't exist
                for col in ["payment_status TEXT", "request_source TEXT DEFAULT 'unknown'",
                            "referer TEXT DEFAULT ''", "origin TEXT DEFAULT ''",
                            "referer_source TEXT DEFAULT ''", "client_fingerprint TEXT DEFAULT ''"]:
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                    except Exception:
                        logger.debug("Column migration skipped (already exists): %s", col.split()[0])
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_source ON {table} (request_source)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_api_requests_refsrc ON {table} (referer_source)"
                )
                conn.execute(
                    f"INSERT INTO {table} (timestamp, endpoint, method, user_agent, "
                    f"status_code, duration_ms, client_ip, payment_status, "
                    f"request_source, referer, origin, referer_source, client_fingerprint) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (now, endpoint, method, user_agent, status_code, duration_ms,
                     client_ip, payment_status, request_source, referer, origin,
                     referer_source, client_fingerprint),
                )
                conn.commit()

    def save_error_event(self, error_type: str, source: str, message: str,
                         context: Optional[Dict] = None) -> None:
        """Log a structured error event for analytics.

        error_type: 'agent_timeout', 'agent_crash', 'api_5xx', 'payment_failure'
        source: agent name or endpoint path
        """
        event = {
            "error_type": error_type,
            "source": source,
            "message": str(message)[:500],
            "context": context or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            # Use a rolling key so we keep last N errors per type
            key = f"{error_type}:{source}:{int(time.time())}"
            self.save_kv_json("error_log", key, event)
        except Exception:
            logger.warning("save_error_event failed: %s", traceback.format_exc(limit=1))

    def load_error_summary(self, days: int = 7) -> Dict[str, Any]:
        """Load error summary from api_requests + error_log kv store."""
        table = "api_requests"
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        result: Dict[str, Any] = {
            "api_errors": {
                "total_5xx": 0,
                "total_4xx": 0,
                "by_endpoint": {},
                "by_status_code": {},
                "error_rate_pct": 0,
            },
            "payment_errors": {
                "total_failures": 0,
                "failure_rate_pct": 0,
            },
            "recent_errors": [],
        }

        try:
            if self.backend == "postgres":
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        # Total requests for rate calculation
                        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE timestamp >= %s", (since,))
                        total = cur.fetchone()[0] or 1

                        # 5xx errors by endpoint
                        cur.execute(
                            f"SELECT endpoint, status_code, COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND status_code >= 500 "
                            f"GROUP BY endpoint, status_code ORDER BY COUNT(*) DESC",
                            (since,),
                        )
                        for row in cur.fetchall():
                            result["api_errors"]["total_5xx"] += row[2]
                            result["api_errors"]["by_endpoint"][row[0]] = \
                                result["api_errors"]["by_endpoint"].get(row[0], 0) + row[2]
                            sc = str(row[1])
                            result["api_errors"]["by_status_code"][sc] = \
                                result["api_errors"]["by_status_code"].get(sc, 0) + row[2]

                        # 4xx errors (excluding 402 which is expected)
                        cur.execute(
                            f"SELECT COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND status_code >= 400 AND status_code < 500 "
                            f"AND status_code != 402",
                            (since,),
                        )
                        row = cur.fetchone()
                        result["api_errors"]["total_4xx"] = row[0] if row else 0

                        result["api_errors"]["error_rate_pct"] = round(
                            (result["api_errors"]["total_5xx"] / total) * 100, 2
                        )

                        # Payment failures
                        cur.execute(
                            f"SELECT COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND payment_status = 'payment_failed'",
                            (since,),
                        )
                        row = cur.fetchone()
                        result["payment_errors"]["total_failures"] = row[0] if row else 0

                        # Payment failure rate (vs total payment attempts)
                        cur.execute(
                            f"SELECT COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND payment_status IN ('paid', 'payment_failed')",
                            (since,),
                        )
                        row = cur.fetchone()
                        pay_total = row[0] if row else 0
                        if pay_total > 0:
                            result["payment_errors"]["failure_rate_pct"] = round(
                                (result["payment_errors"]["total_failures"] / pay_total) * 100, 2
                            )
            else:
                with sqlite3.connect(self.db_path) as conn:
                    total = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE timestamp >= ?", (since,)
                    ).fetchone()[0] or 1

                    rows = conn.execute(
                        f"SELECT endpoint, status_code, COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND status_code >= 500 "
                        f"GROUP BY endpoint, status_code ORDER BY COUNT(*) DESC",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        result["api_errors"]["total_5xx"] += row[2]
                        result["api_errors"]["by_endpoint"][row[0]] = \
                            result["api_errors"]["by_endpoint"].get(row[0], 0) + row[2]
                        sc = str(row[1])
                        result["api_errors"]["by_status_code"][sc] = \
                            result["api_errors"]["by_status_code"].get(sc, 0) + row[2]

                    row = conn.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND status_code >= 400 AND status_code < 500 "
                        f"AND status_code != 402",
                        (since,),
                    ).fetchone()
                    result["api_errors"]["total_4xx"] = row[0] if row else 0

                    result["api_errors"]["error_rate_pct"] = round(
                        (result["api_errors"]["total_5xx"] / total) * 100, 2
                    )

                    row = conn.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND payment_status = 'payment_failed'",
                        (since,),
                    ).fetchone()
                    result["payment_errors"]["total_failures"] = row[0] if row else 0

                    row = conn.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND payment_status IN ('paid', 'payment_failed')",
                        (since,),
                    ).fetchone()
                    pay_total = row[0] if row else 0
                    if pay_total > 0:
                        result["payment_errors"]["failure_rate_pct"] = round(
                            (result["payment_errors"]["total_failures"] / pay_total) * 100, 2
                        )

            # Load recent errors from kv_json error_log
            result["recent_errors"] = self._load_recent_error_events(20)

        except Exception:
            logger.error("load_error_summary failed: %s", traceback.format_exc(limit=1))

        return result

    def _load_recent_error_events(self, limit: int = 20) -> List[Dict]:
        """Load recent error events from kv_json store."""
        events = []
        try:
            if self.backend == "postgres":
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT key, value_json FROM kv_json "
                            "WHERE namespace = 'error_log' "
                            "ORDER BY id DESC LIMIT %s",
                            (limit,),
                        )
                        for row in cur.fetchall():
                            try:
                                events.append(json.loads(row[1]) if isinstance(row[1], str) else row[1])
                            except Exception:
                                pass
            else:
                with sqlite3.connect(self.db_path) as conn:
                    rows = conn.execute(
                        "SELECT key, value_json FROM kv_json "
                        "WHERE namespace = 'error_log' "
                        "ORDER BY ROWID DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                    for row in rows:
                        try:
                            events.append(json.loads(row[1]) if isinstance(row[1], str) else row[1])
                        except Exception:
                            pass
        except Exception:
            logger.warning("_load_recent_error_events failed: %s", traceback.format_exc(limit=1))
        return events

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
            "by_source": {"internal": 0, "external": 0, "unknown": 0},
            "external_unique_ips": 0,
            "external_requests_per_day": {},
            "external_by_client_type": {},
            "external_top_user_agents": [],
            "external_by_endpoint": {},
            "external_by_referer_source": {},
            "funnel": {
                "challenges_402": 0,
                "payment_attempted": 0,
                "payment_succeeded": 0,
                "payment_failed": 0,
            },
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

                        # --- Source segmentation (internal / external) ---
                        cur.execute(
                            f"SELECT COALESCE(request_source, 'unknown'), COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s GROUP BY COALESCE(request_source, 'unknown')",
                            (since,),
                        )
                        for row in cur.fetchall():
                            result["by_source"][row[0]] = row[1]

                        cur.execute(
                            f"SELECT COUNT(DISTINCT client_ip) FROM {table} "
                            f"WHERE timestamp >= %s AND client_ip != '' "
                            f"AND COALESCE(request_source, 'unknown') = 'external'",
                            (since,),
                        )
                        row = cur.fetchone()
                        result["external_unique_ips"] = row[0] if row else 0

                        cur.execute(
                            f"SELECT LEFT(timestamp, 10) as day, COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND COALESCE(request_source, 'unknown') = 'external' "
                            f"GROUP BY LEFT(timestamp, 10) ORDER BY day",
                            (since,),
                        )
                        for row in cur.fetchall():
                            result["external_requests_per_day"][row[0]] = row[1]

                        cur.execute(
                            f"SELECT user_agent, COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND COALESCE(request_source, 'unknown') = 'external' "
                            f"GROUP BY user_agent ORDER BY COUNT(*) DESC LIMIT 20",
                            (since,),
                        )
                        for row in cur.fetchall():
                            ua = row[0] or "unknown"
                            ua_type = _classify_user_agent(ua)
                            result["external_by_client_type"][ua_type] = (
                                result["external_by_client_type"].get(ua_type, 0) + row[1]
                            )
                            result["external_top_user_agents"].append(
                                {"user_agent": ua, "requests": row[1], "type": ua_type}
                            )

                        # --- External by endpoint ---
                        cur.execute(
                            f"SELECT endpoint, COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND COALESCE(request_source, 'unknown') = 'external' "
                            f"GROUP BY endpoint ORDER BY COUNT(*) DESC LIMIT 15",
                            (since,),
                        )
                        for row in cur.fetchall():
                            result["external_by_endpoint"][row[0]] = row[1]

                        # --- Attribution: referer source for external requests ---
                        cur.execute(
                            f"SELECT COALESCE(NULLIF(referer_source, ''), 'direct'), COUNT(*) "
                            f"FROM {table} "
                            f"WHERE timestamp >= %s AND COALESCE(request_source, 'unknown') = 'external' "
                            f"GROUP BY COALESCE(NULLIF(referer_source, ''), 'direct') "
                            f"ORDER BY COUNT(*) DESC",
                            (since,),
                        )
                        for row in cur.fetchall():
                            result["external_by_referer_source"][row[0]] = row[1]

                        # --- Conversion funnel ---
                        cur.execute(
                            f"SELECT payment_status, COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND payment_status IS NOT NULL "
                            f"GROUP BY payment_status",
                            (since,),
                        )
                        for row in cur.fetchall():
                            if row[0] == "payment_required":
                                result["funnel"]["challenges_402"] = row[1]
                            elif row[0] == "paid":
                                result["funnel"]["payment_succeeded"] = row[1]
                            elif row[0] == "payment_failed":
                                result["funnel"]["payment_failed"] = row[1]
                        result["funnel"]["payment_attempted"] = (
                            result["funnel"]["payment_succeeded"] + result["funnel"]["payment_failed"]
                        )

            except Exception:
                logger.error("load_api_analytics (postgres) failed: %s", traceback.format_exc(limit=1))
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

                    # --- Source segmentation (internal / external) ---
                    rows = conn.execute(
                        f"SELECT COALESCE(request_source, 'unknown'), COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? GROUP BY COALESCE(request_source, 'unknown')",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        result["by_source"][row[0]] = row[1]

                    row = conn.execute(
                        f"SELECT COUNT(DISTINCT client_ip) FROM {table} "
                        f"WHERE timestamp >= ? AND client_ip != '' "
                        f"AND COALESCE(request_source, 'unknown') = 'external'",
                        (since,),
                    ).fetchone()
                    result["external_unique_ips"] = row[0] if row else 0

                    rows = conn.execute(
                        f"SELECT SUBSTR(timestamp, 1, 10) as day, COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND COALESCE(request_source, 'unknown') = 'external' "
                        f"GROUP BY SUBSTR(timestamp, 1, 10) ORDER BY day",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        result["external_requests_per_day"][row[0]] = row[1]

                    rows = conn.execute(
                        f"SELECT user_agent, COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND COALESCE(request_source, 'unknown') = 'external' "
                        f"GROUP BY user_agent ORDER BY COUNT(*) DESC LIMIT 20",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        ua = row[0] or "unknown"
                        ua_type = _classify_user_agent(ua)
                        result["external_by_client_type"][ua_type] = (
                            result["external_by_client_type"].get(ua_type, 0) + row[1]
                        )
                        result["external_top_user_agents"].append(
                            {"user_agent": ua, "requests": row[1], "type": ua_type}
                        )

                    # --- External by endpoint ---
                    rows = conn.execute(
                        f"SELECT endpoint, COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND COALESCE(request_source, 'unknown') = 'external' "
                        f"GROUP BY endpoint ORDER BY COUNT(*) DESC LIMIT 15",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        result["external_by_endpoint"][row[0]] = row[1]

                    # --- Attribution: referer source for external requests ---
                    rows = conn.execute(
                        f"SELECT COALESCE(NULLIF(referer_source, ''), 'direct'), COUNT(*) "
                        f"FROM {table} "
                        f"WHERE timestamp >= ? AND COALESCE(request_source, 'unknown') = 'external' "
                        f"GROUP BY COALESCE(NULLIF(referer_source, ''), 'direct') "
                        f"ORDER BY COUNT(*) DESC",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        result["external_by_referer_source"][row[0]] = row[1]

                    # --- Conversion funnel ---
                    rows = conn.execute(
                        f"SELECT payment_status, COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND payment_status IS NOT NULL "
                        f"GROUP BY payment_status",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        if row[0] == "payment_required":
                            result["funnel"]["challenges_402"] = row[1]
                        elif row[0] == "paid":
                            result["funnel"]["payment_succeeded"] = row[1]
                        elif row[0] == "payment_failed":
                            result["funnel"]["payment_failed"] = row[1]
                    result["funnel"]["payment_attempted"] = (
                        result["funnel"]["payment_succeeded"] + result["funnel"]["payment_failed"]
                    )

            except Exception:
                logger.error("load_api_analytics (sqlite) failed: %s", traceback.format_exc(limit=1))

        return result

    # ------------------------------------------------------------------ #
    #  x402 payment analytics
    # ------------------------------------------------------------------ #

    def load_x402_analytics(self, days: int = 30) -> Dict[str, Any]:
        """Load x402 payment-specific analytics."""
        table = "api_requests"
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        result: Dict[str, Any] = {
            "total_paid_calls": 0,
            "total_402_challenges": 0,
            "total_payment_failures": 0,
            "estimated_revenue_usdc": 0.0,
            "by_endpoint": {},
            "by_client_type": {},
            "paid_per_day": {},
            "paid_by_source": {"internal": 0, "external": 0, "unknown": 0},
            "external_paid_calls": 0,
            "internal_paid_calls": 0,
            "external_revenue_usdc": 0.0,
            "avg_paid_latency_ms": 0,
        }

        def _process_rows(rows_paid, rows_402, rows_fail, rows_ep, rows_client,
                          rows_daily, avg_lat):
            result["total_paid_calls"] = rows_paid or 0
            result["total_402_challenges"] = rows_402 or 0
            result["total_payment_failures"] = rows_fail or 0
            result["estimated_revenue_usdc"] = round(
                (rows_paid or 0) * 0.001, 4
            )
            if rows_ep:
                for row in rows_ep:
                    result["by_endpoint"][row[0]] = row[1]
            if rows_client:
                for row in rows_client:
                    ua = row[0] or "unknown"
                    ua_type = _classify_user_agent(ua)
                    result["by_client_type"][ua_type] = (
                        result["by_client_type"].get(ua_type, 0) + row[1]
                    )
            if rows_daily:
                for row in rows_daily:
                    result["paid_per_day"][row[0]] = row[1]
            result["avg_paid_latency_ms"] = round(float(avg_lat), 1) if avg_lat else 0

        if self.backend == "postgres":
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND payment_status = 'paid'",
                            (since,),
                        )
                        paid = (cur.fetchone() or [0])[0]

                        cur.execute(
                            f"SELECT COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND payment_status = 'payment_required'",
                            (since,),
                        )
                        challenges = (cur.fetchone() or [0])[0]

                        cur.execute(
                            f"SELECT COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND payment_status = 'payment_failed'",
                            (since,),
                        )
                        fails = (cur.fetchone() or [0])[0]

                        cur.execute(
                            f"SELECT endpoint, COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND payment_status = 'paid' "
                            f"GROUP BY endpoint ORDER BY COUNT(*) DESC",
                            (since,),
                        )
                        ep_rows = cur.fetchall()

                        cur.execute(
                            f"SELECT user_agent, COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND payment_status = 'paid' "
                            f"GROUP BY user_agent ORDER BY COUNT(*) DESC LIMIT 20",
                            (since,),
                        )
                        client_rows = cur.fetchall()

                        cur.execute(
                            f"SELECT LEFT(timestamp, 10), COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND payment_status = 'paid' "
                            f"GROUP BY LEFT(timestamp, 10) ORDER BY 1",
                            (since,),
                        )
                        daily_rows = cur.fetchall()

                        cur.execute(
                            f"SELECT AVG(duration_ms) FROM {table} "
                            f"WHERE timestamp >= %s AND payment_status = 'paid' "
                            f"AND duration_ms > 0",
                            (since,),
                        )
                        avg = (cur.fetchone() or [0])[0]

                        _process_rows(paid, challenges, fails, ep_rows,
                                      client_rows, daily_rows, avg)

                        # --- Source segmentation for paid calls ---
                        cur.execute(
                            f"SELECT COALESCE(request_source, 'unknown'), COUNT(*) FROM {table} "
                            f"WHERE timestamp >= %s AND payment_status = 'paid' "
                            f"GROUP BY COALESCE(request_source, 'unknown')",
                            (since,),
                        )
                        for row in cur.fetchall():
                            result["paid_by_source"][row[0]] = row[1]
                        result["external_paid_calls"] = result["paid_by_source"].get("external", 0)
                        result["internal_paid_calls"] = result["paid_by_source"].get("internal", 0)
                        result["external_revenue_usdc"] = round(
                            result["external_paid_calls"] * 0.001, 4
                        )

            except Exception:
                logger.error("load_x402_analytics (postgres) failed: %s", traceback.format_exc(limit=1))
        else:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    paid = (conn.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND payment_status = 'paid'",
                        (since,),
                    ).fetchone() or [0])[0]

                    challenges = (conn.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND payment_status = 'payment_required'",
                        (since,),
                    ).fetchone() or [0])[0]

                    fails = (conn.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND payment_status = 'payment_failed'",
                        (since,),
                    ).fetchone() or [0])[0]

                    ep_rows = conn.execute(
                        f"SELECT endpoint, COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND payment_status = 'paid' "
                        f"GROUP BY endpoint ORDER BY COUNT(*) DESC",
                        (since,),
                    ).fetchall()

                    client_rows = conn.execute(
                        f"SELECT user_agent, COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND payment_status = 'paid' "
                        f"GROUP BY user_agent ORDER BY COUNT(*) DESC LIMIT 20",
                        (since,),
                    ).fetchall()

                    daily_rows = conn.execute(
                        f"SELECT SUBSTR(timestamp, 1, 10), COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND payment_status = 'paid' "
                        f"GROUP BY SUBSTR(timestamp, 1, 10) ORDER BY 1",
                        (since,),
                    ).fetchall()

                    avg = (conn.execute(
                        f"SELECT AVG(duration_ms) FROM {table} "
                        f"WHERE timestamp >= ? AND payment_status = 'paid' "
                        f"AND duration_ms > 0",
                        (since,),
                    ).fetchone() or [0])[0]

                    _process_rows(paid, challenges, fails, ep_rows,
                                  client_rows, daily_rows, avg)

                    # --- Source segmentation for paid calls ---
                    rows = conn.execute(
                        f"SELECT COALESCE(request_source, 'unknown'), COUNT(*) FROM {table} "
                        f"WHERE timestamp >= ? AND payment_status = 'paid' "
                        f"GROUP BY COALESCE(request_source, 'unknown')",
                        (since,),
                    ).fetchall()
                    for row in rows:
                        result["paid_by_source"][row[0]] = row[1]
                    result["external_paid_calls"] = result["paid_by_source"].get("external", 0)
                    result["internal_paid_calls"] = result["paid_by_source"].get("internal", 0)
                    result["external_revenue_usdc"] = round(
                        result["external_paid_calls"] * 0.001, 4
                    )

            except Exception:
                logger.error("load_x402_analytics (sqlite) failed: %s", traceback.format_exc(limit=1))

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
