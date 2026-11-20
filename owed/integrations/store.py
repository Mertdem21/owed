"""Storage for cases and the audit trail.

SingleStore when SINGLESTORE_URL is set (and the `singlestoredb` SDK is installed), otherwise a local SQLite file.
Both backends use the same two tables, so the demo can show a plain SQL query over the audit trail.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any

from ..config import RUNTIME, settings
from ..models import AuditEvent, Case

log = logging.getLogger("owed.store")

_SQLITE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, case_id TEXT, ts TEXT, step TEXT,
        provider TEXT, summary TEXT, data TEXT, duration_ms INTEGER)""",
    """CREATE TABLE IF NOT EXISTS cases (
        id TEXT PRIMARY KEY, run_id TEXT, status TEXT, kind TEXT, merchant TEXT,
        amount_recovered REAL, payload TEXT, updated_at TEXT)""",
]
_S2_SCHEMA = [
    """CREATE ROWSTORE TABLE IF NOT EXISTS audit_events (
        id BIGINT AUTO_INCREMENT PRIMARY KEY, run_id VARCHAR(64), case_id VARCHAR(64), ts VARCHAR(40),
        step VARCHAR(64), provider VARCHAR(64), summary TEXT, data LONGTEXT, duration_ms INT)""",
    """CREATE ROWSTORE TABLE IF NOT EXISTS cases (
        id VARCHAR(64) PRIMARY KEY, run_id VARCHAR(64), status VARCHAR(32), kind VARCHAR(32),
        merchant VARCHAR(128), amount_recovered DOUBLE, payload LONGTEXT, updated_at VARCHAR(40))""",
]


def _connect_s2(url: str) -> Any:
    """Connect to SingleStore; create the database named in the URL if it does not exist yet."""
    import singlestoredb as s2  # type: ignore
    from urllib.parse import urlsplit, urlunsplit

    try:
        return s2.connect(url)
    except Exception as exc:  # noqa: BLE001
        parts = urlsplit(url)
        db = parts.path.strip("/")
        if not db or "unknown database" not in str(exc).lower():
            raise
        root = s2.connect(urlunsplit((parts.scheme, parts.netloc, "/", parts.query, parts.fragment)))
        try:
            cur = root.cursor()
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db}`")
            root.commit()
        finally:
            root.close()
        log.warning("Created SingleStore database %s", db)
        return s2.connect(url)


class Store:
    def __init__(self) -> None:
        self.backend = "sqlite"
        self._lock = threading.Lock()
        self._conn: Any = None
        self._ph = "?"
        self.error = ""
        if settings.singlestore_url:
            try:
                self._conn = _connect_s2(settings.singlestore_url)
                self.backend = "singlestore"
                self._ph = "%s"
                self._exec_many(_S2_SCHEMA)
            except Exception as exc:  # noqa: BLE001 - any driver/connection error -> local fallback
                self.error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("SingleStore unavailable, using SQLite: %s", self.error)
                self._conn = None
                self.backend = "sqlite"
                self._ph = "?"
        if self._conn is None:
            self._conn = sqlite3.connect(RUNTIME / "owed.db", check_same_thread=False)
            self._exec_many(_SQLITE_SCHEMA)

    # -- low level -------------------------------------------------------------------------
    def _exec_many(self, statements: list[str]) -> None:
        with self._lock:
            cur = self._conn.cursor()
            for s in statements:
                cur.execute(s)
            self._conn.commit()

    def _execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        sql = sql.replace("?", self._ph)
        with self._lock:
            for attempt in (1, 2):
                try:
                    cur = self._conn.cursor()
                    cur.execute(sql, params)
                    rows = cur.fetchall() if cur.description else []
                    self._conn.commit()
                    return list(rows)
                except Exception as exc:  # noqa: BLE001
                    # A long-running server can lose its SingleStore connection: reconnect once and retry.
                    if self.backend != "singlestore" or attempt == 2:
                        raise
                    log.warning("SingleStore query failed (%s), reconnecting", exc)
                    try:
                        self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._conn = _connect_s2(settings.singlestore_url)
            return []

    # -- api -------------------------------------------------------------------------------
    def add_event(self, e: AuditEvent) -> None:
        self._execute(
            "INSERT INTO audit_events (run_id, case_id, ts, step, provider, summary, data, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (e.run_id, e.case_id or "", e.ts.isoformat(), e.step, e.provider, e.summary,
             json.dumps(e.data, default=str), e.duration_ms or 0),
        )

    def save_case(self, run_id: str, c: Case) -> None:
        payload = c.model_dump_json()
        params = (c.id, run_id, c.status, c.opportunity.kind, c.opportunity.merchant, c.amount_recovered,
                  payload, c.updated_at.isoformat())
        if self.backend == "singlestore":
            self._execute(
                "INSERT INTO cases (id, run_id, status, kind, merchant, amount_recovered, payload, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON DUPLICATE KEY UPDATE run_id=VALUES(run_id), "
                "status=VALUES(status), amount_recovered=VALUES(amount_recovered), payload=VALUES(payload), "
                "updated_at=VALUES(updated_at)", params)
        else:
            self._execute(
                "INSERT OR REPLACE INTO cases (id, run_id, status, kind, merchant, amount_recovered, payload, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", params)

    def cases(self) -> list[Case]:
        rows = self._execute("SELECT payload FROM cases ORDER BY updated_at")
        return [Case.model_validate_json(r[0]) for r in rows]

    def case(self, case_id: str) -> Case | None:
        rows = self._execute("SELECT payload FROM cases WHERE id = ?", (case_id,))
        return Case.model_validate_json(rows[0][0]) if rows else None

    def events(self, case_id: str | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT run_id, case_id, ts, step, provider, summary, data, duration_ms FROM audit_events"
        where, params = [], []
        if case_id:
            where.append("case_id = ?")
            params.append(case_id)
        if run_id:
            where.append("run_id = ?")
            params.append(run_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        out = []
        for r in self._execute(sql, tuple(params)):
            out.append({"run_id": r[0], "case_id": r[1] or None, "ts": r[2], "step": r[3], "provider": r[4],
                        "summary": r[5], "data": json.loads(r[6] or "{}"), "duration_ms": r[7]})
        return out

    def latest_run_id(self) -> str | None:
        rows = self._execute("SELECT run_id FROM audit_events WHERE step = 'start' ORDER BY id DESC LIMIT 1")
        return rows[0][0] if rows else None

    def query(self, sql: str) -> list[tuple]:
        """Read-only SQL for the demo (SELECT only)."""
        if not sql.strip().lower().startswith("select"):
            raise ValueError("only SELECT is allowed")
        return self._execute(sql)

    def reset(self) -> None:
        self._execute("DELETE FROM audit_events")
        self._execute("DELETE FROM cases")


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def smoke() -> str:
    if not settings.singlestore_url:
        return "SKIP: SINGLESTORE_URL not set (SQLite fallback works)"
    s = Store()
    if s.backend != "singlestore":
        return f"FAIL: could not connect - {s.error}"
    rows = s._execute("SELECT COUNT(*) FROM audit_events")
    ver = s._execute("SELECT @@memsql_version")
    return f"OK: SingleStore {ver[0][0] if ver else ''}, database ready, audit_events has {rows[0][0]} rows"
