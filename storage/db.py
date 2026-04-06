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

    def _ago(self, days: int) -> str:
        """SQL expression for 'days ago' — works on both SQLite and Postgres.
        Timestamps are stored as ISO text, so Postgres needs CAST for comparison."""
        if self._backend == "postgres":
            return f"(NOW() - INTERVAL '{days} days')::text"
        return f"datetime('now', '-{days} days')"

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
            cur.execute(
                f"SELECT data_json, timestamp FROM {table} WHERE timestamp >= {self._ago(days)} ORDER BY id DESC"
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
                    WHERE ps.timestamp >= {self._ago(days)}"""
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

    def _ensure_error_table(self):
        if "error_events" in self._created_tables:
            return
        pk = "SERIAL PRIMARY KEY" if self._backend == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""CREATE TABLE IF NOT EXISTS error_events (
                id {pk},
                timestamp TEXT NOT NULL,
                error_type TEXT NOT NULL,
                source TEXT NOT NULL,
                message TEXT NOT NULL
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_error_ts ON error_events(timestamp)")
            conn.commit()
            self._created_tables.add("error_events")
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

    def save_error_event(self, error_type: str, source: str, message: str) -> None:
        self._ensure_error_table()
        ts = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._ph()
            cur.execute(
                f"INSERT INTO error_events (timestamp, error_type, source, message) VALUES ({ph},{ph},{ph},{ph})",
                (ts, error_type, source, message)
            )
            conn.commit()
        finally:
            conn.close()

    def load_error_summary(self, days: int = 7) -> dict[str, Any]:
        self._ensure_error_table()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT error_type, source, message, timestamp FROM error_events WHERE timestamp >= {self._ago(days)} ORDER BY timestamp DESC"
            )
            rows = cur.fetchall()
            by_type: dict[str, int] = {}
            recent: list[dict] = []
            for r in rows:
                by_type[r[0]] = by_type.get(r[0], 0) + 1
                if len(recent) < 20:
                    recent.append({"type": r[0], "source": r[1], "message": r[2], "timestamp": r[3]})
            return {"total_errors": len(rows), "by_type": by_type, "recent": recent}
        finally:
            conn.close()

    def load_api_analytics(self, days: int = 7) -> dict[str, Any]:
        """Aggregate API request analytics for the given window."""
        self._ensure_api_table()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT endpoint, method, user_agent, status_code, duration_ms, client_ip, payment_status, request_source, timestamp FROM api_requests WHERE timestamp >= {self._ago(days)}"
            )
            rows = cur.fetchall()
            if not rows:
                return {"total_requests": 0, "unique_ips": 0, "avg_duration_ms": 0,
                        "by_endpoint": {}, "by_client_type": {}, "requests_per_day": {},
                        "by_source": {}}

            from api.middleware import classify_user_agent

            total = len(rows)
            ips = set()
            durations = []
            by_endpoint: dict[str, int] = {}
            by_client_type: dict[str, int] = {}
            by_day: dict[str, int] = {}
            by_source: dict[str, int] = {}

            for endpoint, method, ua, status, dur, ip, pay_status, req_source, ts in rows:
                ips.add(ip)
                if dur is not None:
                    durations.append(dur)
                by_endpoint[endpoint] = by_endpoint.get(endpoint, 0) + 1
                client_type = classify_user_agent(ua or "")
                by_client_type[client_type] = by_client_type.get(client_type, 0) + 1
                day = ts[:10] if ts else "unknown"
                by_day[day] = by_day.get(day, 0) + 1
                src = req_source or "unknown"
                by_source[src] = by_source.get(src, 0) + 1

            return {
                "total_requests": total,
                "unique_ips": len(ips),
                "avg_duration_ms": round(sum(durations) / len(durations), 1) if durations else 0,
                "by_endpoint": by_endpoint,
                "by_client_type": by_client_type,
                "requests_per_day": dict(sorted(by_day.items())),
                "by_source": by_source,
            }
        finally:
            conn.close()

    def load_x402_analytics(self, days: int = 30) -> dict[str, Any]:
        """Aggregate x402 payment analytics."""
        self._ensure_api_table()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT endpoint, user_agent, payment_status, duration_ms, timestamp FROM api_requests WHERE payment_status IS NOT NULL AND timestamp >= {self._ago(days)}"
            )
            rows = cur.fetchall()

            total_paid = 0
            total_402 = 0
            total_failed = 0
            by_endpoint: dict[str, int] = {}
            by_client_type: dict[str, int] = {}
            by_day: dict[str, int] = {}
            paid_durations = []
            price = float(os.getenv("SIGNAL_PRICE_USDC", "0.001"))

            from api.middleware import classify_user_agent

            for endpoint, ua, pay_status, dur, ts in rows:
                if pay_status == "paid":
                    total_paid += 1
                    by_endpoint[endpoint] = by_endpoint.get(endpoint, 0) + 1
                    ct = classify_user_agent(ua or "")
                    by_client_type[ct] = by_client_type.get(ct, 0) + 1
                    day = ts[:10] if ts else "unknown"
                    by_day[day] = by_day.get(day, 0) + 1
                    if dur is not None:
                        paid_durations.append(dur)
                elif pay_status == "payment_required":
                    total_402 += 1
                elif pay_status == "payment_failed":
                    total_failed += 1

            return {
                "total_paid_calls": total_paid,
                "total_402_challenges": total_402,
                "total_payment_failures": total_failed,
                "estimated_revenue_usdc": round(total_paid * price, 4),
                "by_endpoint": by_endpoint,
                "by_client_type": by_client_type,
                "paid_per_day": dict(sorted(by_day.items())),
                "avg_paid_latency_ms": round(sum(paid_durations) / len(paid_durations), 1) if paid_durations else 0,
            }
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Trades & P&L tracking
    # -----------------------------------------------------------------------

    def _ensure_trades_table(self):
        if "trades" in self._created_tables:
            return
        pk = "SERIAL PRIMARY KEY" if self._backend == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        float_type = "DOUBLE PRECISION" if self._backend == "postgres" else "REAL"
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""CREATE TABLE IF NOT EXISTS trades (
                id {pk},
                asset TEXT NOT NULL,
                direction TEXT NOT NULL,
                composite_score {float_type},
                entry_price {float_type} NOT NULL,
                target_price {float_type} NOT NULL,
                stop_loss {float_type} NOT NULL,
                risk_reward_ratio {float_type},
                entry_time TEXT NOT NULL,
                expiry_time TEXT,
                exit_price {float_type},
                exit_time TEXT,
                outcome TEXT DEFAULT 'open',
                pnl_pct {float_type} DEFAULT 0,
                pnl_usd {float_type} DEFAULT 0,
                position_size_usd {float_type} DEFAULT 0,
                confidence TEXT,
                regime TEXT,
                source TEXT DEFAULT 'live'
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_entry ON trades(entry_time)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_outcome ON trades(outcome)")
            conn.commit()
            self._created_tables.add("trades")
        finally:
            conn.close()

    def save_trade(self, asset: str, direction: str, composite_score: float,
                   entry_price: float, target_price: float, stop_loss: float,
                   risk_reward_ratio: float, confidence: str = "",
                   regime: str = "", position_size_usd: float = 0,
                   source: str = "live") -> int:
        """Record a new trade entry. Returns trade ID."""
        self._ensure_trades_table()
        ph = self._ph()
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""INSERT INTO trades
                (asset, direction, composite_score, entry_price, target_price,
                 stop_loss, risk_reward_ratio, entry_time, confidence, regime,
                 position_size_usd, source)
                VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})""",
                (asset, direction, composite_score, entry_price, target_price,
                 stop_loss, risk_reward_ratio, now, confidence, regime,
                 position_size_usd, source))
            conn.commit()
            trade_id = cur.lastrowid
            return trade_id
        finally:
            conn.close()

    def close_trade(self, trade_id: int, exit_price: float, outcome: str,
                    pnl_pct: float, pnl_usd: float = 0) -> None:
        """Close a trade with exit price and outcome."""
        self._ensure_trades_table()
        ph = self._ph()
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""UPDATE trades SET
                exit_price = {ph}, exit_time = {ph}, outcome = {ph},
                pnl_pct = {ph}, pnl_usd = {ph}
                WHERE id = {ph}""",
                (exit_price, now, outcome, pnl_pct, pnl_usd, trade_id))
            conn.commit()
        finally:
            conn.close()

    def get_open_trades(self) -> list[dict]:
        """Get all open trades (not yet resolved)."""
        self._ensure_trades_table()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades WHERE outcome = 'open' ORDER BY entry_time DESC")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    def load_trades(self, days: int = 30, asset: str = None) -> list[dict]:
        """Load recent trades with optional asset filter."""
        self._ensure_trades_table()
        ph = self._ph()
        ago = self._ago(days)
        conn = self._connect()
        try:
            cur = conn.cursor()
            if asset:
                cur.execute(f"""SELECT * FROM trades
                    WHERE entry_time >= {ago} AND asset = {ph}
                    ORDER BY entry_time DESC""", (asset,))
            else:
                cur.execute(f"""SELECT * FROM trades
                    WHERE entry_time >= {ago}
                    ORDER BY entry_time DESC""")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return [dict(zip(cols, r)) for r in rows]
        finally:
            conn.close()

    def load_trade_stats(self, days: int = 30) -> dict[str, Any]:
        """Compute trade statistics for the dashboard."""
        self._ensure_trades_table()
        ago = self._ago(days)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"""SELECT * FROM trades
                WHERE entry_time >= {ago}
                ORDER BY entry_time ASC""")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            if not rows:
                return {"total_trades": 0, "open_trades": 0, "closed_trades": 0,
                        "win_rate": 0, "total_pnl_pct": 0, "total_pnl_usd": 0,
                        "by_asset": {}, "by_direction": {}, "by_day": {},
                        "equity_curve": [], "recent_trades": []}

            closed = [r for r in rows if r["outcome"] != "open"]
            open_trades = [r for r in rows if r["outcome"] == "open"]
            wins = [r for r in closed if (r.get("pnl_pct") or 0) > 0]
            losses = [r for r in closed if (r.get("pnl_pct") or 0) <= 0]

            total_pnl_pct = sum(r.get("pnl_pct", 0) or 0 for r in closed)
            total_pnl_usd = sum(r.get("pnl_usd", 0) or 0 for r in closed)
            win_rate = len(wins) / len(closed) if closed else 0

            avg_win = sum(r.get("pnl_pct", 0) or 0 for r in wins) / len(wins) if wins else 0
            avg_loss = sum(abs(r.get("pnl_pct", 0) or 0) for r in losses) / len(losses) if losses else 0
            profit_factor = (sum(r.get("pnl_pct", 0) or 0 for r in wins) /
                            abs(sum(r.get("pnl_pct", 0) or 0 for r in losses))
                            if losses and sum(r.get("pnl_pct", 0) or 0 for r in losses) != 0
                            else 0)

            # By asset
            by_asset = {}
            for r in closed:
                a = r["asset"]
                if a not in by_asset:
                    by_asset[a] = {"trades": 0, "wins": 0, "pnl_pct": 0, "pnl_usd": 0}
                by_asset[a]["trades"] += 1
                by_asset[a]["pnl_pct"] += r.get("pnl_pct", 0) or 0
                by_asset[a]["pnl_usd"] += r.get("pnl_usd", 0) or 0
                if (r.get("pnl_pct") or 0) > 0:
                    by_asset[a]["wins"] += 1
            for a in by_asset:
                by_asset[a]["win_rate"] = round(by_asset[a]["wins"] / by_asset[a]["trades"], 3) if by_asset[a]["trades"] > 0 else 0
                by_asset[a]["pnl_pct"] = round(by_asset[a]["pnl_pct"], 2)
                by_asset[a]["pnl_usd"] = round(by_asset[a]["pnl_usd"], 2)

            # By direction
            by_direction = {}
            for r in closed:
                d = r["direction"]
                if d not in by_direction:
                    by_direction[d] = {"trades": 0, "wins": 0, "pnl_pct": 0}
                by_direction[d]["trades"] += 1
                by_direction[d]["pnl_pct"] += r.get("pnl_pct", 0) or 0
                if (r.get("pnl_pct") or 0) > 0:
                    by_direction[d]["wins"] += 1
            for d in by_direction:
                by_direction[d]["win_rate"] = round(by_direction[d]["wins"] / by_direction[d]["trades"], 3) if by_direction[d]["trades"] > 0 else 0

            # By day
            by_day = {}
            for r in closed:
                day = (r.get("exit_time") or r.get("entry_time", ""))[:10]
                if day not in by_day:
                    by_day[day] = {"trades": 0, "pnl_pct": 0, "pnl_usd": 0}
                by_day[day]["trades"] += 1
                by_day[day]["pnl_pct"] += r.get("pnl_pct", 0) or 0
                by_day[day]["pnl_usd"] += r.get("pnl_usd", 0) or 0
            for day in by_day:
                by_day[day]["pnl_pct"] = round(by_day[day]["pnl_pct"], 2)
                by_day[day]["pnl_usd"] = round(by_day[day]["pnl_usd"], 2)

            # Equity curve
            equity = []
            running = 0.0
            for r in closed:
                running += r.get("pnl_pct", 0) or 0
                equity.append({"date": (r.get("exit_time") or r.get("entry_time", ""))[:10],
                               "cumulative_pnl_pct": round(running, 2)})

            # Outcome breakdown
            outcomes = {}
            for r in closed:
                o = r.get("outcome", "unknown")
                outcomes[o] = outcomes.get(o, 0) + 1

            stats = {
                "total_trades": len(rows),
                "open_trades": len(open_trades),
                "closed_trades": len(closed),
                "win_rate": round(win_rate, 3),
                "total_pnl_pct": round(total_pnl_pct, 2),
                "total_pnl_usd": round(total_pnl_usd, 2),
                "avg_win_pct": round(avg_win, 2),
                "avg_loss_pct": round(avg_loss, 2),
                "profit_factor": round(profit_factor, 2),
                "outcomes": outcomes,
                "by_asset": dict(sorted(by_asset.items())),
                "by_direction": by_direction,
                "by_day": dict(sorted(by_day.items())),
                "equity_curve": equity,
                "recent_trades": [
                    {k: v for k, v in r.items() if k != "id"}
                    for r in rows[:20]
                ],
            }

            # Compute risk metrics from closed trades
            try:
                from tools.trade_simulator import compute_risk_metrics
                trade_dicts = [
                    {"pnl_pct": t["pnl_pct"], "date": (t.get("exit_time") or "")[:10],
                     "regime": t.get("regime", "")}
                    for t in rows if t.get("outcome") and t["outcome"] != "open"
                ]
                if trade_dicts:
                    risk_metrics = compute_risk_metrics(trade_dicts, monte_carlo_n=500)
                    stats.update(risk_metrics)
                else:
                    stats.update({
                        "sharpe_ratio": 0, "sortino_ratio": 0, "calmar_ratio": 0,
                        "max_dd_duration_days": 0,
                        "monte_carlo": {"p_value": 1.0, "median_pnl": 0, "pnl_5th": 0, "pnl_95th": 0},
                        "regime_split": {},
                    })
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Risk metrics computation error: {e}")
                stats.update({
                    "sharpe_ratio": 0, "sortino_ratio": 0, "calmar_ratio": 0,
                    "max_dd_duration_days": 0,
                    "monte_carlo": {"p_value": 1.0, "median_pnl": 0, "pnl_5th": 0, "pnl_95th": 0},
                    "regime_split": {},
                })

            return stats
        finally:
            conn.close()
