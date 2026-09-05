"""Общая обвязка: чистая база на каждый тест и фальшивый брокер."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from gateway import journal

MSK = timezone(timedelta(hours=3))


class JournalCase(unittest.TestCase):
    """База очищается перед каждым тестом, схема остаётся."""

    def setUp(self) -> None:
        # Справочник инструментов живёт файлом и переживал бы тесты, делая
        # результат зависимым от их порядка.
        from gateway import marketdata

        marketdata.save_universe({})
        # Кеш свечей тоже переживает тест: без очистки следующий тест
        # получал бы файл предыдущего вместо своих свечей.
        for stale in marketdata.data_dir().glob("*.csv"):
            stale.unlink()
        conn = journal.connect()
        for table in (
            "tool_calls", "orders", "snapshots", "wakeups", "events",
            "transcript", "messages", "kv", "order_intents", "usage", "watches",
            "playbooks",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


def msk(hour: int, minute: int = 0, days: int = 0) -> float:
    """Момент сегодняшнего торгового дня по Москве, со сдвигом в днях.

    Дата обязана быть относительной. С фиксированной календарной датой тесты
    проходили только в тот день, когда их писали: половина проверок сравнивает
    записи с `day_start_ts()`, а он считается от текущего времени.
    """
    today = datetime.now(MSK).date() + timedelta(days=days)
    return datetime(today.year, today.month, today.day, hour, minute, tzinfo=MSK).timestamp()


def add_playbook(
    name: str = "test", trades: int = 40, wins: int = 24, avg_r: float = 0.4,
    source: str = "evaluator",
) -> dict:
    """Зарегистрированный сетап: без него вход не пройдёт реестр."""
    return journal.register_playbook(
        {
            "name": name,
            "entry": "закрытие выше максимума предыдущих 20 баров",
            "invalidation": "закрытие ниже уровня пробоя в течение двух баров",
            "measured_on": "проверка на дневных барах 2024 года, 12 бумаг",
            "trades": trades, "wins": wins, "avg_r": avg_r, "source": source,
        }
    )


def add_order(**kwargs) -> None:
    """Заявка в журнал напрямую: тестам не нужен полный путь _order."""
    conn = journal.connect()
    row = {
        "ts": kwargs.get("ts", 0.0),
        "order_id": kwargs.get("order_id", "x"),
        "instrument_id": kwargs.get("instrument_id", "uid"),
        "figi": kwargs.get("figi", ""),
        "direction": kwargs.get("direction", "buy"),
        "order_type": "market",
        "lots": kwargs.get("lots", 1),
        "lots_executed": kwargs.get("lots_executed", 1),
        "price": kwargs.get("price", 100.0),
    }
    conn.execute(
        "INSERT INTO orders (ts, order_id, instrument_id, figi, direction, order_type,"
        " lots, lots_executed, price) VALUES (:ts, :order_id, :instrument_id, :figi,"
        " :direction, :order_type, :lots, :lots_executed, :price)",
        row,
    )
    conn.commit()


class FakeBroker:
    """Брокер, который делает ровно то, что просит тест."""

    def __init__(self, **kwargs) -> None:
        self.posted: list[dict] = []
        self.total = kwargs.get("total", 100_000.0)
        self.positions = kwargs.get("positions", [])
        self.price = kwargs.get("price", 100.0)
        self.limits = kwargs.get(
            "limits",
            {"buy_max_lots": 100, "buy_max_market_lots": 100, "sell_max_lots": 100,
             "buy_money": 100_000.0},
        )
        self.post_error = kwargs.get("post_error")
        self.active: list[dict] = kwargs.get("active", [])
        self.bars = kwargs.get("bars", [])
        # Состояния заявок по идентификатору: то, что брокер отвечает на
        # точный запрос. Отсутствие ключа означает «такой заявки нет».
        self.states: dict[str, dict] = kwargs.get("states", {})
        self.state_error = kwargs.get("state_error")

    def portfolio(self) -> dict:
        return {"total": self.total, "cash": self.total, "positions": self.positions}

    def max_lots(self, instrument_id, price=None) -> dict:
        return dict(self.limits)

    def last_price(self, ids) -> list[dict]:
        return [{"instrument_id": i, "ticker": "TEST", "price": self.price} for i in ids]

    def trading_status(self, instrument_id) -> dict:
        return {"status": "SECURITY_TRADING_STATUS_NORMAL_TRADING"}

    def instrument_by_uid(self, uid) -> dict:
        return {"instrument_id": uid, "figi": "FIGI1", "ticker": "TEST", "lot": 1}

    def find_instrument(self, query, limit=10, kind="") -> list[dict]:
        return [{"instrument_id": "uid", "figi": "FIGI1", "ticker": query.upper(), "lot": 1}]

    def candles(self, uid, interval, days) -> list[dict]:
        return list(self.bars)

    def active_orders(self) -> list[dict]:
        return list(self.active)

    def operations(self, days: int = 7) -> list[dict]:
        return list(getattr(self, "ops", []))

    def order_state(self, order_id: str, by_request_id: bool = False) -> dict:
        from gateway.tinvest import OrderNotFound

        if self.state_error is not None:
            raise self.state_error
        state = self.states.get(order_id)
        if state is None:
            raise OrderNotFound(f"нет заявки {order_id}", http_status=400)
        return {
            "order_id": state.get("order_id", order_id),
            "request_id": order_id,
            "status": state.get("status", ""),
            "direction": state.get("direction", ""),
            "instrument_id": state.get("instrument_id", "uid"),
            "figi": state.get("figi", "FIGI1"),
            "lots_requested": state.get("lots_requested", 0),
            "lots_executed": state.get("lots_executed", 0),
            "price": state.get("price", 0.0),
            "total": state.get("total", 0.0),
            "commission": state.get("commission", 0.0),
            "stages": state.get("stages", []),
            "raw": {},
        }

    def cancel_order(self, order_id: str) -> str:
        self.active = [o for o in self.active if o.get("order_id") != order_id]
        return "2026-08-24T12:00:00Z"

    def post_order(self, instrument_id, lots, direction, price=None, order_id="") -> dict:
        if self.post_error is not None:
            raise self.post_error
        self.posted.append(
            {"instrument_id": instrument_id, "lots": lots, "direction": direction,
             "price": price, "order_id": order_id}
        )
        state = {
            "order_id": order_id, "request_id": order_id,
            "status": "EXECUTION_REPORT_STATUS_FILL",
            "direction": direction, "instrument_id": instrument_id, "figi": "FIGI1",
            "lots_requested": lots, "lots_executed": lots,
            "price": price or self.price, "total": (price or self.price) * lots,
            "commission": 0.0, "stages": [], "message": "", "raw": {},
        }
        self.states[order_id] = dict(state)
        return state


def flat_bars(count: int = 60, price: float = 100.0, spread: float = 0.2) -> list[dict]:
    """Свечи с предсказуемым ATR: истинный диапазон каждой равен spread."""
    base = datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc)
    return [
        {
            "time": (base + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": price, "high": price + spread / 2, "low": price - spread / 2,
            "close": price, "volume": 1000, "complete": True,
        }
        for i in range(count)
    ]
