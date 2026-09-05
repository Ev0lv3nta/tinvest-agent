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

-- Реестр сетапов. Торговать можно только то, что зарегистрировано ДО сделки
-- и измерено числами. Раньше «плейбук» и «базовая ставка» были двумя
-- непустыми строками в заявке: подходило и несуществующее имя, и текст
-- «выглядит перспективно» вместо статистики.
CREATE TABLE IF NOT EXISTS playbooks (
    name         TEXT    PRIMARY KEY,
    created_ts   REAL    NOT NULL,
    updated_ts   REAL    NOT NULL,
    entry        TEXT    NOT NULL,
    invalidation TEXT    NOT NULL,
    measured_on  TEXT    NOT NULL,
    trades       INTEGER NOT NULL,
    wins         INTEGER NOT NULL,
    avg_r        REAL    NOT NULL,
    retired      INTEGER NOT NULL DEFAULT 0,
    retired_why  TEXT,
    -- Откуда взялись числа: 'evaluator' — посчитал код по истории одним и
    -- тем же прогоном; 'manual' — записал агент со своих слов. Второе
    -- остаётся на проверке навсегда: слова не становятся измерением от
    -- того, что их записали в таблицу.
    source       TEXT NOT NULL DEFAULT 'manual',
    evidence     TEXT
);
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
    ("order_intents", "order_id", "TEXT"),
    ("playbooks", "source", "TEXT NOT NULL DEFAULT 'manual'"),
    ("playbooks", "evidence", "TEXT"),
    ("watches", "base_price", "REAL"),
    ("watches", "url", "TEXT"),
    ("watches", "content_hash", "TEXT"),
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
    """Попытки, а не только записанные ответы.

    Считаются намерения: таймаут, отказ брокера и оборванный запрос — тоже
    обращения к бирже, и от зацикливания защищать надо именно от них.
    """
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM order_intents WHERE ts > ?", (time.time() - 3600,)
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


DAY_BASE_KEY = "day_base"


def day_open_equity() -> Optional[float]:
    """Стоимость портфеля, от которой считается результат дня.

    База — последний срез ПРЕДЫДУЩЕГО дня, а не первый срез сегодняшнего.
    Разница в том, попадает ли ночной разрыв в дневной результат. Раньше не
    попадал: позиция уезжала за ночь на пять тысяч вниз, первый утренний
    срез записывал это как новую точку отсчёта, и дневной стоп начинал день
    с чистого листа — ровно после самого дорогого события.

    Зафиксированная база не пересчитывается до конца дня: иначе её сдвигал
    бы любой следующий срез. Меняет её только внешний поток денег.
    """
    conn = connect()
    start = day_start_ts()
    stored = kv_get(DAY_BASE_KEY, "")
    if stored:
        try:
            saved = json.loads(stored)
            if float(saved.get("day", 0)) == start:
                return float(saved["total"])
        except (ValueError, TypeError, KeyError):
            pass

    row = conn.execute(
        "SELECT total FROM snapshots WHERE ts < ? ORDER BY ts DESC LIMIT 1", (start,)
    ).fetchone()
    if row is None:
        # Первый день прогона: предыдущего дня просто нет.
        row = conn.execute(
            "SELECT total FROM snapshots WHERE ts >= ? ORDER BY ts LIMIT 1", (start,)
        ).fetchone()
    if row is None:
        return None
    total = float(row["total"])
    kv_set(DAY_BASE_KEY, json.dumps({"day": start, "total": total}))
    return total


def adjust_day_base(delta: float) -> None:
    """Внешний поток денег не является результатом торговли.

    Пополнение счёта сдвигает базу на ту же величину, иначе оно выглядело бы
    прибылью, а вывод — убытком, и дневной стоп срабатывал бы от перевода.
    """
    base = day_open_equity()
    if base is None:
        return
    kv_set(DAY_BASE_KEY, json.dumps({"day": day_start_ts(), "total": base + delta}))
    log_event("day_base_adjusted", {"delta": delta, "base": base + delta})


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


def set_intent_state(
    request_id: str, state: str, detail: str = "", order_id: str = ""
) -> None:
    """Состояние намерения. `order_id` — биржевой идентификатор заявки.

    Свой ключ и биржевой — разные вещи: по первому мы ищем заявку у брокера,
    по второму брокер её сам называет. Храним оба, чтобы не гадать, чья
    строка лежит в поле.
    """
    conn = connect()
    if order_id:
        conn.execute(
            "UPDATE order_intents SET state = ?, detail = ?, order_id = ?"
            " WHERE request_id = ?",
            (state, detail[:500] if detail else None, order_id, request_id),
        )
    else:
        conn.execute(
            "UPDATE order_intents SET state = ?, detail = ? WHERE request_id = ?",
            (state, detail[:500] if detail else None, request_id),
        )
    conn.commit()


def intent_by_order(order_id: str) -> Optional[sqlite3.Row]:
    conn = connect()
    return conn.execute(
        "SELECT * FROM order_intents WHERE order_id = ?", (order_id,)
    ).fetchone()


# Состояния намерения, в которых его судьба выяснена окончательно.
# Всё остальное — включая записанное перед сетью `pending` — блокирует
# новую заявку по той же бумаге в ту же сторону.
#
# Раньше блокировали только два состояния, и `pending` в них не входило.
# Падение процесса ровно на отправке оставляло намерение, которое не мешало
# ничему: заявка могла висеть у брокера, а шлюз разрешал вторую.
RESOLVED_STATES = ("filled", "rejected", "cancelled", "not_placed")

# Заявка отправлена и брокер её принял: пока она не дошла до конечного
# состояния, вторая заявка в ту же сторону — это удвоение позиции.
ACTIVE_STATES = ("pending", "sent", "live", "unknown", "ambiguous")


def blocking_intents(instrument_id: str = "") -> list[sqlite3.Row]:
    """Намерения, мешающие отправить новую заявку по этой бумаге.

    Список строится от обратного: блокирует всё, что не разрешено. Новое
    состояние, добавленное когда-нибудь позже, по умолчанию окажется
    блокирующим, а не тихо пропускающим заявку.
    """
    conn = connect()
    marks = ",".join("?" * len(RESOLVED_STATES))
    if instrument_id:
        return conn.execute(
            f"SELECT * FROM order_intents WHERE state NOT IN ({marks})"
            f" AND instrument_id = ? ORDER BY ts",
            (*RESOLVED_STATES, instrument_id),
        ).fetchall()
    return conn.execute(
        f"SELECT * FROM order_intents WHERE state NOT IN ({marks}) ORDER BY ts",
        RESOLVED_STATES,
    ).fetchall()


def intent_by_request(request_id: str) -> Optional[sqlite3.Row]:
    conn = connect()
    return conn.execute(
        "SELECT * FROM order_intents WHERE request_id = ?", (request_id,)
    ).fetchone()


def close_order(order_id: str, status: str) -> None:
    """Пометить заявку закрытой, не трогая уже исполненный объём.

    Частично исполненная лимитка после снятия остаётся частично исполненной:
    обнулять lots_executed нельзя, иначе сделка исчезнет из статистики.
    """
    conn = connect()
    conn.execute(
        "UPDATE orders SET status = ?, closed_ts = ? WHERE order_id = ?",
        (status, time.time(), order_id),
    )
    conn.commit()


# Конечные состояния заявки перечислены явно, а не угадываются по подстроке.
# Подстрока «FILL» совпадала с PARTIALLYFILL: частично исполненная заявка
# получала closed_ts, переставала синхронизироваться и навсегда оставалась
# наполовину исполненной, хотя остаток продолжал работать у брокера.
TERMINAL_ORDER_STATUS = frozenset(
    {
        "EXECUTION_REPORT_STATUS_FILL",
        "EXECUTION_REPORT_STATUS_REJECTED",
        "EXECUTION_REPORT_STATUS_CANCELLED",
    }
)

ACTIVE_ORDER_STATUS = frozenset(
    {
        "EXECUTION_REPORT_STATUS_NEW",
        "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
    }
)


def is_terminal(status: str) -> bool:
    return (status or "") in TERMINAL_ORDER_STATUS


def update_order_status(order_id: str, status: str, lots_executed: int, price: float) -> None:
    """Довести запись о заявке до фактического состояния.

    Раньше сохранялся только первый ответ брокера: заявка, исполнившаяся
    позже, навсегда оставалась NEW с нулём исполненных лотов, а отмена не
    записывалась вовсе.

    Исполненный объём только растёт: ответ брокера, пришедший не по порядку,
    не должен уменьшать уже учтённое количество.
    """
    conn = connect()
    conn.execute(
        "UPDATE orders SET status = ?, lots_executed = MAX(COALESCE(lots_executed, 0), ?),"
        " price = CASE WHEN ? > 0 THEN ? ELSE price END,"
        " closed_ts = CASE WHEN ? THEN COALESCE(closed_ts, ?) ELSE closed_ts END"
        " WHERE order_id = ?",
        (
            status,
            lots_executed,
            price,
            price,
            1 if is_terminal(status) else 0,
            time.time(),
            order_id,
        ),
    )
    conn.commit()


def has_open_entry(instrument_id: str) -> bool:
    """Есть ли по бумаге незакрытый вход по нашим собственным записям.

    Срез портфеля у брокера отстаёт: сразу после исполнения позиция в нём
    может ещё не появиться, и проверка «уже держим» пропустит второй вход в
    ту же идею. Журнал знает об исполнении раньше.
    """
    conn = connect()
    row = conn.execute(
        "SELECT direction, MAX(ts) AS ts FROM orders"
        " WHERE instrument_id = ? AND COALESCE(lots_executed, 0) > 0"
        " GROUP BY direction",
        (instrument_id,),
    ).fetchall()
    moments = {item["direction"]: item["ts"] for item in row}
    bought = moments.get("buy")
    if bought is None:
        return False
    sold = moments.get("sell")
    return sold is None or sold < bought


def open_risk() -> float:
    """Плановый риск всего, что сейчас на столе.

    Считается по карточкам входов: для каждой бумаги, по которой есть
    позиция или неразрешённое намерение на покупку, берётся риск последнего
    входа. Проверка на сделку этого не видит — она смотрит на одну заявку и
    пропускает обе идеи по отдельности.

    Величина плановая: это потеря до стопа, а не обещанный максимум убытка.
    Разрыв цены перепрыгивает стоп, и тогда факт будет больше.
    """
    conn = connect()
    at_risk: set[str] = set()

    flat: set[str] = set()
    row = conn.execute("SELECT positions FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    if row:
        try:
            for position in json.loads(row["positions"]):
                instrument_id = position.get("instrument_id")
                if not instrument_id:
                    continue
                if (position.get("quantity") or 0) > 0:
                    at_risk.add(instrument_id)
                else:
                    # Явный ноль у брокера — это ответ «позиции нет».
                    flat.add(instrument_id)
        except (ValueError, TypeError):
            pass

    for intent in blocking_intents():
        if intent["direction"] == "buy":
            at_risk.add(intent["instrument_id"])

    # Плюс то, что журнал уже считает купленным: срез брокера отстаёт.
    for row in conn.execute(
        "SELECT DISTINCT instrument_id FROM orders WHERE direction = 'buy'"
        " AND COALESCE(lots_executed, 0) > 0 AND ts > ?",
        (time.time() - 7 * 86400,),
    ).fetchall():
        if row["instrument_id"] not in flat and has_open_entry(row["instrument_id"]):
            at_risk.add(row["instrument_id"])

    total = 0.0
    for instrument_id in at_risk:
        total += max(
            _card_risk(
                conn.execute(
                    "SELECT card FROM orders WHERE instrument_id = ? AND direction = 'buy'"
                    " AND card IS NOT NULL ORDER BY ts DESC LIMIT 1",
                    (instrument_id,),
                ).fetchone()
            ),
            _card_risk(
                conn.execute(
                    "SELECT card FROM order_intents WHERE instrument_id = ?"
                    " AND direction = 'buy' AND card IS NOT NULL ORDER BY ts DESC LIMIT 1",
                    (instrument_id,),
                ).fetchone()
            ),
        )
    return round(total, 2)


def _card_risk(row: Optional[sqlite3.Row]) -> float:
    if row is None or not row["card"]:
        return 0.0
    try:
        return float(json.loads(row["card"]).get("risk_rub") or 0.0)
    except (ValueError, TypeError, AttributeError):
        return 0.0


def open_orders() -> list[sqlite3.Row]:
    conn = connect()
    return conn.execute(
        "SELECT * FROM orders WHERE closed_ts IS NULL AND ts > ? ORDER BY ts",
        (time.time() - 7 * 86400,),
    ).fetchall()


# --- реестр сетапов -------------------------------------------------------

# Сколько наблюдений нужно, чтобы сетап считался проверенным. Число
# инженерное, а не статистически достаточное: пятнадцать коррелированных
# сделок одного дня — это не пятнадцать независимых наблюдений.
PLAYBOOK_MIN_TRADES = 15


def register_playbook(record: dict) -> dict:
    """Записать или обновить сетап. Статус выводится из чисел, не объявляется."""
    source = str(record.get("source") or "manual")
    if source not in ("manual", "evaluator"):
        raise ValueError(f"неизвестный источник статистики {source!r}")
    conn = connect()
    name = str(record["name"]).strip()[:80]
    now = time.time()
    row = conn.execute(
        "SELECT created_ts FROM playbooks WHERE name = ?", (name,)
    ).fetchone()
    created = float(row["created_ts"]) if row else now
    conn.execute(
        "INSERT INTO playbooks (name, created_ts, updated_ts, entry, invalidation,"
        " measured_on, trades, wins, avg_r, source, evidence)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(name) DO UPDATE SET updated_ts = excluded.updated_ts,"
        " entry = excluded.entry, invalidation = excluded.invalidation,"
        " measured_on = excluded.measured_on, trades = excluded.trades,"
        " wins = excluded.wins, avg_r = excluded.avg_r,"
        " source = excluded.source, evidence = excluded.evidence",
        (
            name,
            created,
            now,
            str(record["entry"])[:600],
            str(record["invalidation"])[:600],
            str(record["measured_on"])[:600],
            int(record["trades"]),
            int(record["wins"]),
            float(record["avg_r"]),
            source,
            _dump(record.get("evidence")) if record.get("evidence") else None,
        ),
    )
    conn.commit()
    log_event(
        "playbook_registered",
        {"name": name, "trades": int(record["trades"]), "source": source},
    )
    return playbook(name)


def retire_playbook(name: str, why: str) -> None:
    conn = connect()
    conn.execute(
        "UPDATE playbooks SET retired = 1, retired_why = ?, updated_ts = ? WHERE name = ?",
        (why[:400], time.time(), name),
    )
    conn.commit()
    log_event("playbook_retired", {"name": name, "why": why[:200]})


def playbook(name: str) -> Optional[dict]:
    conn = connect()
    row = conn.execute(
        "SELECT * FROM playbooks WHERE name = ?", (str(name).strip()[:80],)
    ).fetchone()
    return _playbook_view(row) if row else None


def playbooks() -> list[dict]:
    conn = connect()
    return [
        _playbook_view(row)
        for row in conn.execute("SELECT * FROM playbooks ORDER BY name").fetchall()
    ]


def _playbook_view(row: sqlite3.Row) -> dict:
    trades, wins = int(row["trades"]), int(row["wins"])
    source = row["source"] if "source" in row.keys() else "manual"
    if row["retired"]:
        status = "retired"
    elif (
        trades >= PLAYBOOK_MIN_TRADES
        and float(row["avg_r"]) > 0
        and source == "evaluator"
    ):
        status = "working"
    else:
        status = "probation"
    return {
        "name": row["name"],
        "entry": row["entry"],
        "invalidation": row["invalidation"],
        "measured_on": row["measured_on"],
        "trades": trades,
        "wins": wins,
        "hit_rate": round(wins / trades, 2) if trades else None,
        "avg_r": round(float(row["avg_r"]), 2),
        "status": status,
        "source": source,
        "retired_why": row["retired_why"] or "",
        "updated_ts": row["updated_ts"],
    }


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
WATCH_KINDS = ("price_above", "price_below", "pct_move", "url_changed")


def add_watch(
    instrument_id: str,
    ticker: str,
    kind: str,
    threshold: float,
    note: str,
    expires_ts: float,
    base_price: Optional[float] = None,
    url: str = "",
    content_hash: str = "",
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
        " expires_ts, base_price, url, content_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            time.time(), instrument_id, ticker, kind, float(threshold), note,
            expires_ts, base_price, url, content_hash,
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


def set_watch_hash(watch_id: int, content_hash: str) -> None:
    conn = connect()
    conn.execute("UPDATE watches SET content_hash = ? WHERE id = ?", (content_hash, watch_id))
    conn.commit()


def mark_watch_fired(watch_id: int, value: float) -> None:
    conn = connect()
    conn.execute(
        "UPDATE watches SET fired_ts = ?, fired_value = ? WHERE id = ?",
        (time.time(), value, watch_id),
    )
    conn.commit()


def fire_watch(watch_id: int, value: float, text: str) -> None:
    """Снять наблюдатель и поставить сообщение в очередь одной транзакцией.

    Раздельно это две записи с зазором между ними: наблюдатель уже снят, а
    сообщение ещё не поставлено. Падение в этот момент теряет событие
    навсегда — наблюдатель больше не сработает, потому что уже сработал.
    """
    conn = connect()
    moment = time.time()
    with conn:
        conn.execute(
            "UPDATE watches SET fired_ts = ?, fired_value = ? WHERE id = ?",
            (moment, value, watch_id),
        )
        conn.execute(
            "INSERT INTO messages (ts, source, text) VALUES (?, ?, ?)",
            (moment, "watch", text),
        )


def expire_watches() -> int:
    conn = connect()
    cursor = conn.execute(
        "UPDATE watches SET cancelled = 1 WHERE fired_ts IS NULL AND cancelled = 0"
        " AND expires_ts IS NOT NULL AND expires_ts <= ?",
        (time.time(),),
    )
    conn.commit()
    return cursor.rowcount
