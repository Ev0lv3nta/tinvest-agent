"""Ограничители. Проверяются до отправки заявки.

Песочница не считает маржинальные показатели, допускает отрицательный
баланс и не выставляет маржинколл — то есть сама по себе не наказывает
за риск. Ограничения, которые в боевом контуре обеспечивал бы брокер,
приходится держать здесь.

Второй слой появился после первого прогона. Кода хватало на катастрофу
(плечо, шорт, потеря 70% капитала), но не на медленный слив, а слив здесь
как раз медленный и по одному сценарию: вход без преимущества, стоп,
следующий вход. Обе сделки того дня были убыточны арифметически, до всякого
рынка: у одной цель была ближе стопа, у другой стоп стоял внутри шума. Ни
один барьер этого не поймал, потому что барьеров не было — были пожелания в
инструкции, которые агент вечером сам же и разобрал как нарушенные.

Правило разделения простое: всё, что можно объективно посчитать, считает
код. Промпт отвечает за гипотезу и интерпретацию.
"""

from __future__ import annotations

import time
from typing import Optional

from . import config, journal


class GuardRejection(RuntimeError):
    """Заявка отклонена ограничителем. Текст уходит агенту как результат."""


HALT_KEY = "halted"


# --- остановка прогона ----------------------------------------------------


def halted() -> str:
    """Причина остановки, если прогон остановлен. Пустая строка — работаем."""
    return journal.kv_get(HALT_KEY, "") or ""


def halt(reason: str) -> None:
    if not halted():
        journal.kv_set(HALT_KEY, reason)
        journal.log_event("halt", {"reason": reason})


def check_entry_allowed() -> None:
    """Остановка запрещает набирать риск, но не запрещает от него избавляться.

    Продажа и снятие заявки разрешены всегда. Иначе защита срабатывала бы
    ровно в тот момент, когда выход нужнее всего: до этой правки падение
    портфеля ниже порога блокировало не только покупку, но и продажу, а
    первая же попытка выйти сама ставила блокировку навсегда.
    """
    reason = halted()
    if reason:
        raise GuardRejection(
            f"Прогон остановлен: {reason}\n"
            f"Новые позиции запрещены. Закрыть имеющиеся и снять заявки можно. "
            f"Возобновить торговлю может только человек — опиши в заметках, "
            f"что произошло и что бы ты сделал иначе."
        )


def check_capital_floor(total: float) -> None:
    """Порог остановки: ниже него прогон считается проигранным и закрывается."""
    if total < config.CAPITAL_FLOOR:
        halt(
            f"стоимость портфеля {total:.2f} ₽ опустилась ниже порога "
            f"{config.CAPITAL_FLOOR:.0f} ₽"
        )
        check_entry_allowed()


# --- темп -----------------------------------------------------------------


def check_rate_limit() -> None:
    recent = journal.orders_last_hour()
    if recent >= config.MAX_ORDERS_PER_HOUR:
        raise GuardRejection(
            f"Отклонено: за последний час уже {recent} заявок при лимите "
            f"{config.MAX_ORDERS_PER_HOUR}. Лимит защищает от зацикливания — "
            f"если серия сделок осмысленна, подожди и объясни причину в заметках."
        )


def check_entries_today() -> None:
    used = journal.entries_today()
    if used >= config.MAX_ENTRIES_PER_DAY:
        raise GuardRejection(
            f"Отклонено: сегодня уже {used} входов при лимите "
            f"{config.MAX_ENTRIES_PER_DAY}. Ограничение стоит на числе решений, "
            f"а не на скорости заявок: пятый вход за день почти всегда хуже "
            f"первых четырёх. Остаток дня — на разбор и подготовку."
        )


def check_daily_loss(total: float) -> None:
    """Дневной стоп в коде, а не в дисциплине.

    В разобранном дне между двумя убыточными сделками прошло три с половиной
    часа поиска третьей попытки, а правило «после убытков не отыгрываться»
    агент записал себе уже после второго убытка. Правило, которое вспоминают
    задним числом, правилом не является.
    """
    opening = journal.day_open_equity()
    if opening is None:
        return
    result = total - opening
    if result > -config.DAILY_LOSS_LIMIT:
        return
    raise GuardRejection(
        f"Отклонено: результат дня {result:+.0f} ₽ достиг дневного стопа "
        f"−{config.DAILY_LOSS_LIMIT:.0f} ₽ "
        f"({config.DAILY_LOSS_LIMIT / config.RISK_UNIT:.0f}R). "
        f"Новых входов сегодня нет, выходы разрешены. Оставшееся время — "
        f"разбор: что в отборе пропустило эти сделки и какой барьер должен "
        f"был их остановить."
    )


def check_cooldown(instrument_id: str, ticker: str) -> None:
    """После убыточного выхода из бумаги — пауза, и только по новому сигналу.

    Именно этот паттерн дал OZON → TRNFP: убыток, три часа поиска новой
    попытки, второй убыток.
    """
    loss_ts = journal.last_losing_exit(instrument_id)
    if not loss_ts:
        return
    passed = time.time() - loss_ts
    if passed < config.LOSS_COOLDOWN:
        left = int((config.LOSS_COOLDOWN - passed) / 60)
        raise GuardRejection(
            f"Отклонено: сегодня ты уже вышел из {ticker} в минус "
            f"{int(passed / 60)} минут назад. Пауза ещё {left} мин. "
            f"Возврат в ту же бумагу сразу после стопа — это отыгрывание, "
            f"а не новая идея. Если появился независимый факт, которого не "
            f"было при первом входе, дождись конца паузы и запиши его."
        )


# --- размер и направление -------------------------------------------------


def check_buy(lots: int, limits: dict, ticker: str, market: bool) -> None:
    """Покупка только на свои.

    Для рыночной заявки у API отдельный лимит: `buyMaxMarketLots` учитывает
    возможное проскальзывание и обычно меньше лимитного.
    """
    if lots <= 0:
        raise GuardRejection("Количество лотов должно быть положительным.")
    key = "buy_max_market_lots" if market else "buy_max_lots"
    available = limits.get(key, 0)
    if not config.ALLOW_LEVERAGE and lots > available:
        kind = "рыночной" if market else "лимитной"
        raise GuardRejection(
            f"Отклонено: запрошено {lots} лот(ов) {ticker}, для {kind} заявки "
            f"на свои деньги доступно {available}. Плечо в этом стенде "
            f"запрещено — свободных средств {limits.get('buy_money', 0):.2f} ₽."
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


def check_positions(portfolio: dict, instrument_id: str) -> None:
    open_positions = {
        position.get("instrument_id")
        for position in portfolio.get("positions", [])
        if (position.get("quantity") or 0) > 0
    }
    if instrument_id in open_positions:
        return
    if len(open_positions) >= config.MAX_POSITIONS:
        raise GuardRejection(
            f"Отклонено: уже {len(open_positions)} открытых позиций при лимите "
            f"{config.MAX_POSITIONS}. Внимание не делится бесконечно: чтобы "
            f"взять новую идею, закрой ту, в которую веришь меньше."
        )


# --- экономика сделки -----------------------------------------------------


def check_trade_card(
    entry: float,
    stop: float,
    target: float,
    lots: int,
    lot_size: int,
    ticker: str,
    atr: Optional[float] = None,
) -> dict:
    """Три барьера входа. Все три — арифметика по уже записанным числам.

    Проверяются до сделки именно потому, что после сделки их проверяет рынок,
    и это дороже.
    """
    if not (entry > 0 and stop > 0 and target > 0):
        raise GuardRejection("Цены входа, стопа и цели должны быть положительными.")
    if stop >= entry:
        raise GuardRejection(
            f"Стоп {stop} не ниже входа {entry}. Здесь только длинные позиции: "
            f"стоп — это цена, ниже которой тезис опровергнут."
        )
    if target <= entry:
        raise GuardRejection(f"Цель {target} не выше входа {entry}.")

    risk_per_share = entry - stop
    reward_per_share = target - entry
    shares = lots * max(lot_size, 1)
    risk_rub = risk_per_share * shares
    reward_risk = reward_per_share / risk_per_share
    target_pct = reward_per_share / entry
    cost_pct = config.ROUND_TRIP_COST

    if reward_risk < config.MIN_REWARD_RISK:
        raise GuardRejection(
            f"Отклонено: соотношение прибыль/риск {reward_risk:.2f} при минимуме "
            f"{config.MIN_REWARD_RISK}. Вход {entry}, стоп {stop} "
            f"(−{risk_per_share:.2f}), цель {target} (+{reward_per_share:.2f}). "
            f"Такая сделка убыточна по матожиданию при точности ниже "
            f"{100 / (1 + reward_risk):.0f}%. Чтобы пройти барьер, цель должна "
            f"быть не ближе {entry + risk_per_share * config.MIN_REWARD_RISK:.2f} "
            f"либо стоп не дальше "
            f"{entry - reward_per_share / config.MIN_REWARD_RISK:.2f}."
        )

    if target_pct < cost_pct * config.MIN_TARGET_OVER_COST:
        raise GuardRejection(
            f"Отклонено: цель {target_pct * 100:.2f}% при круговых издержках "
            f"{cost_pct * 100:.2f}% — комиссия съест "
            f"{cost_pct / target_pct * 100:.0f}% движения. Минимум для входа — "
            f"цель {cost_pct * config.MIN_TARGET_OVER_COST * 100:.2f}%, то есть "
            f"не ближе {entry * (1 + cost_pct * config.MIN_TARGET_OVER_COST):.2f}. "
            f"Скальпинг на таких издержках не окупается."
        )

    if atr and atr > 0 and risk_per_share < config.MIN_STOP_ATR * atr:
        raise GuardRejection(
            f"Отклонено: стоп в {risk_per_share:.2f} при ATR(15м) {atr:.2f} — "
            f"это {risk_per_share / atr:.2f} ATR, то есть внутри обычного шума. "
            f"Такую позицию выбьет случайным движением независимо от того, прав "
            f"ты или нет. Минимум {config.MIN_STOP_ATR} ATR: стоп не выше "
            f"{entry - config.MIN_STOP_ATR * atr:.2f}."
        )

    if risk_rub > config.MAX_RISK_PER_TRADE:
        max_lots = int(config.MAX_RISK_PER_TRADE / (risk_per_share * max(lot_size, 1)))
        raise GuardRejection(
            f"Отклонено: при стопе {stop} позиция в {lots} лот(ов) {ticker} "
            f"рискует {risk_rub:.0f} ₽ при потолке "
            f"{config.MAX_RISK_PER_TRADE:.0f} ₽ (1R). Размер считается от стопа, "
            f"а не от свободных денег: на этом расстоянии помещается "
            f"{max_lots} лот(ов)."
        )

    return {
        "risk_rub": round(risk_rub, 2),
        "risk_r": round(risk_rub / config.RISK_UNIT, 2),
        "reward_risk": round(reward_risk, 2),
        "target_pct": round(target_pct * 100, 2),
        "cost_pct": round(cost_pct * 100, 3),
        "stop_atr": round(risk_per_share / atr, 2) if atr else None,
    }
