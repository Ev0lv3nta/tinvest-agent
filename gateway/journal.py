"""Журнал в SQLite: то, что агент не может ни пропустить, ни переписать задним числом.

Пишется автоматически на каждом вызове инструмента. Отдельно от заметок
самого агента: заметки — это его интерпретация, журнал — то, что было.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import config

_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    tool        TEXT    NOT NULL,
    arguments   TEXT    NOT NULL,
    ok          INTEGER NOT NULL,
    result      TEXT,
    error       TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls(ts);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    order_id      TEXT    NOT NULL,
    instrument_id TEXT    NOT NULL,
    ticker        TEXT,
    direction     TEXT    NOT NULL,
    order_type    TEXT    NOT NULL,
    lots          INTEGER NOT NULL,
    lots_executed INTEGER,
    price         REAL,
    total         REAL,
    status        TEXT,
    rationale     TEXT,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    total     REAL    NOT NULL,
    cash      REAL    NOT NULL,
    positions TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);

CREATE TABLE IF NOT EXISTS wakeups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts  REAL    NOT NULL,
    due_ts      REAL    NOT NULL,
    reason      TEXT    NOT NULL,
    fired_ts    REAL,
    cancelled   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wakeups_due ON wakeups(due_ts, fired_ts, cancelled);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    kind    TEXT    NOT NULL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    """Соединение на поток: SQLite не любит шаринг между потоками."""
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
        _LOCAL.conn = conn
    return conn


def _dump(value: Any, limit: int = 20000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit]


def log_tool_call(
    tool: str,
    arguments: dict,
    ok: bool,
    result: Any = None,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO tool_calls (ts, tool, arguments, ok, result, error, duration_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            time.time(),
            tool,
            _dump(arguments),
            1 if ok else 0,
            _dump(result) if result is not None else None,
            error,
            duration_ms,
        ),
    )
    conn.commit()


def log_order(order: dict, rationale: str = "") -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO orders (ts, order_id, instrument_id, ticker, direction, order_type,"
        " lots, lots_executed, price, total, status, rationale, raw)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            time.time(),
            order.get("order_id", ""),
            order.get("instrument_id", ""),
            order.get("ticker"),
            order.get("direction", ""),
            order.get("order_type", ""),
            int(order.get("lots") or 0),
            order.get("lots_executed"),
            order.get("price"),
            order.get("total"),
            order.get("status"),
            rationale,
            _dump(order.get("raw")),
        ),
    )
    conn.commit()


def log_snapshot(total: float, cash: float, positions: list) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO snapshots (ts, total, cash, positions) VALUES (?, ?, ?, ?)",
        (time.time(), total, cash, _dump(positions)),
    )
    conn.commit()


def log_event(kind: str, payload: Any = None) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)",
        (time.time(), kind, _dump(payload) if payload is not None else None),
    )
    conn.commit()


def orders_last_hour() -> int:
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE ts > ?", (time.time() - 3600,)
    ).fetchone()
    return int(row["n"])


def schedule_wakeup(due_ts: float, reason: str) -> int:
    """Новый будильник отменяет прежние незакрытые: активный ровно один."""
    conn = connect()
    conn.execute(
        "UPDATE wakeups SET cancelled = 1 WHERE fired_ts IS NULL AND cancelled = 0"
    )
    cursor = conn.execute(
        "INSERT INTO wakeups (created_ts, due_ts, reason) VALUES (?, ?, ?)",
        (time.time(), due_ts, reason),
    )
    conn.commit()
    return int(cursor.lastrowid)


def pending_wakeup() -> Optional[sqlite3.Row]:
    conn = connect()
    return conn.execute(
        "SELECT * FROM wakeups WHERE fired_ts IS NULL AND cancelled = 0"
        " ORDER BY due_ts LIMIT 1"
    ).fetchone()


def mark_wakeup_fired(wakeup_id: int) -> None:
    conn = connect()
    conn.execute("UPDATE wakeups SET fired_ts = ? WHERE id = ?", (time.time(), wakeup_id))
    conn.commit()


def kv_get(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = connect()
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
