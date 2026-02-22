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
    return psycopg2.connect(os.environ["DATABASE_URL"])


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
