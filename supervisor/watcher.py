"""Фоновая проверка условий: за рынком следит код, а не модель.

За первый прогон агент шестнадцать раз будил себя будильником, чтобы
посмотреть, дошла ли цена до уровня. Почти все эти пробуждения кончились
ничем, но каждое стоило полного хода: перечитать заметки, опросить котировки,
поставить следующий будильник.

Наблюдение за уровнем — работа для одного batch-запроса раз в полминуты.
Модель просыпается, когда условие выполнено, и получает сразу цену и свою
заметку о том, что это значит.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from gateway import journal
from gateway.tinvest import SandboxClient, TInvestError

# Раз в полминуты — это быстрее любого разумного будильника и при этом
# один запрос на все условия сразу.
INTERVAL_MARKET = 30.0
INTERVAL_CLOSED = 300.0


def satisfied(row, price: float) -> bool:
    kind = row["kind"]
    threshold = float(row["threshold"])
    if kind == "price_above":
        return price >= threshold
    if kind == "price_below":
        return price <= threshold
    if kind == "pct_move":
        base = float(row["base_price"] or 0)
        if base <= 0:
            return False
        return abs(price / base - 1) * 100 >= threshold
    return False


def describe(row, price: float) -> str:
    ticker = row["ticker"] or row["instrument_id"][:8]
    if row["kind"] == "price_above":
        what = f"{ticker} прошла {row['threshold']} вверх"
    elif row["kind"] == "price_below":
        what = f"{ticker} ушла ниже {row['threshold']}"
    else:
        base = float(row["base_price"] or 0)
        moved = (price / base - 1) * 100 if base else 0.0
        what = f"{ticker} сдвинулась на {moved:+.2f}% от {base}"
    return f"{what}. Сейчас {price}. Твоя заметка: {row['note']}"


class Watcher(threading.Thread):
    def __init__(
        self,
        on_fire: Callable[[list[str]], None],
        market_open: Callable[[], bool],
        client_factory: Callable[[], SandboxClient],
    ) -> None:
        super().__init__(daemon=True, name="watcher")
        self.on_fire = on_fire
        self.market_open = market_open
        self.client_factory = client_factory
        self.running = True

    def run(self) -> None:
        while self.running:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — поток не должен падать
                journal.log_event("watcher_error", {"error": repr(exc)[:300]})
            time.sleep(INTERVAL_MARKET if self.market_open() else INTERVAL_CLOSED)

    def tick(self) -> list[str]:
        expired = journal.expire_watches()
        if expired:
            journal.log_event("watches_expired", {"count": expired})
        rows = journal.active_watches()
        if not rows or not self.market_open():
            return []

        instruments = sorted({row["instrument_id"] for row in rows})
        try:
            prices = {
                item["instrument_id"]: item["price"]
                for item in self.client_factory().last_price(instruments)
                if item.get("price")
            }
        except TInvestError as exc:
            journal.log_event("watcher_prices_failed", {"error": str(exc)[:200]})
            return []

        fired = []
        for row in rows:
            price = prices.get(row["instrument_id"])
            if price is None or not satisfied(row, float(price)):
                continue
            journal.mark_watch_fired(row["id"], float(price))
            fired.append(describe(row, float(price)))
        if fired:
            journal.log_event("watches_fired", {"count": len(fired), "texts": fired})
            self.on_fire(fired)
        return fired

    def stop(self) -> None:
        self.running = False
