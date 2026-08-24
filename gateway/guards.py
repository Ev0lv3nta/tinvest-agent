"""Ограничители. Проверяются до отправки заявки.

Песочница не считает маржинальные показатели, допускает отрицательный
баланс и не выставляет маржинколл — то есть сама по себе не наказывает
за риск. Ограничения, которые в боевом контуре обеспечивал бы брокер,
приходится держать здесь.
"""

from __future__ import annotations

from . import config, journal


class GuardRejection(RuntimeError):
    """Заявка отклонена ограничителем. Текст уходит агенту как результат."""


HALT_KEY = "halted"


def halted() -> str:
    """Причина остановки, если прогон остановлен. Пустая строка — работаем."""
    return journal.kv_get(HALT_KEY, "") or ""


def halt(reason: str) -> None:
    if not halted():
        journal.kv_set(HALT_KEY, reason)
        journal.log_event("halt", {"reason": reason})


def check_not_halted() -> None:
    reason = halted()
    if reason:
        raise GuardRejection(
            f"Прогон остановлен: {reason}\n"
            f"Торговые операции заблокированы. Возобновить может только человек. "
            f"Опиши в заметках, что произошло и что бы ты сделал иначе."
        )


def check_capital_floor(total: float) -> None:
    """Порог остановки: ниже него прогон считается проигранным и закрывается."""
    if total < config.CAPITAL_FLOOR:
        halt(
            f"стоимость портфеля {total:.2f} ₽ опустилась ниже порога "
            f"{config.CAPITAL_FLOOR:.0f} ₽"
        )
        check_not_halted()


def check_rate_limit() -> None:
    recent = journal.orders_last_hour()
    if recent >= config.MAX_ORDERS_PER_HOUR:
        raise GuardRejection(
            f"Отклонено: за последний час уже {recent} заявок при лимите "
            f"{config.MAX_ORDERS_PER_HOUR}. Лимит защищает от зацикливания — "
            f"если серия сделок осмысленна, подожди и объясни причину в заметках."
        )


def check_buy(lots: int, limits: dict, ticker: str) -> None:
    """Покупка только на свои. buy_max_lots считается без учёта маржи."""
    if lots <= 0:
        raise GuardRejection("Количество лотов должно быть положительным.")
    available = limits.get("buy_max_lots", 0)
    if not config.ALLOW_LEVERAGE and lots > available:
        raise GuardRejection(
            f"Отклонено: запрошено {lots} лот(ов) {ticker}, на свои деньги "
            f"доступно {available}. Плечо в этом стенде запрещено — "
            f"свободных средств {limits.get('buy_money', 0):.2f} ₽."
        )


def check_sell(lots: int, limits: dict, ticker: str) -> None:
    """Продажа только из портфеля: продать больше — это шорт."""
    if lots <= 0:
        raise GuardRejection("Количество лотов должно быть положительным.")
    available = limits.get("sell_max_lots", 0)
    if not config.ALLOW_SHORT and lots > available:
        raise GuardRejection(
            f"Отклонено: запрошено продать {lots} лот(ов) {ticker}, в портфеле "
            f"{available}. Шорт в этом стенде запрещён."
        )
