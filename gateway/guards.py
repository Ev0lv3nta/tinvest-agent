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

import math
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


def check_positions(
    portfolio: dict, instrument_id: str, pending_buys: Optional[set] = None
) -> None:
    """Лимит считает и висящие заявки на вход.

    Иначе его обходят стопкой неисполненных лимиток: позиций формально нет,
    а обязательства уже набраны.
    """
    taken = {
        position.get("instrument_id")
        for position in portfolio.get("positions", [])
        if (position.get("quantity") or 0) > 0
    }
    taken |= set(pending_buys or ())
    if instrument_id in taken:
        return
    if len(taken) >= config.MAX_POSITIONS:
        raise GuardRejection(
            f"Отклонено: уже {len(taken)} позиций и заявок на вход при лимите "
            f"{config.MAX_POSITIONS}. Внимание не делится бесконечно: чтобы "
            f"взять новую идею, закрой ту, в которую веришь меньше, или сними "
            f"её заявку."
        )


# --- экономика сделки -----------------------------------------------------


def check_playbook(name: str, risk_rub: float) -> dict:
    """Торгуется только зарегистрированный сетап, и размером по его статусу.

    Раньше требовались две непустые строки: имя сетапа и «базовая ставка».
    Проходило и несуществующее имя, и текст вместо статистики — то есть
    проверка была на длину строки, а не на существование измерения.

    Регистрация обязана предшествовать сделке. Сетап, придуманный в момент
    входа, не является основанием: он не отличим от объяснения задним числом.
    """
    record = journal.playbook(name)
    if record is None:
        known = ", ".join(item["name"] for item in journal.playbooks()) or "ни одного"
        raise GuardRejection(
            f"Отклонено: сетап {name!r} не зарегистрирован. Зарегистрированы: "
            f"{known}.\nСетап регистрируется до сделки через register_playbook: "
            f"условие входа, условие опровержения, на чём измерена статистика, "
            f"число сделок, попаданий и средний результат в R. Пока измерения "
            f"нет, входа нет — иначе объяснение пишется задним числом."
        )
    if record["status"] == "retired":
        raise GuardRejection(
            f"Отклонено: сетап {name!r} снят с торговли — {record['retired_why']}. "
            f"Красивый график не отменяет отрицательной базы. Если появились "
            f"новые данные, перерегистрируй его с обновлённой статистикой."
        )
    if record["status"] == "probation":
        limit = config.MAX_RISK_PER_TRADE * config.PROBATION_RISK_FRACTION
        if risk_rub > limit:
            raise GuardRejection(
                f"Отклонено: сетап {name!r} на проверке — {record['trades']} сделок "
                f"при пороге {journal.PLAYBOOK_MIN_TRADES}, средний результат "
                f"{record['avg_r']}R. Такие торгуются четвертью размера: риск не "
                f"больше {limit:.0f} ₽, запрошено {risk_rub:.0f} ₽. Полный размер "
                f"даёт накопленная статистика, а не уверенность в моменте."
            )
    return record


def check_no_pyramiding(portfolio: dict, instrument_id: str, ticker: str) -> None:
    """Второй вход в ту же бумагу — это не вторая идея, а увеличение первой.

    Риск считается по карточке одного входа, поэтому две покупки одного
    тезиса проходят порознь и складываются уже в позиции. Наращивание
    требует пересчёта стопа по всей позиции, а этого механизма здесь нет.
    """
    held = any(
        position.get("instrument_id") == instrument_id
        and (position.get("quantity") or 0) > 0
        for position in portfolio.get("positions", [])
    )
    # Срез брокера отстаёт от исполнения, поэтому спрашиваем и журнал.
    if not held and not journal.has_open_entry(instrument_id):
        return
    raise GuardRejection(
        f"Отклонено: позиция в {ticker} уже есть. Добавление к открытой "
        f"позиции здесь запрещено: риск посчитан по стопу первого входа, а "
        f"после долива его пришлось бы пересчитывать на всю позицию. Если "
        f"тезис изменился — выйди и войди заново с новой карточкой."
    )


def check_portfolio_heat(new_risk: float, open_risk: float, ticker: str) -> None:
    """Риск считается по портфелю, а не по одной сделке.

    Потолок в 1R на сделку сам по себе ничего не ограничивает: две идеи по
    900 ₽ проходят каждая, а на столе уже 1800 ₽. Две сильно связанные
    бумаги при этом выбивает одним движением рынка, то есть «два независимых
    риска» оказываются одним.
    """
    total = new_risk + open_risk
    if total <= config.MAX_PORTFOLIO_RISK:
        return
    left = max(config.MAX_PORTFOLIO_RISK - open_risk, 0.0)
    raise GuardRejection(
        f"Отклонено: под риском уже {open_risk:.0f} ₽, новая позиция в {ticker} "
        f"добавляет {new_risk:.0f} ₽ — вместе {total:.0f} ₽ при потолке "
        f"{config.MAX_PORTFOLIO_RISK:.0f} ₽ "
        f"({config.MAX_PORTFOLIO_RISK / config.RISK_UNIT:.0f}R на портфель). "
        f"Свободно {left:.0f} ₽: либо уменьши размер, либо закрой то, во что "
        f"веришь меньше. Стопы по разным бумагам могут сработать в один день."
    )


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
    for name, value in (("вход", entry), ("стоп", stop), ("цель", target)):
        # Бесконечность и NaN проходят любое сравнение «больше нуля», а
        # дальше делают барьеры декоративными: при бесконечной цели
        # отношение прибыли к риску бесконечно, и три проверки подряд
        # отвечают «да». Объявленная в схеме типизация вызов не проверяет —
        # это делает код.
        if not math.isfinite(value):
            raise GuardRejection(
                f"Цена ({name}) должна быть конечным числом, получено {value!r}. "
                f"Карточка сделки — это измерение, а не декларация."
            )
    if not (entry > 0 and stop > 0 and target > 0):
        raise GuardRejection("Цены входа, стопа и цели должны быть положительными.")
    if not isinstance(lots, int) or lots <= 0:
        raise GuardRejection(
            f"Количество лотов должно быть целым положительным, получено {lots!r}."
        )
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
