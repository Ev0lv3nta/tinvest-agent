"""Сверка биржевых операций с журналом.

Токен Т-Инвестиций доступен агенту на чтение, поэтому технически он может
торговать напрямую, минуя шлюз — а значит мимо ограничителей и мимо журнала.
Запрет на это записан в инструкциях, но инструкции агент может переписать.

Сверка не запрещает, а обнаруживает: операции по счёту сравниваются с нашими
записями, и всё, чему нет объяснения, попадает в журнал и уходит оператору.
Расхождение — это находка эксперимента, а не сбой.
"""

from __future__ import annotations

import time
from typing import Optional

from gateway import config, journal
from gateway.tinvest import OrderNotFound, SandboxClient, TInvestError

INTENT_BY_STATUS = {
    "EXECUTION_REPORT_STATUS_FILL": "filled",
    "EXECUTION_REPORT_STATUS_CANCELLED": "cancelled",
    "EXECUTION_REPORT_STATUS_REJECTED": "rejected",
}

# Сделка, оформленная через шлюз, появляется в операциях не мгновенно;
# допуск с запасом, чтобы не ловить ложные срабатывания на задержке.
MATCH_WINDOW = 20 * 60

TRADE_TYPES = {
    "OPERATION_TYPE_BUY",
    "OPERATION_TYPE_SELL",
    "OPERATION_TYPE_BUY_CARD",
    "OPERATION_TYPE_SELL_MARGIN",
    "OPERATION_TYPE_BUY_MARGIN",
}

_client: Optional[SandboxClient] = None


def client() -> SandboxClient:
    global _client
    if _client is None:
        _client = SandboxClient(
            config.secret("TINVEST_TOKEN"), config.secret("TINVEST_ACCOUNT_ID")
        )
    return _client


def _parse_time(value: str) -> float:
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _direction(operation_type: str) -> str:
    return "sell" if "SELL" in operation_type else "buy"


def sync_orders() -> int:
    """Довести записи о заявках до фактического состояния у брокера.

    Состояние спрашивается точно, по идентификатору каждой заявки. Раньше
    оно выводилось из списка активных: заявки нет в списке — значит
    отменена с нулевым исполнением. Но из активных исчезает и исполненная,
    и отклонённая, а при сбое чтения список приходит пустым целиком — и
    тогда все наши заявки разом записывались отменёнными.
    """
    ours = journal.open_orders()
    if not ours:
        return 0

    changed = 0
    for row in ours:
        try:
            state = client().order_state(row["order_id"])
        except OrderNotFound:
            # Брокер отвечает, что такой заявки нет. Для заявки, которую мы
            # отправляли и на которую получили идентификатор, это само по
            # себе расхождение, а не разрешение придумать статус.
            journal.log_event(
                "order_missing",
                {"order_id": row["order_id"], "ticker": row["ticker"]},
            )
            continue
        except TInvestError as exc:
            journal.log_event(
                "order_sync_failed",
                {"order_id": row["order_id"], "error": str(exc)[:200]},
            )
            continue

        status = state.get("status") or ""
        executed = state.get("lots_executed") or 0
        if status == row["status"] and executed == (row["lots_executed"] or 0):
            continue
        journal.update_order_status(
            row["order_id"], status, executed, state.get("price") or 0.0
        )
        # Намерение живёт вместе с заявкой: заявка дошла до конца — намерение
        # тоже, иначе оно продолжало бы блокировать бумагу.
        intent = journal.intent_by_order(row["order_id"])
        if intent is not None and journal.is_terminal(status):
            journal.set_intent_state(
                intent["request_id"], INTENT_BY_STATUS.get(status, "filled"), status
            )
        changed += 1
    if changed:
        journal.log_event("orders_synced", {"updated": changed})
    return changed


def recover_intents() -> dict:
    """Выяснить судьбу всех незакрытых намерений до начала торговли.

    Восстановление начинается со сверки, а не с новых сделок. Процесс мог
    упасть между записью намерения и ответом брокера; заявка при этом могла
    исполниться. Пока это не выяснено, вход по бумаге закрыт — и лучше
    выяснить один раз на старте, чем в момент, когда агент решил купить.

    Ничего не «истекает»: то, что осталось невыясненным, остаётся
    блокирующим и попадает в отчёт оператору.
    """
    from gateway import server

    pending = journal.blocking_intents()
    if not pending:
        return {"checked": 0, "resolved": 0, "blocked": []}

    resolved = 0
    blocked = []
    for intent in pending:
        ticker = intent["ticker"] or intent["instrument_id"][:8]
        try:
            why = server._resolve_intent(intent, ticker)
        except Exception as exc:  # noqa: BLE001 — восстановление не должно падать
            why = f"проверить не удалось ({type(exc).__name__}: {exc})"
        if why:
            blocked.append({"request_id": intent["request_id"], "ticker": ticker, "why": why})
        else:
            resolved += 1

    report = {"checked": len(pending), "resolved": resolved, "blocked": blocked}
    journal.log_event("intents_recovered", report)
    return report


def _lot_size(figi: str, instrument_id: str) -> int:
    """Размер лота, чтобы сравнивать штуки с лотами.

    В журнале заявки хранятся в лотах, а операции брокера приходят в штуках.
    Без пересчёта сверка сравнивала разные единицы.
    """
    from gateway import marketdata

    ticker = marketdata.ticker_for(instrument_id)
    if ticker:
        entry = marketdata.resolve(ticker) or {}
        if entry.get("lot"):
            return int(entry["lot"])
    try:
        return int(client().instrument_by_uid(instrument_id).get("lot") or 1)
    except TInvestError:
        return 1


def unexplained(days: int = 1) -> list[dict]:
    """Операции по счёту, которым не нашлось нашей заявки.

    Сопоставление идёт по направлению, инструменту, количеству и времени.
    Прежняя версия брала первого попавшегося кандидата и не смотрела на
    количество — из-за этого отменённая заявка с нулевым исполнением могла
    «объяснить» настоящую покупку мимо шлюза, а одна заявка, исполненная
    биржей тремя частями, давала две ложные тревоги.
    """
    operations = [
        item
        for item in client().operations(days)
        if item.get("type") in TRADE_TYPES
        and item.get("state") == "OPERATION_STATE_EXECUTED"
    ]
    if not operations:
        return []

    conn = journal.connect()
    ours = conn.execute(
        "SELECT ts, figi, instrument_id, direction, lots, lots_executed FROM orders"
        " WHERE ts > ? AND COALESCE(lots_executed, 0) > 0",
        (time.time() - days * 86400 - MATCH_WINDOW,),
    ).fetchall()

    # Каждая наша заявка объясняет операции суммарно не больше своего
    # исполненного объёма: частичные исполнения складываются, лишнее
    # остаётся сиротой.
    budget = []
    for row in ours:
        lot = _lot_size(row["figi"] or "", row["instrument_id"])
        budget.append(
            {
                "ts": row["ts"],
                "figi": row["figi"] or "",
                "direction": row["direction"],
                "left": int(row["lots_executed"]) * max(lot, 1),
            }
        )

    orphans = []
    for operation in sorted(operations, key=lambda o: _parse_time(o.get("date", ""))):
        moment = _parse_time(operation.get("date", ""))
        figi = operation.get("figi") or ""
        side = _direction(operation.get("type", ""))
        quantity = abs(int(operation.get("quantity") or 0))

        candidates = [
            candidate
            for candidate in budget
            if candidate["left"] > 0
            and candidate["direction"] == side
            and (not figi or not candidate["figi"] or candidate["figi"] == figi)
            and (not moment or abs(candidate["ts"] - moment) <= MATCH_WINDOW)
        ]
        # Ближайшая по времени, а не первая попавшаяся.
        candidates.sort(key=lambda c: abs(c["ts"] - moment) if moment else 0)

        remaining = quantity
        for candidate in candidates:
            if remaining <= 0:
                break
            taken = min(candidate["left"], remaining)
            candidate["left"] -= taken
            remaining -= taken

        if remaining > 0:
            orphans.append(
                {
                    "date": operation.get("date"),
                    "type": operation.get("type"),
                    "figi": figi,
                    "quantity": remaining,
                    "payment": operation.get("payment"),
                }
            )

    return orphans


def check_and_alert(notify) -> int:
    """Сверить и сообщить о расхождениях. Возвращает число находок."""
    try:
        orphans = unexplained()
    except TInvestError as exc:
        journal.log_event("reconcile_failed", {"error": str(exc)[:300]})
        return 0

    if not orphans:
        journal.kv_set("reconcile_ok_ts", str(time.time()))
        return 0

    # Об одной и той же операции сообщаем один раз.
    # Список, а не множество: порядок у множества произволен, и обрезка
    # выбрасывала случайные записи — дедупликация могла «забыть» операцию
    # и разбудить оператора повторно.
    seen = [x for x in (journal.kv_get("reconcile_seen", "") or "").split("|") if x]
    known = set(seen)
    fresh = [o for o in orphans if f"{o['date']}:{o['figi']}" not in known]
    if not fresh:
        return 0

    seen.extend(f"{o['date']}:{o['figi']}" for o in fresh)
    journal.kv_set("reconcile_seen", "|".join(seen[-200:]))
    journal.log_event("reconcile_mismatch", {"operations": fresh})

    lines = [
        "⚠️ <b>Сделки мимо шлюза</b>",
        "",
        f"Найдено операций без нашей заявки: {len(fresh)}.",
        "Это значит, что торговля шла в обход ограничителей и журнала.",
        "",
    ]
    for item in fresh[:5]:
        side = "покупка" if _direction(item["type"]) == "buy" else "продажа"
        lines.append(
            f"{item['date'][:16].replace('T', ' ')} · {side} · {item['figi']} · "
            f"{item['quantity']} шт · {item['payment']} ₽"
        )
    notify("\n".join(lines))
    return len(fresh)
