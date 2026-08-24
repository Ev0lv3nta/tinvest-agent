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
        conn = journal.connect()
        for table in (
            "tool_calls", "orders", "snapshots", "wakeups", "events",
            "transcript", "messages", "kv", "order_intents", "usage", "watches",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


def msk(hour: int, minute: int = 0, day: int = 24) -> float:
    return datetime(2026, 8, day, hour, minute, tzinfo=MSK).timestamp()


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

    def post_order(self, instrument_id, lots, direction, price=None, order_id="") -> dict:
        if self.post_error is not None:
            raise self.post_error
        self.posted.append(
            {"instrument_id": instrument_id, "lots": lots, "direction": direction,
             "price": price, "order_id": order_id}
        )
        return {
            "order_id": order_id, "status": "EXECUTION_REPORT_STATUS_FILL",
            "lots_requested": lots, "lots_executed": lots,
            "price": price or self.price, "total": (price or self.price) * lots,
            "commission": 0.0, "message": "", "raw": {},
        }


def flat_bars(count: int = 60, price: float = 100.0, spread: float = 0.2) -> list[dict]:
    """Свечи с предсказуемым ATR: истинный диапазон каждой равен spread."""
    base = datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc)
    return [
        {
            "time": (base + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": price, "high": price + spread / 2, "low": price - spread / 2,
            "close": price, "volume": 1000,
        }
        for i in range(count)
    ]
