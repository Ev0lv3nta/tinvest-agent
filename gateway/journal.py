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
    figi          TEXT,
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

CREATE TABLE IF NOT EXISTS transcript (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    thread_id  TEXT    NOT NULL DEFAULT '',
    turn_id    TEXT    NOT NULL DEFAULT '',
    is_own     INTEGER NOT NULL DEFAULT 1,
    kind       TEXT    NOT NULL,
    title      TEXT,
    body       TEXT,
    payload    TEXT
);
CREATE INDEX IF NOT EXISTS idx_transcript_ts ON transcript(id);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    source     TEXT    NOT NULL,
    text       TEXT    NOT NULL,
    delivered  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_undelivered ON messages(delivered, id);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Намерение отправить заявку. Пишется ДО обращения к брокеру: если ответ
-- потерялся, а заявка принята, у нас остаётся ключ, по которому её можно
-- найти, вместо второй заявки с новым ключом.
CREATE TABLE IF NOT EXISTS order_intents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    request_id    TEXT    NOT NULL UNIQUE,
    instrument_id TEXT    NOT NULL,
    ticker        TEXT,
    direction     TEXT    NOT NULL,
    order_type    TEXT    NOT NULL,
    lots          INTEGER NOT NULL,
    price         REAL,
    card          TEXT,
    state         TEXT    NOT NULL DEFAULT 'pending',
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_intents_state ON order_intents(state, ts);

-- Расход модели. Приходит отдельным событием app-server; до этой таблицы
-- супервизор читал несуществующее поле и писал нули.
CREATE TABLE IF NOT EXISTS usage (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    turn_id        TEXT,
    input          INTEGER,
    cached         INTEGER,
    output         INTEGER,
    reasoning      INTEGER,
    total          INTEGER,
    context_used   INTEGER,
    context_window INTEGER
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);

-- Условия пробуждения. За рынком следит код и будит агента по событию,
-- вместо того чтобы агент просыпался по таймеру и всё перечитывал.
CREATE TABLE IF NOT EXISTS watches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts    REAL    NOT NULL,
    instrument_id TEXT    NOT NULL,
    ticker        TEXT,
    kind          TEXT    NOT NULL,
    threshold     REAL    NOT NULL,
    note          TEXT    NOT NULL,
    expires_ts    REAL,
    fired_ts      REAL,
    fired_value   REAL,
    cancelled     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_watches_active ON watches(fired_ts, cancelled, expires_ts);
"""

# Столбцы, добавленные после первой схемы. В SQLite нет ADD COLUMN IF NOT
# EXISTS, поэтому пробуем и глотаем только «уже существует»: остальные
# OperationalError (блокировка, повреждение) должны быть видны.
MIGRATIONS = [
    ("orders", "figi", "TEXT"),
    ("orders", "request_id", "TEXT"),
    ("orders", "requested_price", "REAL"),
    ("orders", "card", "TEXT"),
    ("orders", "closed_ts", "REAL"),
    ("watches", "base_price", "REAL"),
]


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
        for table, column, kind in MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
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
        "INSERT INTO orders (ts, order_id, request_id, instrument_id, figi, ticker,"
        " direction, order_type, lots, lots_executed, price, requested_price, total,"
        " status, rationale, card, raw)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            time.time(),
            order.get("order_id", ""),
            order.get("request_id", ""),
            order.get("instrument_id", ""),
            order.get("figi"),
            order.get("ticker"),
            order.get("direction", ""),
            order.get("order_type", ""),
            int(order.get("lots") or 0),
            order.get("lots_executed"),
            order.get("price"),
            # Неисполненная лимитка приходит с executedOrderPrice = 0, поэтому
            # запрошенная цена хранится отдельно — иначе в журнале и в панели
            # такая заявка выглядит сделкой по нулю.
            order.get("requested_price"),
            order.get("total"),
            order.get("status"),
            rationale,
            _dump(order.get("card")) if order.get("card") else None,
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


MAX_ACTIVE_WAKEUPS = 5


def schedule_wakeup(due_ts: float, reason: str) -> int:
    """Будильников может быть несколько.

    Раньше активным был ровно один, и новый отменял прежний. Из-за этого
    независимые поводы («проверить позицию в 15:10» и «отчёт около 14:00»)
    схлопывались в один ранний, а агент просыпался чаще, чем нужно.
    """
    conn = connect()
    active = conn.execute(
        "SELECT COUNT(*) AS n FROM wakeups WHERE fired_ts IS NULL AND cancelled = 0"
    ).fetchone()["n"]
    if active >= MAX_ACTIVE_WAKEUPS:
        raise ValueError(
            f"уже {active} активных будильников при лимите {MAX_ACTIVE_WAKEUPS}; "
            f"снимай ненужные через cancel_wakeup или следи ценой через watch"
        )
    cursor = conn.execute(
        "INSERT INTO wakeups (created_ts, due_ts, reason) VALUES (?, ?, ?)",
        (time.time(), due_ts, reason),
    )
    conn.commit()
    return int(cursor.lastrowid)


def cancel_wakeup(wakeup_id: int) -> bool:
    conn = connect()
    cursor = conn.execute(
        "UPDATE wakeups SET cancelled = 1 WHERE id = ? AND fired_ts IS NULL"
        " AND cancelled = 0",
        (wakeup_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def active_wakeups() -> list[sqlite3.Row]:
    conn = connect()
    return conn.execute(
        "SELECT * FROM wakeups WHERE fired_ts IS NULL AND cancelled = 0 ORDER BY due_ts"
    ).fetchall()


def pending_wakeup() -> Optional[sqlite3.Row]:
    """Ближайший несработавший будильник."""
    rows = active_wakeups()
    return rows[0] if rows else None


def due_wakeups(now: Optional[float] = None) -> list[sqlite3.Row]:
    moment = time.time() if now is None else now
    conn = connect()
    return conn.execute(
        "SELECT * FROM wakeups WHERE fired_ts IS NULL AND cancelled = 0"
        " AND due_ts <= ? ORDER BY due_ts",
        (moment,),
    ).fetchall()


def postpone_wakeup(wakeup_id: int, seconds: float) -> None:
    """Доставка не удалась — не гасим повод, а сдвигаем его."""
    conn = connect()
    conn.execute(
        "UPDATE wakeups SET due_ts = ? WHERE id = ?", (time.time() + seconds, wakeup_id)
    )
    conn.commit()


def mark_wakeup_fired(wakeup_id: int) -> None:
    conn = connect()
    conn.execute("UPDATE wakeups SET fired_ts = ? WHERE id = ?", (time.time(), wakeup_id))
    conn.commit()


def log_transcript(
    kind: str,
    title: str = "",
    body: str = "",
    payload: Any = None,
    thread_id: str = "",
    turn_id: str = "",
    is_own: bool = True,
) -> None:
    """Лента сессии для чтения человеком: всё, что происходило по порядку."""
    conn = connect()
    conn.execute(
        "INSERT INTO transcript (ts, thread_id, turn_id, is_own, kind, title, body, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            time.time(),
            thread_id,
            turn_id,
            1 if is_own else 0,
            kind,
            title[:400] if title else None,
            body[:20000] if body else None,
            _dump(payload, 8000) if payload is not None else None,
        ),
    )
    conn.commit()


def enqueue_message(text: str, source: str = "operator") -> int:
    """Сообщение агенту в обход будильников: расписание не трогаем."""
    conn = connect()
    cursor = conn.execute(
        "INSERT INTO messages (ts, source, text) VALUES (?, ?, ?)",
        (time.time(), source, text),
    )
    conn.commit()
    return int(cursor.lastrowid)


def peek_messages(limit: int = 10) -> list[sqlite3.Row]:
    """Недоставленные сообщения, БЕЗ пометки о доставке.

    Пометка ставится отдельно и только после того, как агент событие принял.
    Раньше строка помечалась доставленной при чтении, и сообщение оператора,
    пришедшееся на обрыв канала, исчезало навсегда — в журнале первого
    прогона такой случай есть.
    """
    conn = connect()
    return conn.execute(
        "SELECT * FROM messages WHERE delivered = 0 ORDER BY id LIMIT ?", (limit,)
    ).fetchall()


def mark_message_delivered(message_id: int) -> None:
    conn = connect()
    conn.execute("UPDATE messages SET delivered = 1 WHERE id = ?", (message_id,))
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


# --- день: опора для дневных лимитов --------------------------------------

MSK_OFFSET = 3 * 3600


def day_start_ts(now: Optional[float] = None) -> float:
    """Полночь по Москве. Дневные лимиты считаются от неё, а не от суток UTC."""
    moment = time.time() if now is None else now
    return ((moment + MSK_OFFSET) // 86400) * 86400 - MSK_OFFSET


def day_open_equity() -> Optional[float]:
    """Стоимость портфеля на начало торгового дня.

    Берётся первый срез после полуночи; если сегодня срезов ещё не было —
    последний вчерашний. Если нет и его, дневной лимит не применяется:
    лучше не ограничивать, чем ограничивать по выдуманному числу.
    """
    conn = connect()
    start = day_start_ts()
    row = conn.execute(
        "SELECT total FROM snapshots WHERE ts >= ? ORDER BY ts LIMIT 1", (start,)
    ).fetchone()
    if row:
        return float(row["total"])
    row = conn.execute(
        "SELECT total FROM snapshots WHERE ts < ? ORDER BY ts DESC LIMIT 1", (start,)
    ).fetchone()
    return float(row["total"]) if row else None


def day_result(total: float) -> Optional[float]:
    opening = day_open_equity()
    return None if opening is None else total - opening


def entries_today() -> int:
    """Сколько раз сегодня открывали или наращивали позицию."""
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE ts >= ? AND direction = 'buy'"
        " AND COALESCE(lots_executed, 0) > 0",
        (day_start_ts(),),
    ).fetchone()
    return int(row["n"])


def last_losing_exit(instrument_id: str) -> Optional[float]:
    """Время последнего сегодняшнего выхода из бумаги в минус.

    Сравнивается средневзвешенная цена сегодняшних покупок с ценой продажи:
    точного посделочного матчинга здесь не нужно, нужен факт «вышел хуже,
    чем заходил».
    """
    conn = connect()
    start = day_start_ts()
    buys = conn.execute(
        "SELECT SUM(price * lots_executed) AS amount, SUM(lots_executed) AS lots"
        " FROM orders WHERE ts >= ? AND instrument_id = ? AND direction = 'buy'"
        " AND COALESCE(lots_executed, 0) > 0 AND COALESCE(price, 0) > 0",
        (start, instrument_id),
    ).fetchone()
    if not buys or not buys["lots"]:
        return None
    average = float(buys["amount"]) / float(buys["lots"])
    row = conn.execute(
        "SELECT ts FROM orders WHERE ts >= ? AND instrument_id = ? AND direction = 'sell'"
        " AND COALESCE(lots_executed, 0) > 0 AND COALESCE(price, 0) > 0 AND price < ?"
        " ORDER BY ts DESC LIMIT 1",
        (start, instrument_id, average),
    ).fetchone()
    return float(row["ts"]) if row else None


# --- намерения по заявкам -------------------------------------------------


def create_intent(intent: dict) -> None:
    """Записать намерение до сети. Ключ идемпотентности — request_id."""
    conn = connect()
    conn.execute(
        "INSERT INTO order_intents (ts, request_id, instrument_id, ticker, direction,"
        " order_type, lots, price, card, state)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
        (
            time.time(),
            intent["request_id"],
            intent["instrument_id"],
            intent.get("ticker", ""),
            intent["direction"],
            intent["order_type"],
            int(intent["lots"]),
            intent.get("price"),
            _dump(intent.get("card")) if intent.get("card") else None,
        ),
    )
    conn.commit()


def set_intent_state(request_id: str, state: str, detail: str = "") -> None:
    conn = connect()
    conn.execute(
        "UPDATE order_intents SET state = ?, detail = ? WHERE request_id = ?",
        (state, detail[:500] if detail else None, request_id),
    )
    conn.commit()


def ambiguous_intents(instrument_id: str = "") -> list[sqlite3.Row]:
    """Намерения, судьба которых неизвестна: ответ брокера не дошёл."""
    conn = connect()
    if instrument_id:
        return conn.execute(
            "SELECT * FROM order_intents WHERE state = 'ambiguous'"
            " AND instrument_id = ? ORDER BY ts",
            (instrument_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM order_intents WHERE state = 'ambiguous' ORDER BY ts"
    ).fetchall()


def update_order_status(order_id: str, status: str, lots_executed: int, price: float) -> None:
    """Довести запись о заявке до фактического состояния.

    Раньше сохранялся только первый ответ брокера: заявка, исполнившаяся
    позже, навсегда оставалась NEW с нулём исполненных лотов, а отмена не
    записывалась вовсе.
    """
    conn = connect()
    conn.execute(
        "UPDATE orders SET status = ?, lots_executed = ?,"
        " price = CASE WHEN ? > 0 THEN ? ELSE price END,"
        " closed_ts = CASE WHEN ? THEN ? ELSE closed_ts END"
        " WHERE order_id = ?",
        (
            status,
            lots_executed,
            price,
            price,
            1 if status and ("FILL" in status or "CANCELLED" in status or "REJECTED" in status) else 0,
            time.time(),
            order_id,
        ),
    )
    conn.commit()


def open_orders() -> list[sqlite3.Row]:
    conn = connect()
    return conn.execute(
        "SELECT * FROM orders WHERE closed_ts IS NULL AND ts > ? ORDER BY ts",
        (time.time() - 7 * 86400,),
    ).fetchall()


# --- расход модели --------------------------------------------------------


def log_usage(turn_id: str, usage: dict) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO usage (ts, turn_id, input, cached, output, reasoning, total,"
        " context_used, context_window) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            time.time(),
            turn_id,
            usage.get("input"),
            usage.get("cached"),
            usage.get("output"),
            usage.get("reasoning"),
            usage.get("total"),
            usage.get("context_used"),
            usage.get("context_window"),
        ),
    )
    conn.commit()


def usage_since(seconds: float) -> dict:
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) AS turns, SUM(input) AS input, SUM(output) AS output,"
        " SUM(total) AS total FROM usage WHERE ts > ?",
        (time.time() - seconds,),
    ).fetchone()
    last = conn.execute(
        "SELECT context_used, context_window FROM usage"
        " WHERE context_used IS NOT NULL ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return {
        "turns": int(row["turns"] or 0),
        "input": int(row["input"] or 0),
        "output": int(row["output"] or 0),
        "total": int(row["total"] or 0),
        "context_used": int(last["context_used"]) if last and last["context_used"] else 0,
        "context_window": int(last["context_window"]) if last and last["context_window"] else 0,
    }


# --- наблюдатели ----------------------------------------------------------

MAX_ACTIVE_WATCHES = 20
WATCH_KINDS = ("price_above", "price_below", "pct_move")


def add_watch(
    instrument_id: str,
    ticker: str,
    kind: str,
    threshold: float,
    note: str,
    expires_ts: float,
    base_price: Optional[float] = None,
) -> int:
    if kind not in WATCH_KINDS:
        raise ValueError(f"условие должно быть одним из {', '.join(WATCH_KINDS)}")
    conn = connect()
    active = conn.execute(
        "SELECT COUNT(*) AS n FROM watches WHERE fired_ts IS NULL AND cancelled = 0"
    ).fetchone()["n"]
    if active >= MAX_ACTIVE_WATCHES:
        raise ValueError(f"уже {active} наблюдателей при лимите {MAX_ACTIVE_WATCHES}")
    cursor = conn.execute(
        "INSERT INTO watches (created_ts, instrument_id, ticker, kind, threshold, note,"
        " expires_ts, base_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            time.time(), instrument_id, ticker, kind, float(threshold), note,
            expires_ts, base_price,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def active_watches() -> list[sqlite3.Row]:
    conn = connect()
    return conn.execute(
        "SELECT * FROM watches WHERE fired_ts IS NULL AND cancelled = 0"
        " AND (expires_ts IS NULL OR expires_ts > ?) ORDER BY id",
        (time.time(),),
    ).fetchall()


def cancel_watch(watch_id: int) -> bool:
    conn = connect()
    cursor = conn.execute(
        "UPDATE watches SET cancelled = 1 WHERE id = ? AND fired_ts IS NULL"
        " AND cancelled = 0",
        (watch_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_watch_fired(watch_id: int, value: float) -> None:
    conn = connect()
    conn.execute(
        "UPDATE watches SET fired_ts = ?, fired_value = ? WHERE id = ?",
        (time.time(), value, watch_id),
    )
    conn.commit()


def expire_watches() -> int:
    conn = connect()
    cursor = conn.execute(
        "UPDATE watches SET cancelled = 1 WHERE fired_ts IS NULL AND cancelled = 0"
        " AND expires_ts IS NOT NULL AND expires_ts <= ?",
        (time.time(),),
    )
    conn.commit()
    return cursor.rowcount
