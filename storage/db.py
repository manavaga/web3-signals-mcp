# storage/db.py
"""SQLite/Postgres dual-mode storage.

Selects backend based on DATABASE_URL env var.
All table creation is lazy (on first write).
"""
from __future__ import annotations
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


def _get_backend() -> str:
    return "postgres" if os.getenv("DATABASE_URL") else "sqlite"


class Storage:
    def __init__(self, db_path: str = "signals.db"):
        self._backend = _get_backend()
        self._db_path = db_path
        self._created_tables: set[str] = set()

    def _connect(self):
        if self._backend == "postgres":
            import psycopg2
            return psycopg2.connect(os.environ["DATABASE_URL"])
        return sqlite3.connect(self._db_path)

    def _ph(self) -> str:
        return "%s" if self._backend == "postgres" else "?"

    def _table_name(self, name: str) -> str:
        return re.sub(r"[^a-z0-9_]", "_", name.lower())

    def _ensure_agent_table(self, agent_name: str):
        table = f"agent_{self._table_name(agent_name)}"
        if table in self._created_tables:
            return table
        pk = "SERIAL PRIMARY KEY" if self._backend == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                id {pk},
                timestamp TEXT NOT NULL,
                data_json TEXT NOT NULL
            )""")
            cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table}(timestamp)")
            conn.commit()
            self._created_tables.add(table)
        finally:
            conn.close()
        return table

    def _ensure_kv_table(self, namespace: str):
        table = f"kv_{self._table_name(namespace)}"
        if table in self._created_tables:
            return table
        pk = "SERIAL PRIMARY KEY" if self._backend == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        float_type = "DOUBLE PRECISION" if self._backend == "postgres" else "REAL"
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                id {pk},
                key TEXT NOT NULL,
                value {float_type} NOT NULL,
                timestamp TEXT NOT NULL
            )""")
            conn.commit()
            self._created_tables.add(table)
        finally:
            conn.close()
        return table

    def _ensure_kvj_table(self, namespace: str):
        table = f"kvj_{self._table_name(namespace)}"
        if table in self._created_tables:
            return table
        pk = "SERIAL PRIMARY KEY" if self._backend == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                id {pk},
                key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )""")
            conn.commit()
            self._created_tables.add(table)
        finally:
            conn.close()
        return table

    def _ensure_perf_tables(self):
        if "performance_snapshots" in self._created_tables:
            return
        pk = "SERIAL PRIMARY KEY" if self._backend == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        float_type = "DOUBLE PRECISION" if self._backend == "postgres" else "REAL"
        bool_default = "BOOLEAN DEFAULT FALSE" if self._backend == "postgres" else "INTEGER DEFAULT 0"
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""CREATE TABLE IF NOT EXISTS performance_snapshots (
                id {pk},
                timestamp TEXT NOT NULL,
                asset TEXT NOT NULL,
                signal_score {float_type} NOT NULL,
                signal_direction TEXT NOT NULL,
                price_at_signal {float_type} NOT NULL,
                sources_count INTEGER NOT NULL,
                detail TEXT,
                evaluated_24h {bool_default},
                evaluated_48h {bool_default}
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_perf_snap_ts ON performance_snapshots(timestamp)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_perf_snap_asset ON performance_snapshots(asset)")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS performance_accuracy (
                id {pk},
                snapshot_id INTEGER NOT NULL,
                window_hours INTEGER NOT NULL,
                price_at_window {float_type} NOT NULL,
                gradient_score {float_type},
                pct_change {float_type},
                evaluated_at TEXT NOT NULL
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_perf_acc_sid ON performance_accuracy(snapshot_id)")
            cur.execute(f"""CREATE TABLE IF NOT EXISTS ic_dimension_scores (
                id {pk},
                snapshot_id INTEGER NOT NULL,
                dimension_scores TEXT NOT NULL,
                config_version TEXT DEFAULT '',
                regime TEXT DEFAULT '',
                timestamp TEXT NOT NULL
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ic_sid ON ic_dimension_scores(snapshot_id)")
            conn.commit()
            self._created_tables.add("performance_snapshots")
        finally:
            conn.close()

    # --- Agent Data ---

    def save(self, agent_name: str, data: dict[str, Any]) -> None:
        table = self._ensure_agent_table(agent_name)
        ts = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            cur.execute(f"INSERT INTO {table} (timestamp, data_json) VALUES ({ph}, {ph})", (ts, json.dumps(data)))
            conn.commit()
        finally:
            conn.close()

    def load_latest(self, agent_name: str) -> Optional[dict[str, Any]]:
        table = self._ensure_agent_table(agent_name)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT data_json FROM {table} ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
        finally:
            conn.close()

    def load_recent(self, agent_name: str, days: int) -> list[dict[str, Any]]:
        table = self._ensure_agent_table(agent_name)
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            cur.execute(
                f"SELECT data_json, timestamp FROM {table} WHERE timestamp >= datetime('now', {ph}) ORDER BY id DESC",
                (f"-{days} days",)
            )
            return [{"data": json.loads(row[0]), "timestamp": row[1]} for row in cur.fetchall()]
        finally:
            conn.close()

    def load_history(self, agent_name: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        table = self._ensure_agent_table(agent_name)
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            cur.execute(
                f"SELECT data_json, timestamp FROM {table} ORDER BY id DESC LIMIT {ph} OFFSET {ph}",
                (limit, offset)
            )
            return [{"data": json.loads(row[0]), "timestamp": row[1]} for row in cur.fetchall()]
        finally:
            conn.close()

    def load_all_latest(self, agent_names: list[str]) -> dict[str, Optional[dict[str, Any]]]:
        return {name: self.load_latest(name) for name in agent_names}

    def count_rows(self, agent_name: str) -> int:
        table = self._ensure_agent_table(agent_name)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]
        finally:
            conn.close()

    # --- KV Float ---

    def save_kv(self, namespace: str, key: str, value: float) -> None:
        table = self._ensure_kv_table(namespace)
        ts = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            cur.execute(f"DELETE FROM {table} WHERE key = {ph}", (key,))
            cur.execute(f"INSERT INTO {table} (key, value, timestamp) VALUES ({ph}, {ph}, {ph})", (key, value, ts))
            conn.commit()
        finally:
            conn.close()

    def load_kv(self, namespace: str, key: str) -> Optional[float]:
        table = self._ensure_kv_table(namespace)
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            cur.execute(f"SELECT value FROM {table} WHERE key = {ph} ORDER BY id DESC LIMIT 1", (key,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    # --- KV JSON ---

    def save_kv_json(self, namespace: str, key: str, value: dict) -> None:
        table = self._ensure_kvj_table(namespace)
        ts = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            cur.execute(f"DELETE FROM {table} WHERE key = {ph}", (key,))
            cur.execute(
                f"INSERT INTO {table} (key, value_json, timestamp) VALUES ({ph}, {ph}, {ph})",
                (key, json.dumps(value), ts)
            )
            conn.commit()
        finally:
            conn.close()

    def load_kv_json(self, namespace: str, key: str) -> Optional[dict]:
        table = self._ensure_kvj_table(namespace)
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            cur.execute(f"SELECT value_json FROM {table} WHERE key = {ph} ORDER BY id DESC LIMIT 1", (key,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
        finally:
            conn.close()

    # --- Performance ---

    def save_performance_snapshot(self, asset: str, signal_score: float, signal_direction: str,
                                  price_at_signal: float, sources_count: int, detail: str) -> Optional[int]:
        self._ensure_perf_tables()
        ts = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            if self._backend == "postgres":
                cur.execute(
                    f"""INSERT INTO performance_snapshots
                        (timestamp, asset, signal_score, signal_direction, price_at_signal, sources_count, detail)
                        VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph}) RETURNING id""",
                    (ts, asset, signal_score, signal_direction, price_at_signal, sources_count, detail)
                )
                sid = cur.fetchone()[0]
            else:
                cur.execute(
                    f"""INSERT INTO performance_snapshots
                        (timestamp, asset, signal_score, signal_direction, price_at_signal, sources_count, detail)
                        VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})""",
                    (ts, asset, signal_score, signal_direction, price_at_signal, sources_count, detail)
                )
                sid = cur.lastrowid
            conn.commit()
            return sid
        finally:
            conn.close()

    def save_performance_accuracy(self, snapshot_id: int, window_hours: int,
                                   price_at_window: float, gradient_score: Optional[float],
                                   pct_change: Optional[float] = None) -> None:
        self._ensure_perf_tables()
        ts = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            cur.execute(
                f"""INSERT INTO performance_accuracy
                    (snapshot_id, window_hours, price_at_window, gradient_score, pct_change, evaluated_at)
                    VALUES ({ph},{ph},{ph},{ph},{ph},{ph})""",
                (snapshot_id, window_hours, price_at_window, gradient_score, pct_change, ts)
            )
            bool_true = "TRUE" if self._backend == "postgres" else "1"
            cur.execute(
                f"UPDATE performance_snapshots SET evaluated_{window_hours}h = {bool_true} WHERE id = {ph}",
                (snapshot_id,)
            )
            conn.commit()
        finally:
            conn.close()

    def load_unevaluated_snapshots(self, window_hours: int, min_age_hours: int = 48) -> list[dict]:
        self._ensure_perf_tables()
        conn = self._connect()
        try:
            cur = conn.cursor()
            bool_false = "FALSE" if self._backend == "postgres" else "0"
            cur.execute(
                f"""SELECT id, timestamp, asset, signal_score, signal_direction, price_at_signal
                    FROM performance_snapshots
                    WHERE evaluated_{window_hours}h = {bool_false}
                    ORDER BY timestamp ASC""",
            )
            return [
                {"id": r[0], "timestamp": r[1], "asset": r[2], "signal_score": r[3],
                 "signal_direction": r[4], "price_at_signal": r[5]}
                for r in cur.fetchall()
            ]
        finally:
            conn.close()

    def save_dimension_scores(self, snapshot_id: int, dimension_scores: dict,
                               config_version: str = "", regime: str = "") -> None:
        self._ensure_perf_tables()
        ts = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            cur.execute(
                f"""INSERT INTO ic_dimension_scores
                    (snapshot_id, dimension_scores, config_version, regime, timestamp)
                    VALUES ({ph},{ph},{ph},{ph},{ph})""",
                (snapshot_id, json.dumps(dimension_scores), config_version, regime, ts)
            )
            conn.commit()
        finally:
            conn.close()

    def load_accuracy_stats(self, days: int = 30) -> dict[str, Any]:
        self._ensure_perf_tables()
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            cur.execute(
                f"""SELECT pa.window_hours, pa.gradient_score, pa.pct_change,
                           ps.asset, ps.signal_direction
                    FROM performance_accuracy pa
                    JOIN performance_snapshots ps ON pa.snapshot_id = ps.id
                    WHERE ps.timestamp >= datetime('now', {ph})""",
                (f"-{days} days",)
            )
            rows = cur.fetchall()
            if not rows:
                return {"total": 0, "windows": {}}
            by_window: dict[int, list] = {}
            for wh, gs, pc, asset, direction in rows:
                by_window.setdefault(wh, []).append({"gradient_score": gs, "pct_change": pc, "asset": asset, "direction": direction})
            result = {"total": len(rows), "windows": {}}
            for wh, entries in by_window.items():
                scored = [e for e in entries if e["gradient_score"] is not None]
                avg = sum(e["gradient_score"] for e in scored) / len(scored) if scored else 0
                result["windows"][wh] = {"count": len(entries), "avg_gradient": round(avg, 4)}
            return result
        finally:
            conn.close()

    # --- API Analytics ---

    def _ensure_api_table(self):
        if "api_requests" in self._created_tables:
            return
        pk = "SERIAL PRIMARY KEY" if self._backend == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        float_type = "DOUBLE PRECISION" if self._backend == "postgres" else "REAL"
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""CREATE TABLE IF NOT EXISTS api_requests (
                id {pk},
                timestamp TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                method TEXT NOT NULL,
                user_agent TEXT,
                status_code INTEGER NOT NULL,
                duration_ms {float_type},
                client_ip TEXT DEFAULT '',
                payment_status TEXT,
                request_source TEXT DEFAULT 'unknown'
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_api_ts ON api_requests(timestamp)")
            conn.commit()
            self._created_tables.add("api_requests")
        finally:
            conn.close()

    def save_api_request(self, endpoint: str, method: str, user_agent: str,
                          status_code: int, duration_ms: float,
                          client_ip: str = "", payment_status: Optional[str] = None,
                          request_source: str = "unknown") -> None:
        self._ensure_api_table()
        ts = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            cur.execute(
                f"""INSERT INTO api_requests
                    (timestamp, endpoint, method, user_agent, status_code, duration_ms, client_ip, payment_status, request_source)
                    VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})""",
                (ts, endpoint, method, user_agent, status_code, duration_ms, client_ip, payment_status, request_source)
            )
            conn.commit()
        finally:
            conn.close()
