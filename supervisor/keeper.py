"""Сторож позиций: выход исполняет код, а не пробуждение модели.

Стоп, записанный в карточке сделки, сам по себе ничего не останавливает. Он
исполняется только если агент проснулся, вспомнил о позиции, посмотрел цену
и отправил заявку. Между этими «если» помещается весь убыток: сессия
занята другим, канал к модели лежит, ход идёт полторы минуты, а цена уже
прошла уровень.

Поэтому обязательство живёт в базе, а исполняет его этот поток. Он умеет
ровно одно действие — продать то, что уже куплено. Ни открыть позицию, ни
увеличить её он не может: у него нет для этого кода.

Что здесь важно и почему:

  * условие защёлкивается ДО отправки заявки и переживает перезапуск. Цена,
    вернувшаяся выше стопа, не отменяет уже принятого решения выйти;
  * продаётся только то количество, которое подтвердил брокер в портфеле, —
    не то, что мы думаем, что у нас есть;
  * заявка идёт тем же путём, что и заявка агента: через шлюз, с журналом,
    под той же блокировкой счёта. Сделки, которой нет в журнале, не бывает;
  * агент и оператор узнают о выходе после, а не вместо.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from gateway import journal
from gateway.tinvest import TInvestError

# Цена проверяется одним батч-запросом на все позиции. Чаще, чем наблюдатели
# за уровнями: здесь речь о деньгах, которые уже в рынке.
INTERVAL = 20.0

# Сколько ждать между попытками, если выход не удался. Требование при этом
# остаётся защёлкнутым и повторится — но не каждые двадцать секунд до утра:
# вне торгов брокер отбивает заявку кодом 30079, и упорство здесь означает
# только шум в журнале и в телефоне оператора.
RETRY_DELAY = 30.0
RETRY_MAX = 10 * 60.0

# Об одной и той же неудаче оператору сообщается не чаще, чем раз в четверть
# часа. Первая — сразу.
NOTIFY_INTERVAL = 15 * 60.0


class Keeper(threading.Thread):
    def __init__(self, client_factory: Callable, notify: Callable[[str], None]) -> None:
        super().__init__(daemon=True, name="keeper")
        self.client_factory = client_factory
        self.notify = notify
        self.running = True
        self.last_failure = 0.0
        self.failures = 0
        self.last_notified = 0.0

    def run(self) -> None:
        while self.running:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — поток не должен падать
                journal.log_event("keeper_error", {"error": repr(exc)[:300]})
            time.sleep(INTERVAL)

    def tick(self) -> list[str]:
        mandates = journal.open_mandates()
        if not mandates:
            return []

        client = self.client_factory()
        try:
            portfolio = client.portfolio()
        except TInvestError as exc:
            journal.log_event("keeper_portfolio_failed", {"error": str(exc)[:200]})
            return []

        held = {
            position.get("instrument_id"): position
            for position in portfolio.get("positions", [])
            if (position.get("quantity") or 0) > 0
        }

        # Обязательства по бумагам, которых в портфеле уже нет, закрываются:
        # позиции нет — стеречь нечего.
        alive = []
        for mandate in mandates:
            if mandate["instrument_id"] not in held:
                journal.close_mandate(mandate["id"], "позиции в портфеле нет")
                continue
            alive.append(mandate)
        if not alive:
            return []

        prices = self.prices(client, [m["instrument_id"] for m in alive])
        done = []
        for mandate in alive:
            why = self.triggered(mandate, prices.get(mandate["instrument_id"]))
            if not why:
                continue
            journal.latch_mandate(mandate["id"], why)
            text = self.exit(mandate, held[mandate["instrument_id"]], why)
            if text:
                done.append(text)
        return done

    def prices(self, client, instrument_ids: list[str]) -> dict:
        try:
            return {
                item["instrument_id"]: float(item["price"])
                for item in client.last_price(sorted(set(instrument_ids)))
                if item.get("price")
            }
        except TInvestError as exc:
            journal.log_event("keeper_prices_failed", {"error": str(exc)[:200]})
            return {}

    def triggered(self, mandate, price) -> str:
        """Почему пора выходить. Пустая строка — рано.

        Защёлкнутое требование остаётся требованием: если предыдущая попытка
        не прошла, повторяем, а не пересматриваем.
        """
        if mandate["latched_ts"]:
            return mandate["latched_why"] or "требование уже защёлкнуто"
        deadline = mandate["deadline_ts"]
        if deadline and time.time() >= float(deadline):
            return "истёк срок идеи"
        if price is None:
            # Нет цены — не повод считать, что всё в порядке, но и не повод
            # продавать вслепую. Молчание источника само по себе не сигнал.
            return ""
        if price <= float(mandate["stop"]):
            return f"цена {price} ниже стопа {mandate['stop']}"
        target = mandate["target"]
        if target and price >= float(target):
            return f"цена {price} достигла цели {target}"
        return ""

    def exit(self, mandate, position, why: str) -> str:
        """Продать подтверждённое брокером количество. Заявка идёт через шлюз."""
        if time.time() - self.last_failure < self.backoff():
            return ""
        from gateway import server

        lots = int(position.get("lots") or 0)
        if lots <= 0:
            journal.close_mandate(mandate["id"], "в портфеле нет целых лотов")
            return ""
        ticker = mandate["ticker"] or mandate["instrument_id"][:8]
        try:
            server._order(
                "ORDER_DIRECTION_SELL",
                mandate["instrument_id"],
                lots,
                None,
                f"выход по обязательству: {why}",
            )
        except Exception as exc:  # noqa: BLE001 — брокер, сеть, ограничитель
            self.last_failure = time.time()
            self.failures += 1
            journal.log_event(
                "keeper_exit_failed",
                {"ticker": ticker, "why": why, "attempt": self.failures,
                 "error": str(exc)[:300]},
            )
            if time.time() - self.last_notified >= NOTIFY_INTERVAL:
                self.last_notified = time.time()
                self.notify(
                    f"⚠️ Не удалось выйти из {ticker} по обязательству ({why}): "
                    f"{str(exc)[:200]}. Требование остаётся, попробую снова."
                )
            return ""

        self.failures = 0
        self.last_notified = 0.0
        journal.close_mandate(mandate["id"], f"выход исполнен: {why}")
        text = f"Позиция {ticker} закрыта кодом: {why}."
        journal.log_event("keeper_exit", {"ticker": ticker, "why": why, "lots": lots})
        # Агент узнаёт об этом как о факте: решение уже исполнено, обсуждать
        # нечего — но знать он обязан, иначе будет рассуждать о позиции,
        # которой нет.
        journal.enqueue_message(
            f"{text} Обязательство было записано при входе. Позиции больше нет "
            f"— перепиши state.md и разбери, что это значит для тезиса.",
            source="keeper",
        )
        self.notify(text)
        return text

    def backoff(self) -> float:
        """Пауза между попытками растёт: вне торгов повторять чаще бесполезно."""
        return min(RETRY_DELAY * (2 ** max(self.failures - 1, 0)), RETRY_MAX)

    def stop(self) -> None:
        self.running = False
