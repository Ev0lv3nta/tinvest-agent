"""Фоновая проверка условий: за рынком следит код, а не модель.

За первый прогон агент шестнадцать раз будил себя будильником, чтобы
посмотреть, дошла ли цена до уровня. Почти все эти пробуждения кончились
ничем, но каждое стоило полного хода: перечитать заметки, опросить котировки,
поставить следующий будильник.

Наблюдение за уровнем — работа для одного batch-запроса раз в полминуты.
Модель просыпается, когда условие выполнено, и получает сразу цену и свою
заметку о том, что это значит.

Сработавшее условие не доставляется отсюда: оно кладётся в очередь сообщений
той же транзакцией, которой снимается наблюдатель. Поток наблюдателя не
должен зависеть ни от состояния канала, ни от того, жив ли получатель.
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

# Страницы раскрытия меняются редко; чаще пяти минут смысла нет, а лишний
# трафик к эмитенту ни к чему.
URL_INTERVAL = 300.0


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
        market_open: Callable[[], bool],
        client_factory: Callable[[], SandboxClient],
    ) -> None:
        super().__init__(daemon=True, name="watcher")
        self.market_open = market_open
        self.client_factory = client_factory
        self.running = True

    def run(self) -> None:
        last_urls = 0.0
        while self.running:
            try:
                self.tick()
                if time.time() - last_urls >= URL_INTERVAL:
                    last_urls = time.time()
                    self.tick_urls()
            except Exception as exc:  # noqa: BLE001 — поток не должен падать
                journal.log_event("watcher_error", {"error": repr(exc)[:300]})
            time.sleep(self.interval())

    def interval(self) -> float:
        """Частоту задаёт наличие условий, а не только календарь.

        Календарь у нас грубый: суббота и воскресенье считаются выходными
        целиком, хотя на Мосбирже есть выходные сессии по отдельным
        инструментам. Ошибиться в частоте опроса дёшево, опоздать со стопом
        по открытой позиции — нет. Поэтому пока есть хоть одно ценовое
        условие, опрашиваем часто.
        """
        if self.market_open():
            return INTERVAL_MARKET
        watching = any(
            row["kind"] in ("price_above", "price_below", "pct_move")
            for row in journal.active_watches()
        )
        return INTERVAL_MARKET if watching else INTERVAL_CLOSED

    def tick(self) -> list[str]:
        expired = journal.expire_watches()
        if expired:
            journal.log_event("watches_expired", {"count": expired})
        rows = [
            row for row in journal.active_watches()
            if row["kind"] in ("price_above", "price_below", "pct_move")
        ]
        if not rows:
            return []
        # Календарь определяет частоту опроса, но не право проверить условие.
        # Раньше проверка целиком отключалась по «суббота или воскресенье»,
        # хотя на Мосбирже есть выходные сессии по отдельным инструментам, а
        # наблюдатель на уровне открытой позиции — это управление риском.
        # Если бумага действительно не торгуется, цена просто не придёт.

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
            text = describe(row, float(price))
            # Снятие наблюдателя и постановка сообщения — одна транзакция.
            # Порознь между ними есть зазор: наблюдатель уже снят, сообщения
            # ещё нет, и падение в этот момент теряет событие насовсем.
            journal.fire_watch(row["id"], float(price), text)
            fired.append(text)
        if fired:
            journal.log_event("watches_fired", {"count": len(fired), "texts": fired})
        return fired

    def tick_urls(self) -> list[str]:
        """Страницы проверяются и вне торгов: раскрытие выходит когда угодно."""
        from gateway import marketdata

        rows = [row for row in journal.active_watches() if row["kind"] == "url_changed"]
        fired = []
        for row in rows:
            try:
                current = marketdata.page_hash(row["url"])
            except Exception as exc:  # noqa: BLE001 — сайт эмитента может лежать
                journal.log_event(
                    "url_watch_failed", {"url": row["url"], "error": str(exc)[:200]}
                )
                continue
            if not row["content_hash"]:
                journal.set_watch_hash(row["id"], current)
                continue
            if current == row["content_hash"]:
                continue
            text = f"Изменилась страница {row['url']}. Твоя заметка: {row['note']}"
            journal.fire_watch(row["id"], 0.0, text)
            fired.append(text)
        if fired:
            journal.log_event("url_watches_fired", {"count": len(fired), "texts": fired})
        return fired

    def stop(self) -> None:
        self.running = False
