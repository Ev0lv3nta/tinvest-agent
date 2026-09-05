"""MCP-сервер с торговыми инструментами.

Транспорт — stdio, протокол JSON-RPC 2.0 построчно. Codex запускает этот
процесс сам и глушит вместе с сессией; отдельного демона не требуется.

Окружение до подпроцесса MCP не доезжает, поэтому секреты читаются из
файлов через gateway.config.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import fcntl
import json
import math
import re
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from research.spec import FAMILIES

from . import config, guards, journal, marketdata, search
from .guards import GuardRejection
from .tinvest import OrderNotFound, SandboxClient, TInvestError

MSK = ZoneInfo("Europe/Moscow")
PROTOCOL_VERSION = "2024-11-05"

_client: SandboxClient | None = None


def client() -> SandboxClient:
    global _client
    if _client is None:
        _client = SandboxClient(
            config.secret("TINVEST_TOKEN"), config.secret("TINVEST_ACCOUNT_ID")
        )
    return _client


# --- вспомогательное ------------------------------------------------------


def _parse_when(value: str) -> float:
    """Принимает '+30m', '+2h', '2026-08-24T16:45' (МСК) или ISO с зоной."""
    text = (value or "").strip()
    relative = re.fullmatch(r"\+(\d+)\s*([mhd])", text, re.IGNORECASE)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2).lower()
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
        return (datetime.now(tz=MSK) + delta).timestamp()

    normalized = text.replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"не понял время {value!r}; ожидается '+30m', '+2h' или "
            f"'2026-08-24T16:45' по московскому времени"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK)
    return parsed.timestamp()


def _report_path(path: str) -> Path:
    """Отчёт берётся только из notes/reports и только markdown.

    Инструмент принимает путь от модели и отправляет файл в Telegram. Без
    ограничения это способ выгрузить наружу любой читаемый файл, включая
    секреты, — а токен брокера лежит на этой же машине.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = config.REPORTS_DIR / candidate
    resolved = candidate.resolve()
    root = config.REPORTS_DIR.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"отчёт должен лежать в {root}, получено: {resolved}")
    if resolved.suffix.lower() != ".md":
        raise ValueError("отчёт должен быть markdown-файлом (.md)")
    if not resolved.is_file():
        raise ValueError(f"файл не найден: {resolved}")
    size = resolved.stat().st_size
    if size > config.MAX_REPORT_BYTES:
        raise ValueError(
            f"файл {size} байт при пределе {config.MAX_REPORT_BYTES}"
        )
    return resolved


def _telegram_document(path: str, caption: str) -> bool:
    """Отправка файла в Telegram: multipart собираем вручную, без зависимостей."""
    token = config.secret("TELEGRAM_BOT_TOKEN")
    chat_id = config.secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    file_path = _report_path(path)

    boundary = f"----tinvest{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in (("chat_id", chat_id), ("caption", caption[:1000])):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\";"
        f" filename=\"{file_path.name}\"\r\n"
        f"Content-Type: text/markdown\r\n\r\n".encode()
    )
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=b"".join(parts),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read()).get("ok", False)
    except Exception:
        return False


def _telegram(text: str) -> bool:
    token = config.secret("TELEGRAM_BOT_TOKEN")
    chat_id = config.secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
    ).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read()).get("ok", False)
    except Exception:
        return False


def _resolve(instrument_id: str) -> dict:
    """Тикер, FIGI и размер лота. Справочник на диске, иначе точный вызов API."""
    ticker = marketdata.ticker_for(instrument_id)
    if ticker:
        entry = marketdata.resolve(ticker) or {}
        if entry.get("figi"):
            return {"ticker": ticker, "figi": entry["figi"], "lot": entry.get("lot", 1)}
    info = client().instrument_by_uid(instrument_id)
    if info.get("ticker"):
        marketdata.remember_instrument(info)
    return {
        "ticker": info.get("ticker") or instrument_id[:8],
        "figi": info.get("figi") or "",
        "lot": int(info.get("lot") or 1),
    }


def _atr_15m(instrument_id: str, ticker: str) -> float:
    """Шум бумаги в единицах цены. Файл кешируется, лишнего запроса нет.

    Недоступность данных — причина отказать во входе, а не работать без
    барьера. Раньше любая ошибка возвращала ноль, а ноль в `check_trade_card`
    означает «ATR не задан, проверку пропускаем»: сбой свечей выключал
    обязательный фильтр вместо того, чтобы остановить сделку. Отсутствие
    обязательного признака — это отсутствие основания для входа.
    """
    try:
        summary = marketdata.candles(
            client(), instrument_id, "CANDLE_INTERVAL_15_MIN", 3, name=ticker
        )
    except Exception as exc:  # noqa: BLE001 — источник может лежать
        raise GuardRejection(
            f"Отклонено: не удалось получить свечи {ticker} для расчёта ATR "
            f"({type(exc).__name__}: {exc}). Барьер по шуму обязателен, а без "
            f"данных он не считается. Повтори позже или проверь quote."
        ) from exc
    noise = float(summary.get("atr14") or 0.0)
    closed = int(summary.get("closed_bars") or 0)
    if noise <= 0 or closed < 15:
        raise GuardRejection(
            f"Отклонено: ATR(15м) по {ticker} не посчитан — закрытых свечей "
            f"{closed}, ATR {noise}. Барьер по шуму обязателен: без него стоп "
            f"нечем сравнить с обычным движением бумаги."
        )
    return noise


def _resolve_pending(instrument_id: str, ticker: str, direction: str) -> None:
    """Не отправлять новую заявку, пока не выяснена судьба предыдущей.

    Выяснение — это точный запрос состояния по идентификатору, и только он.
    Раньше вывод делался из отсутствия заявки в списке активных, а если
    брокер вообще не отвечал, через пять минут намерение объявлялось
    потерянным и вход разрешался. То есть недоступность API работала как
    разрешение удвоить позицию — ровно в тот момент, когда проверить ничего
    нельзя.

    Теперь неизвестность не истекает по таймеру. Она снимается ответом
    брокера, и ничем другим.
    """
    # Незакрытая покупка не должна мешать продать: выход разрешён всегда.
    pending = [
        intent
        for intent in journal.blocking_intents(instrument_id)
        if intent["direction"] == direction
    ]
    if not pending:
        return

    blocking = []
    for intent in pending:
        why = _resolve_intent(intent, ticker)
        if why:
            blocking.append((intent["request_id"], why))

    if blocking:
        request_id, why = blocking[0]
        raise GuardRejection(
            f"Отклонено: по {ticker} есть заявка {request_id}, судьба которой не "
            f"закрыта — {why}. Пока она не выяснена, вторая заявка в ту же "
            f"сторону запрещена: если первая исполнилась, это удвоение позиции.\n"
            f"Посмотри order_state('{request_id}'), active_orders и operations. "
            f"Если заявка активна — сними её через cancel_order. Если брокер "
            f"недоступен, подожди и повтори проверку; сама по себе пауза "
            f"неизвестность не снимает."
        )


def _resolve_intent(intent, ticker: str) -> str:
    """Выяснить судьбу намерения. Пустая строка — выяснена, можно дальше.

    Возвращаемый текст объясняет, почему намерение всё ещё блокирует.
    """
    request_id = intent["request_id"]
    order_id = (intent["order_id"] or "") if "order_id" in intent.keys() else ""
    try:
        # По биржевому идентификатору, если он у нас есть, иначе по своему
        # ключу: брокер принимает оба, но должен знать, какой передан.
        state = client().order_state(order_id or request_id, by_request_id=not order_id)
    except OrderNotFound:
        # Брокер ответил, что такой заявки нет. Это содержательный ответ, но
        # доверять ему в одиночку нельзя: сверяем с операциями по счёту.
        if time.time() - intent["ts"] < SETTLE_SECONDS:
            # Операции появляются у брокера не мгновенно. Сразу после
            # отправки «нет заявки и нет операции» ещё не отличается от
            # «только что исполнилась и не доехала». Пауза здесь работает
            # в сторону запрета, а не разрешения.
            return (
                "прошло слишком мало времени, чтобы отличить неотправленную "
                "заявку от только что исполненной"
            )
        evidence = _fill_evidence(intent)
        if evidence == "none":
            journal.set_intent_state(request_id, "not_placed", "брокер такой заявки не знает")
            return ""
        if evidence == "unknown":
            return "брокер такой заявки не знает, но операции по счёту не читаются"
        journal.log_event(
            "intent_contradiction",
            {
                "request_id": request_id,
                "ticker": ticker,
                "detail": "нет заявки, но есть операция",
            },
        )
        return (
            "брокер не находит заявку, но по бумаге есть исполненная операция "
            "в ту же сторону — расхождение, нужен разбор"
        )
    except TInvestError as exc:
        return f"состояние заявки не читается ({exc})"

    status = state.get("status") or ""
    exchange_id = state.get("order_id") or order_id or request_id
    _recover_order(intent, state, exchange_id, status)
    journal.update_order_status(
        exchange_id, status, state.get("lots_executed") or 0, state.get("price") or 0.0
    )
    if journal.is_terminal(status):
        journal.set_intent_state(
            request_id, _INTENT_BY_STATUS.get(status, "filled"), status,
            order_id=state.get("order_id") or "",
        )
        return ""
    journal.set_intent_state(
        request_id, "live", status, order_id=state.get("order_id") or ""
    )
    if status in journal.ACTIVE_ORDER_STATUS:
        executed = state.get("lots_executed") or 0
        requested = state.get("lots_requested") or 0
        detail = f" (исполнено {executed} из {requested})" if executed else ""
        return f"висит активной заявкой{detail}"
    return f"у брокера в состоянии {status or 'неопределённом'}"


# Сколько ждать, прежде чем «заявки нет» станет выводом. Это не таймер
# истечения неизвестности: по его окончании ничего не разрешается само —
# только начинает учитываться ответ брокера об отсутствии заявки.
SETTLE_SECONDS = 30.0

_INTENT_BY_STATUS = {
    "EXECUTION_REPORT_STATUS_FILL": "filled",
    "EXECUTION_REPORT_STATUS_CANCELLED": "cancelled",
    "EXECUTION_REPORT_STATUS_REJECTED": "rejected",
}


def _recover_order(intent, state: dict, exchange_id: str, status: str) -> None:
    """Записать заявку, о которой мы узнали только из ответа на запрос.

    Если ответ на отправку потерялся, `log_order` не вызывался и заявки в
    журнале нет. Когда она потом находится у брокера, обновлять нечего:
    UPDATE молча не трогает ни одной строки, а позиция при этом уже есть.
    Дальше её не видят ни дневные лимиты, ни расчёт совокупного риска, ни
    сверка — то есть сделка существует только на счёте.
    """
    if journal.order_exists(exchange_id):
        return
    journal.log_order(
        {
            "order_id": exchange_id,
            "request_id": intent["request_id"],
            "instrument_id": intent["instrument_id"],
            "figi": state.get("figi") or "",
            "ticker": intent["ticker"] or "",
            "direction": intent["direction"],
            "order_type": intent["order_type"],
            "lots": intent["lots"],
            "lots_executed": state.get("lots_executed") or 0,
            "price": state.get("price") or 0.0,
            "requested_price": intent["price"],
            "total": state.get("total") or 0.0,
            "status": status,
            "card": json.loads(intent["card"]) if intent["card"] else None,
            "raw": {"recovered": True},
        },
        rationale="восстановлена по ответу брокера: отправка прошла, ответ не дошёл",
    )
    journal.log_event(
        "order_recovered",
        {"request_id": intent["request_id"], "order_id": exchange_id, "status": status},
    )


def _fill_evidence(intent) -> str:
    """Есть ли по бумаге исполненная операция в ту же сторону после намерения.

    Возвращает `none`, `found` или `unknown`. Различие между «операции нет» и
    «операции не читаются» здесь и есть смысл функции: раньше сбой чтения
    возвращал False и означал «ничего не было».

    Направление сравнивается обязательно. Раньше подходила любая операция по
    бумаге, и исполненная продажа закрывала неопределённую покупку.
    """
    ticker = intent["ticker"] or marketdata.ticker_for(intent["instrument_id"]) or ""
    figi = (marketdata.resolve(ticker) or {}).get("figi", "")
    if not figi:
        # Без FIGI операцию не с чем сопоставить: подошла бы любая по счёту.
        # Совпадение со всем подряд — не совпадение, это незнание.
        return "unknown"
    want = intent["direction"]
    try:
        operations = client().operations(1)
    except TInvestError:
        return "unknown"
    for item in operations:
        if item.get("state") != "OPERATION_STATE_EXECUTED":
            continue
        if figi and item.get("figi") and item.get("figi") != figi:
            continue
        kind = str(item.get("type") or "")
        side = "sell" if "SELL" in kind else "buy" if "BUY" in kind else ""
        if side != want:
            continue
        moment = _iso_ts(item.get("date", ""))
        if moment and moment >= intent["ts"] - 60:
            return "found"
    return "none"


def _iso_ts(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


# Счётом в каждый момент распоряжается кто-то один. Блокировка файловая, а
# не в памяти процесса: заявку отправляет и шлюз по просьбе модели, и
# сторож позиций из супервизора — это разные процессы, и `threading.Lock`
# между ними ничего не значит. Два процесса увидели бы одни и те же
# свободные деньги и оба прошли бы проверку.
ACCOUNT_LOCK = Path(config.DB_PATH).parent / "account.lock"


@contextlib.contextmanager
def account_lock(timeout: float = 30.0):
    ACCOUNT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = ACCOUNT_LOCK.open("a+")
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise GuardRejection(
                        "Счёт занят другой операцией дольше "
                        f"{timeout:.0f} с. Повтори чуть позже: одновременных "
                        "заявок по одному счёту быть не должно."
                    )
                time.sleep(0.2)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def _order(
    direction: str,
    instrument_id: str,
    lots: int,
    price,
    rationale: str,
    card: dict | None = None,
) -> dict:
    with account_lock():
        return _order_locked(direction, instrument_id, lots, price, rationale, card)


def _order_locked(
    direction: str,
    instrument_id: str,
    lots: int,
    price,
    rationale: str,
    card: dict | None = None,
) -> dict:
    api = client()
    buying = direction == "ORDER_DIRECTION_BUY"
    market = price is None

    instrument = _resolve(instrument_id)
    ticker, figi, lot_size = instrument["ticker"], instrument["figi"], instrument["lot"]
    _resolve_pending(instrument_id, ticker, "buy" if buying else "sell")

    checks: dict = {}
    if buying:
        # Порядок важен: остановка прогона, дневной стоп и лимит частоты
        # закрывают вход, но не выход. Продажа и снятие заявки доступны
        # всегда — иначе защита срабатывает ровно тогда, когда избавиться
        # от риска нужнее всего. Лимит частоты стоял общим и отбивал
        # продажу наравне с покупкой.
        guards.check_rate_limit()
        guards.check_entry_allowed()
        portfolio = api.portfolio()
        journal.log_snapshot(portfolio["total"], portfolio["cash"], portfolio["positions"])
        guards.check_capital_floor(portfolio["total"])
        guards.check_daily_loss(portfolio["total"])
        pending_buys = {
            order.get("instrument_id")
            for order in api.active_orders()
            if str(order.get("direction", "")).endswith("BUY")
        }
        guards.check_no_pyramiding(portfolio, instrument_id, ticker)
        guards.check_positions(portfolio, instrument_id, pending_buys)
        guards.check_entries_today()
        guards.check_cooldown(instrument_id, ticker)

        entry = float(price) if price is not None else 0.0
        if not entry:
            prices = api.last_price([instrument_id])
            entry = float(prices[0]["price"]) if prices else 0.0
        if not entry:
            raise GuardRejection(
                f"Нет текущей цены {ticker} — вероятно, вне торгов. "
                f"Проверь quote перед заявкой."
            )
        checks = guards.check_trade_card(
            entry=entry,
            stop=float(card["stop"]),
            target=float(card["target"]),
            lots=lots,
            lot_size=lot_size,
            ticker=ticker,
            atr=_atr_15m(instrument_id, ticker),
        )
        guards.check_portfolio_heat(checks["risk_rub"], journal.open_risk(), ticker)
        # Базовая ставка берётся из реестра, а не из текста заявки: в карточку
        # попадает то, что было измерено и записано ДО сделки.
        record = guards.check_playbook(
            str((card or {}).get("playbook", "")), checks["risk_rub"]
        )
        checks["playbook_status"] = record["status"]
        checks["base_rate"] = {
            "trades": record["trades"],
            "wins": record["wins"],
            "avg_r": record["avg_r"],
            "measured_on": record["measured_on"],
        }
        checks["entry"] = round(entry, 4)

    limits = api.max_lots(instrument_id, price)
    if buying:
        guards.check_buy(lots, limits, ticker, market)
    else:
        guards.check_sell(lots, limits, ticker)

    # Намерение записывается ДО сети: ключ идемпотентности должен пережить
    # потерю ответа, иначе повтор создаст вторую заявку.
    request_id = str(uuid.uuid4())
    journal.create_intent(
        {
            "request_id": request_id,
            "instrument_id": instrument_id,
            "ticker": ticker,
            "direction": "buy" if buying else "sell",
            "order_type": "market" if market else "limit",
            "lots": lots,
            "price": price,
            "card": {**(card or {}), **checks} if buying else None,
        }
    )

    try:
        result = api.post_order(instrument_id, lots, direction, price, order_id=request_id)
    except TInvestError as exc:
        if exc.answered:
            # Брокер разобрал запрос и отказал: заявки у него нет.
            journal.set_intent_state(request_id, "rejected", str(exc))
            raise
        # Ответа не было. Заявка могла дойти и исполниться: единственный
        # честный вывод — «неизвестно». Ключ остаётся открытым и блокирует
        # повтор, пока состояние не будет прочитано по идентификатору.
        journal.set_intent_state(request_id, "unknown", str(exc))
        raise GuardRejection(
            f"Связь с брокером прервалась ({exc}). Заявка могла быть принята — "
            f"её ключ {request_id}. Повторять вслепую нельзя: новый ключ создаст "
            f"вторую заявку поверх возможно исполненной первой.\n"
            f"Проверь order_state('{request_id}'). Пока состояние неизвестно, "
            f"вход по {ticker} в эту сторону заблокирован."
        ) from exc
    except Exception as exc:  # noqa: BLE001 — любой иной сбой тоже неизвестность
        journal.set_intent_state(request_id, "unknown", repr(exc))
        raise GuardRejection(
            f"Отправка заявки прервалась ({type(exc).__name__}: {exc}). Судьба "
            f"заявки {request_id} неизвестна — проверь order_state перед любым "
            f"повтором."
        ) from exc

    journal.set_intent_state(
        request_id, "sent", result.get("status", ""), order_id=result.get("order_id") or ""
    )
    journal.log_order(
        {
            "order_id": result["order_id"],
            "request_id": request_id,
            "instrument_id": instrument_id,
            "figi": figi,
            "ticker": ticker,
            "direction": "buy" if buying else "sell",
            "order_type": "limit" if price is not None else "market",
            "lots": lots,
            "lots_executed": result["lots_executed"],
            "price": result["price"],
            "requested_price": price,
            "total": result["total"],
            "status": result["status"],
            "card": {**(card or {}), **checks} if buying else None,
            "raw": result["raw"],
        },
        rationale=rationale,
    )
    if buying:
        # Обязательство создаётся сразу после отправки, а не после
        # исполнения: лимитка может исполниться в любой момент, а позиция
        # без стопа не должна существовать даже минуту.
        journal.add_mandate(
            {
                "instrument_id": instrument_id,
                "ticker": ticker,
                "request_id": request_id,
                "stop": float(card["stop"]),
                "target": float(card["target"]),
                "deadline_ts": _deadline(card),
            }
        )
    if not buying:
        _close_mandate_if_flat(api, instrument_id, ticker)
    if checks:
        result["checks"] = checks
    return result


# Сколько живёт идея, если срок не назван. Позиция без срока — это позиция,
# про которую забыли: тезис или сработал, или не сработал, а «подождём ещё»
# не является третьим исходом.
DEFAULT_HOLD_HOURS = 8


def _deadline(card: dict) -> float:
    hours = card.get("hold_hours")
    try:
        hours = float(hours) if hours else DEFAULT_HOLD_HOURS
    except (TypeError, ValueError):
        hours = DEFAULT_HOLD_HOURS
    hours = max(0.25, min(hours, 24 * 14))
    return time.time() + hours * 3600


def _close_mandate_if_flat(api, instrument_id: str, ticker: str) -> None:
    """После продажи проверить, осталась ли позиция. Нет — снять обязательство."""
    try:
        portfolio = api.portfolio()
    except Exception:  # noqa: BLE001 — не смогли проверить, оставляем как есть
        return
    left = next(
        (
            position.get("quantity") or 0
            for position in portfolio.get("positions", [])
            if position.get("instrument_id") == instrument_id
        ),
        0,
    )
    if left <= 0:
        journal.close_mandate_for(instrument_id, "позиция закрыта")


# --- инструменты ----------------------------------------------------------

TOOLS: list[dict] = []
HANDLERS: dict[str, Callable[..., Any]] = {}


def tool(name: str, description: str, schema: dict):
    def decorator(func):
        TOOLS.append({"name": name, "description": description, "inputSchema": schema})
        HANDLERS[name] = func
        return func

    return decorator


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


@tool(
    "portfolio",
    "Состав портфеля: свободные деньги, позиции, текущая переоценка. "
    "Срез автоматически попадает в журнал.",
    _obj({}),
)
def tool_portfolio() -> dict:
    data = client().portfolio()
    journal.log_snapshot(data["total"], data["cash"], data["positions"])
    # К каждой позиции — действующее обязательство: стоп, цель и срок, по
    # которым из неё выйдет код, если ты не успеешь.
    for position in data["positions"]:
        mandate = journal.mandate_for(position.get("instrument_id") or "")
        if mandate is None:
            continue
        position["mandate"] = {
            "stop": mandate["stop"],
            "target": mandate["target"],
            "deadline": datetime.fromtimestamp(
                mandate["deadline_ts"], tz=MSK
            ).strftime("%d.%m %H:%M") if mandate["deadline_ts"] else None,
            "latched": bool(mandate["latched_ts"]),
        }
    data["result_vs_start"] = round(data["total"] - config.STARTING_CAPITAL, 2)
    data["result_percent"] = round(
        (data["total"] / config.STARTING_CAPITAL - 1) * 100, 2
    )
    data["capital_floor"] = config.CAPITAL_FLOOR
    if guards.halted():
        data["halted"] = guards.halted()
    return data


@tool(
    "find_instrument",
    "Поиск инструмента по тикеру или названию. ВАЖНО: без параметра kind "
    "выдачу занимают облигации — даже точный запрос 'SBER' вернёт бонды. "
    "Для акций всегда указывай kind='INSTRUMENT_TYPE_SHARE'. "
    "Названия в API отличаются от разговорных: акция Сбербанка называется "
    "'Сбер Банк', поэтому запрос 'Сбербанк' слитно её не находит. "
    "Найденные тикеры запоминаются на сутки — повторный поиск бесплатен.",
    _obj(
        {
            "query": {"type": "string", "description": "тикер или часть названия"},
            "kind": {
                "type": "string",
                "description": "INSTRUMENT_TYPE_SHARE, _BOND, _ETF, _FUTURES, _CURRENCY",
            },
            "limit": {"type": "integer", "description": "по умолчанию 10"},
        },
        ["query"],
    ),
)
def tool_find_instrument(query: str, kind: str = "", limit: int = 10) -> list[dict]:
    limit = max(1, min(int(limit or 10), 50))
    cached = marketdata.resolve(query)
    if cached and kind in ("", "INSTRUMENT_TYPE_SHARE"):
        return [
            {
                "instrument_id": cached["uid"],
                "figi": cached.get("figi", ""),
                "ticker": query.upper(),
                "name": cached.get("name", ""),
                "lot": cached.get("lot", 1),
                "cached": True,
            }
        ]
    found = client().find_instrument(query, limit=limit, kind=kind)
    for item in found[:1]:
        marketdata.remember_instrument(item)
    return found


@tool(
    "quote",
    "Последняя цена и статус торгов. Принимает список: цены на несколько "
    "бумаг приходят одним запросом, поштучно спрашивать не надо. "
    "Статус возвращается только для одиночного инструмента — если он не "
    "SECURITY_TRADING_STATUS_NORMAL_TRADING, заявки отбиваются кодом 30079.",
    _obj(
        {
            "instrument_id": {"type": "string"},
            "instrument_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "до 50 идентификаторов за раз",
            },
        }
    ),
)
def tool_quote(instrument_id: str = "", instrument_ids: list | None = None):
    ids = [str(x) for x in (instrument_ids or []) if x]
    if instrument_id:
        ids.insert(0, instrument_id)
    if not ids:
        raise ValueError("нужен instrument_id или instrument_ids")
    if len(ids) > 50:
        raise ValueError("не больше 50 инструментов за вызов")

    prices = client().last_price(ids)
    if len(ids) == 1:
        status = client().trading_status(ids[0])
        found = prices[0] if prices else {}
        return {
            "price": found.get("price"),
            "ticker": found.get("ticker"),
            # Время биржи, а не наше: по нему видно, свежая котировка или
            # висит с прошлой сессии.
            "price_time": found.get("time"),
            **status,
        }
    return {
        "prices": {
            item.get("ticker") or item.get("instrument_id"): item.get("price")
            for item in prices
        },
        "time": prices[0].get("time") if prices else None,
        "note": "статус торгов спрашивай отдельно по конкретной бумаге перед заявкой",
    }


@tool(
    "market_snapshot",
    "Широкий срез рынка одной таблицей: по каждой бумаге цена, движение к "
    "вчерашнему закрытию и это же движение в дневных ATR. Стоит один "
    "батч-запрос независимо от числа бумаг — рынок смотреть надо им, а не "
    "перебором тикеров поштучно. Без аргументов берёт бумаги, которые уже "
    "встречались в работе.",
    _obj(
        {
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "тикеры, например ['SBER','LKOH']; до 50",
            }
        }
    ),
)
def tool_market_snapshot(tickers: list | None = None) -> dict:
    api = client()
    names = [str(t).upper() for t in (tickers or []) if t]
    if not names:
        names = sorted(marketdata.load_universe())
    if not names:
        raise ValueError("справочник пуст — передай список тикеров, дальше он запомнится")
    names = names[:50]

    resolved: dict[str, str] = {}
    for ticker in names:
        entry = marketdata.resolve(ticker)
        if not entry:
            found = api.find_instrument(ticker, limit=1, kind="INSTRUMENT_TYPE_SHARE")
            if not found:
                continue
            marketdata.remember_instrument(found[0])
            entry = marketdata.resolve(ticker)
        if entry:
            resolved[ticker] = entry["uid"]
    if not resolved:
        raise ValueError("ни один тикер не разрешился в инструмент")

    prices = {
        item.get("instrument_id"): item.get("price")
        for item in api.last_price(list(resolved.values()))
    }

    rows = []
    for ticker, uid in resolved.items():
        last = prices.get(uid)
        ref = marketdata.reference(api, ticker, uid)
        previous = ref.get("prev_close") or 0.0
        noise = ref.get("atr_day") or 0.0
        change = (last - previous) if (last and previous) else 0.0
        rows.append(
            {
                "ticker": ticker,
                "last": last,
                "chg_pct": round(change / previous * 100, 2) if previous else None,
                # Движение, поделённое на обычный дневной размах: полпроцента
                # у спокойной бумаги и у волатильной — разные события.
                "atr_x": round(change / noise, 2) if noise else None,
            }
        )
    rows.sort(key=lambda r: abs(r.get("atr_x") or 0), reverse=True)
    return {
        "as_of": datetime.now(MSK).strftime("%Y-%m-%d %H:%M"),
        "instruments": len(rows),
        "note": "chg_pct — к вчерашнему закрытию; atr_x — то же движение в дневных ATR",
        "rows": rows,
    }


@tool(
    "candles",
    "Исторические свечи. Серия пишется в CSV на диск, в ответ идёт путь и "
    "сводка: O/H/L/C, VWAP, ATR(14), диапазон открытия, положение цены в "
    "диапазоне. Считай по файлу своим кодом — pandas читает его напрямую. "
    "Сырую таблицу в контекст не тяни: минутные свечи за день это около "
    "38 тысяч токенов, которые останутся в сессии до самого сжатия. "
    "Повторный запрос за тот же интервал берёт файл с диска, не ходя в API. "
    "interval: CANDLE_INTERVAL_1_MIN, _5_MIN, _15_MIN, _HOUR, _DAY, _WEEK.",
    _obj(
        {
            "instrument_id": {"type": "string"},
            "interval": {"type": "string"},
            "days": {"type": "integer", "description": "глубина в днях"},
            "tail": {
                "type": "integer",
                "description": "сколько последних свечей вернуть строками, 0-50",
            },
            "refresh": {
                "type": "boolean",
                "description": "перекачать, не беря файл из кеша",
            },
        },
        ["instrument_id", "interval", "days"],
    ),
)
def tool_candles(
    instrument_id: str,
    interval: str,
    days: int,
    tail: int = 0,
    refresh: bool = False,
) -> dict:
    days = max(1, min(int(days), 1830))
    tail = max(0, min(int(tail or 0), 50))
    return marketdata.candles(
        client(),
        instrument_id,
        interval,
        days,
        name=marketdata.ticker_for(instrument_id) or instrument_id,
        tail=tail,
        refresh=bool(refresh),
    )


@tool(
    "orderbook",
    "Биржевой стакан. Вне торговой сессии приходит пустым.",
    _obj(
        {"instrument_id": {"type": "string"}, "depth": {"type": "integer"}},
        ["instrument_id"],
    ),
)
def tool_orderbook(instrument_id: str, depth: int = 10) -> dict:
    return client().orderbook(instrument_id, depth)


@tool(
    "max_lots",
    "Сколько лотов можно купить на свободные деньги и продать из портфеля. "
    "Значения без учёта маржи — именно по ним режутся заявки.",
    _obj(
        {"instrument_id": {"type": "string"}, "price": {"type": "number"}},
        ["instrument_id"],
    ),
)
def tool_max_lots(instrument_id: str, price: float | None = None) -> dict:
    return client().max_lots(instrument_id, price)


@tool(
    "buy",
    "Купить. Без price — по рынку, с price — лимитная заявка. Количество "
    "в ЛОТАХ, не в штуках.\n"
    "Вход требует карточки сделки: стоп, цель, плейбук, базовая ставка. "
    "Это не бюрократия — по этим числам шлюз считает три барьера и режет "
    "сделки, убыточные арифметически до всякого рынка: соотношение "
    "прибыль/риск ниже 2, цель меньше десяти круговых комиссий, стоп внутри "
    "шума (ближе 1.5 ATR). Размер позиции тоже считается от стопа, а не от "
    "свободных денег: риск на идею не больше 1000 ₽.\n"
    "Барьер нельзя обойти, изменив числа задним числом: карточка попадает в "
    "журнал вместе с заявкой.\n"
    "playbook — имя сетапа из реестра. Незарегистрированный сетап торговать "
    "нельзя: зарегистрируй его через register_playbook до сделки. Сетап без "
    "накопленной статистики торгуется четвертью размера.",
    _obj(
        {
            "instrument_id": {"type": "string"},
            "lots": {"type": "integer"},
            "price": {"type": "number", "description": "цена лимитной заявки"},
            "stop": {
                "type": "number",
                "description": "цена, ниже которой тезис опровергнут и ты выходишь",
            },
            "target": {"type": "number", "description": "первая цель по цене"},
            "playbook": {
                "type": "string",
                "description": "имя сетапа из реестра (playbooks)",
            },
            "hold_hours": {
                "type": "number",
                "description": "сколько часов живёт идея; по умолчанию 8. "
                               "По истечении срока позиция закрывается кодом",
            },
            "rationale": {
                "type": "string",
                "description": "тезис: что должно произойти и почему рынок этого ещё не учёл",
            },
        },
        ["instrument_id", "lots", "stop", "target", "playbook", "rationale"],
    ),
)
def tool_buy(
    instrument_id: str,
    lots: int,
    stop: float,
    target: float,
    playbook: str,
    rationale: str,
    price: float | None = None,
    hold_hours: float | None = None,
) -> dict:
    if not str(playbook).strip():
        raise ValueError("playbook обязателен: торгуются только зарегистрированные сетапы")
    card = {
        "stop": float(stop),
        "target": float(target),
        "playbook": str(playbook)[:80],
        "hold_hours": hold_hours,
    }
    return _order("ORDER_DIRECTION_BUY", instrument_id, lots, price, rationale, card)


@tool(
    "sell",
    "Продать. Без price — по рынку, с price — лимитная заявка. Количество "
    "в ЛОТАХ. Обоснование обязательно. Продажа сверх позиции отклоняется: "
    "шорт запрещён. Выход не ограничен ни дневным стопом, ни остановкой "
    "прогона — избавиться от риска можно всегда.",
    _obj(
        {
            "instrument_id": {"type": "string"},
            "lots": {"type": "integer"},
            "price": {"type": "number"},
            "rationale": {
                "type": "string",
                "description": "почему продаёшь: сработал тезис, стоп, истёк срок идеи",
            },
        },
        ["instrument_id", "lots", "rationale"],
    ),
)
def tool_sell(instrument_id: str, lots: int, rationale: str, price: float | None = None) -> dict:
    return _order("ORDER_DIRECTION_SELL", instrument_id, lots, price, rationale)


@tool("active_orders", "Неисполненные заявки.", _obj({}))
def tool_active_orders() -> list[dict]:
    return client().active_orders()


@tool(
    "cancel_order",
    "Снять неисполненную заявку.",
    _obj({"order_id": {"type": "string"}}, ["order_id"]),
)
def tool_cancel_order(order_id: str) -> dict:
    """Снять заявку и прочитать, чем она на самом деле кончилась.

    Снятие — это гонка с исполнением. Подтверждение отмены не доказывает,
    что исполнено ноль: часть могла уйти, пока запрос шёл. Поэтому после
    отмены состояние читается точно, а не записывается по факту отправки.
    """
    with account_lock():
        intent = journal.intent_by_request(order_id)
        cancelled_at = client().cancel_order(order_id, by_request_id=bool(intent))
        result: dict[str, Any] = {"cancelled_at": cancelled_at}
        return _after_cancel(order_id, intent, result)


def _after_cancel(order_id: str, intent, result: dict) -> dict:
    try:
        state = client().order_state(order_id, by_request_id=bool(intent))
    except TInvestError as exc:
        # Отмену подтвердили, но чем кончилась заявка — неизвестно.
        if intent:
            journal.set_intent_state(
                order_id, "unknown", f"снята, состояние не прочитано: {exc}"
            )
        result["warning"] = (
            f"Отмена принята, но состояние заявки не прочиталось ({exc}). "
            f"Сколько лотов успело исполниться — неизвестно; проверь portfolio "
            f"и order_state перед новой заявкой."
        )
        return result

    status = state.get("status") or "EXECUTION_REPORT_STATUS_CANCELLED"
    executed = state.get("lots_executed") or 0
    journal.update_order_status(
        state.get("order_id") or order_id, status, executed, state.get("price") or 0.0
    )
    if intent:
        journal.set_intent_state(
            order_id,
            _INTENT_BY_STATUS.get(status, "cancelled")
            if journal.is_terminal(status)
            else "live",
            status,
            order_id=state.get("order_id") or "",
        )
    result["status"] = status
    result["lots_executed"] = executed
    if executed:
        result["note"] = (
            f"До отмены успело исполниться {executed} лот(ов) — позиция "
            f"изменилась, свободные лоты пересчитай по portfolio."
        )
    return result


@tool(
    "tighten_stop",
    "Подтянуть стоп по открытой позиции вверх. Опустить нельзя: условие, "
    "записанное до сигнала, внутри дня не смягчается — иначе позиция падает "
    "дальше, чем было решено, когда думалось спокойно.\n"
    "Стоп исполняет код: если цена его пройдёт, позиция закроется без твоего "
    "участия, даже если ты в этот момент занят другим.",
    _obj({"instrument_id": {"type": "string"}, "stop": {"type": "number"}},
         ["instrument_id", "stop"]),
)
def tool_tighten_stop(instrument_id: str, stop: float) -> dict:
    if not math.isfinite(float(stop)) or float(stop) <= 0:
        raise ValueError("стоп должен быть конечным положительным числом")
    mandate = journal.tighten_mandate(instrument_id, float(stop))
    if mandate is None:
        raise ValueError(
            "по этой бумаге нет открытого обязательства — позиции нет либо "
            "она уже закрыта"
        )
    return {
        "ticker": mandate["ticker"],
        "stop": mandate["stop"],
        "target": mandate["target"],
        "latched": bool(mandate["latched_ts"]),
    }


@tool(
    "backtest",
    "Прогнать правило по истории и получить честный ответ «да» или «нет».\n"
    "Считает один и тот же код: выбор кандидата только на обучающей части, "
    "зазор перед проверкой, двойные издержки, вход на бар позже, сравнение с "
    "«ничего не делать» и «купил и держал», нижняя граница с поправкой на "
    "число уже сделанных попыток.\n"
    "Каждый вызов — зарегистрированная попытка. Чем больше вариантов "
    "перебрано, тем строже порог: перебор не бесплатен, и это не наказание, "
    "а арифметика.\n"
    "С register_as результат записывается в реестр сетапов числами, которые "
    "посчитал код. Только такой сетап торгуется полным размером.",
    _obj(
        {
            "tickers": {"type": "array", "items": {"type": "string"}},
            "family": {"type": "string", "enum": list(FAMILIES)},
            "interval": {"type": "string"},
            "days": {"type": "integer", "description": "глубина истории"},
            "lookback": {"type": "integer"},
            "stop_atr": {"type": "number"},
            "target_atr": {"type": "number"},
            "register_as": {
                "type": "string",
                "description": "имя сетапа, под которым записать результат в реестр",
            },
        },
        ["tickers", "family"],
    ),
)
def tool_backtest(
    tickers: list,
    family: str,
    interval: str = "CANDLE_INTERVAL_HOUR",
    days: int = 180,
    lookback: int = 20,
    stop_atr: float = 1.5,
    target_atr: float = 3.0,
    register_as: str = "",
) -> dict:
    from research import trials
    from research.bars import load_csv
    from research.evaluate import Protocol, evaluate
    from research.history import fetch
    from research.replay import Costs, Rules
    from research.spec import Spec

    minutes = marketdata.INTERVAL_MINUTES.get(interval)
    if not minutes:
        raise ValueError(f"неизвестный интервал {interval!r}")
    names = [str(t).upper() for t in tickers if str(t).strip()]
    if not names:
        raise ValueError("нужен хотя бы один тикер")

    # История качается окнами: брокер отдаёт ограниченный отрезок за запрос,
    # и полгода часовых свечей одним куском он просто отбивает. Файл живёт
    # час — перекачивать полгода ради последнего бара незачем.
    bars = []
    missing = []
    history = marketdata.data_dir() / "history"
    for ticker in names:
        entry = marketdata.resolve(ticker)
        if not entry:
            missing.append(ticker)
            continue
        path = history / f"{ticker}_{interval[16:].lower()}_{days}d.csv"
        fetch(client(), entry["uid"], interval, days, path, max_age=3600)
        bars.extend(load_csv(path, ticker, minutes))
    if missing:
        raise ValueError(
            f"нет в справочнике: {', '.join(missing)}. Сначала find_instrument."
        )
    if not bars:
        raise ValueError("история пустая — проверь интервал и глубину")

    spec = Spec(
        family=family, lookback=lookback, stop_atr=stop_atr, target_atr=target_atr
    )
    campaign = f"{family}:{interval}:{','.join(sorted(names))}"
    period = f"{min(b.start for b in bars).date()}..{max(b.start for b in bars).date()}"
    protocol = Protocol()
    trials.open_campaign(
        campaign,
        f"есть ли преимущество у семейства {family} на этих бумагах",
        names, period, asdict(protocol),
    )
    # Попытка записывается ДО расчёта: иначе неудачные варианты исчезали бы
    # из знаменателя, а порог считался бы как для одной проверки.
    trials.register(campaign, spec.version, spec.to_dict())
    registered = trials.count(campaign)

    report = evaluate(
        [spec], bars, registered, protocol, Costs(), Rules(capital=config.STARTING_CAPITAL)
    )
    report["campaign"] = campaign
    report["period"] = period
    report["universe"] = names
    trials.finish(campaign, spec.version, {
        "net": report.get("net"), "verdict": report.get("verdict")
    })

    path = marketdata.data_dir() / f"backtest_{spec.version}.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )

    answer = {
        "verdict": report["verdict"],
        "why": report.get("why") or "",
        "file": str(path),
        "spec_version": spec.version,
        "campaign": campaign,
        "period": period,
        "net": report.get("net"),
        "net_pct": report.get("net_pct"),
        "oos": report.get("oos"),
        "oos_sessions": report.get("oos_sessions"),
        "positive_folds": report.get("positive_folds"),
        "stress": report.get("stress"),
        "baselines": report.get("baselines"),
        "bootstrap_lower_mean": (report.get("bootstrap") or {}).get("lower_mean"),
        "registered_trials": registered,
        "failed_gates": report.get("failed_gates"),
        "trading_allowed": False,
    }
    if register_as:
        oos = report.get("oos") or {}
        answer["playbook"] = journal.register_playbook(
            {
                "name": register_as,
                "entry": f"{family}, lookback {lookback}, стоп {stop_atr} ATR, "
                         f"цель {target_atr} ATR",
                "invalidation": "средний результат вне обучения перестаёт быть выше нуля",
                "measured_on": f"{interval}, {period}, бумаги: {', '.join(names)}",
                "trades": int(oos.get("trades") or 0),
                "wins": int(oos.get("wins") or 0),
                "avg_r": float(oos.get("avg_r") or 0.0),
                "source": "evaluator",
                "evidence": {"file": str(path), "version": spec.version,
                             "trials": registered, "verdict": report["verdict"]},
            }
        )
    return answer


@tool(
    "register_playbook",
    "Зарегистрировать или обновить сетап. Торгуются только сетапы из "
    "реестра, и размер зависит от накопленной статистики: до 15 сделок с "
    "положительным средним R — четверть размера, дальше полный.\n"
    "measured_on — на чём считалась статистика: период, бумаги, как "
    "проверял. «Выглядит перспективно» статистикой не является. Числа "
    "проверяются: попаданий не больше сделок.",
    _obj(
        {
            "name": {"type": "string"},
            "entry": {"type": "string", "description": "условие входа, проверяемое по данным"},
            "invalidation": {
                "type": "string",
                "description": "что докажет, что сетап не работает",
            },
            "measured_on": {
                "type": "string",
                "description": "период, бумаги и способ измерения статистики",
            },
            "trades": {"type": "integer", "description": "число наблюдений"},
            "wins": {"type": "integer", "description": "сколько из них в плюс"},
            "avg_r": {"type": "number", "description": "средний результат в R"},
        },
        ["name", "entry", "invalidation", "measured_on", "trades", "wins", "avg_r"],
    ),
)
def tool_register_playbook(
    name: str,
    entry: str,
    invalidation: str,
    measured_on: str,
    trades: int,
    wins: int,
    avg_r: float,
) -> dict:
    if not str(name).strip():
        raise ValueError("имя сетапа обязательно")
    trades, wins = int(trades), int(wins)
    if trades < 0 or wins < 0 or wins > trades:
        raise ValueError(
            f"числа не сходятся: попаданий {wins} при {trades} сделках. "
            f"Реестр — это запись измерения, а не оценки."
        )
    if not math.isfinite(float(avg_r)):
        raise ValueError("средний результат должен быть конечным числом")
    for field, value in (("entry", entry), ("invalidation", invalidation),
                         ("measured_on", measured_on)):
        if len(str(value).strip()) < 10:
            raise ValueError(
                f"{field}: слишком коротко. Условие, которое нельзя проверить "
                f"по данным, не является условием."
            )
    return journal.register_playbook(
        {
            "name": name, "entry": entry, "invalidation": invalidation,
            "measured_on": measured_on, "trades": trades, "wins": wins, "avg_r": avg_r,
        }
    )


@tool(
    "playbooks",
    "Реестр сетапов: статус, статистика, условия. Статус выводится из чисел: "
    "working — не меньше 15 сделок при положительном среднем R, probation — "
    "всё остальное, retired — снят вручную.",
    _obj({}),
)
def tool_playbooks() -> list[dict]:
    return journal.playbooks()


@tool(
    "retire_playbook",
    "Снять сетап с торговли. Нужно, когда собственная проверка дала "
    "отрицательную базу: торговать сетап, который сам измерил как "
    "неработающий, запрещено.",
    _obj({"name": {"type": "string"}, "why": {"type": "string"}}, ["name", "why"]),
)
def tool_retire_playbook(name: str, why: str) -> dict:
    if journal.playbook(name) is None:
        raise ValueError(f"сетап {name!r} не зарегистрирован")
    journal.retire_playbook(name, why)
    return journal.playbook(name)


@tool(
    "order_state",
    "Точное состояние заявки по идентификатору. Принимает и наш ключ "
    "(request_id из ответа buy/sell), и биржевой orderId. Это единственный "
    "способ узнать судьбу заявки: отсутствие в active_orders не отличает "
    "исполненную от отменённой. Если после обрыва связи вход заблокирован — "
    "разблокирует его именно этот вызов.",
    _obj({"order_id": {"type": "string"}}, ["order_id"]),
)
def tool_order_state(order_id: str) -> dict:
    intent = journal.intent_by_request(order_id)
    if intent is not None:
        why = _resolve_intent(intent, intent["ticker"] or "")
        state = journal.intent_by_request(order_id)
        return {
            "request_id": order_id,
            "order_id": state["order_id"] or "",
            "intent_state": state["state"],
            "detail": state["detail"] or "",
            "blocking": why or "",
            "resolved": not why,
        }
    try:
        state = client().order_state(order_id)
    except OrderNotFound:
        return {"order_id": order_id, "found": False,
                "note": "брокер такой заявки не знает"}
    state.pop("raw", None)
    state["found"] = True
    return state


@tool(
    "operations",
    "История операций по счёту за последние N дней.",
    _obj({"days": {"type": "integer"}}),
)
def tool_operations(days: int = 7) -> list[dict]:
    return client().operations(max(1, min(int(days), 90)))


@tool(
    "web_search",
    "Поиск в интернете с открытием страниц. Возвращает текст и ссылки на "
    "источники. В контекст ложится немного (около тысячи токенов), но на "
    "стороне провайдера запрос стоит десятки тысяч и тратит квоту аккаунта. "
    "Формулируй конкретно. Если проверяешь, вышел ли документ, ставь "
    "watch_url на страницу раскрытия: за первый прогон один и тот же вопрос "
    "про отчёт был задан поиском тринадцать раз за шесть часов.",
    _obj({"query": {"type": "string"}}, ["query"]),
)
def tool_web_search(query: str) -> dict:
    return search.web_search(query)


@tool(
    "schedule_wakeup",
    "Назначить себе пробуждение. Время: '+30m', '+2h' или "
    "'2026-08-24T16:45' по Москве. Причина попадёт в текст пробуждения — "
    "пиши так, чтобы будущий ты понял, зачем проснулся. "
    "Одновременно можно держать до пяти будильников. "
    "Будильник — для событий по расписанию (публикация, открытие торгов). "
    "Следить за ценой им не надо: для этого есть watch, который не тратит "
    "ни одного токена, пока условие не сработало.",
    _obj(
        {
            "at": {"type": "string", "description": "'+45m' или '2026-08-24T16:45'"},
            "reason": {"type": "string", "description": "зачем просыпаешься"},
        },
        ["at", "reason"],
    ),
)
def tool_schedule_wakeup(at: str, reason: str) -> dict:
    due = _parse_when(at)
    if due < time.time():
        raise ValueError("время пробуждения в прошлом")
    if not str(reason).strip():
        raise ValueError("причина обязательна")
    wakeup_id = journal.schedule_wakeup(due, str(reason)[:400])
    return {
        "id": wakeup_id,
        "at_msk": datetime.fromtimestamp(due, MSK).strftime("%Y-%m-%d %H:%M"),
        "in_minutes": round((due - time.time()) / 60, 1),
        "reason": reason,
    }


@tool("wakeups", "Активные будильники.", _obj({}))
def tool_wakeups() -> list[dict]:
    return [
        {
            "id": row["id"],
            "at_msk": datetime.fromtimestamp(row["due_ts"], MSK).strftime("%Y-%m-%d %H:%M"),
            "in_minutes": round((row["due_ts"] - time.time()) / 60, 1),
            "reason": row["reason"],
        }
        for row in journal.active_wakeups()
    ]


@tool(
    "cancel_wakeup",
    "Снять будильник, который больше не нужен.",
    _obj({"id": {"type": "integer"}}, ["id"]),
)
def tool_cancel_wakeup(id: int) -> dict:  # noqa: A002 — имя поля схемы
    return {"cancelled": journal.cancel_wakeup(int(id))}


@tool(
    "watch",
    "Поставить условие на цену. Пока условие не выполнено, ты спишь и не "
    "тратишь ни токена: цену раз в полминуты проверяет код одним батч-"
    "запросом. Как только сработает — тебя разбудят с текущей ценой и твоей "
    "заметкой.\n"
    "Это основной способ следить за рынком. Просыпаться по будильнику, чтобы "
    "посмотреть, дошла ли цена до уровня, не надо: за прошлый прогон таких "
    "пробуждений было шестнадцать, и почти все закончились ничем.\n"
    "Условие: price_above, price_below или pct_move (движение в процентах от "
    "цены на момент постановки, в любую сторону).",
    _obj(
        {
            "instrument_id": {"type": "string"},
            "condition": {
                "type": "string",
                "description": "price_above | price_below | pct_move",
            },
            "value": {"type": "number", "description": "цена или проценты"},
            "note": {
                "type": "string",
                "description": "что это значит и что делать при срабатывании",
            },
            "hours": {"type": "number", "description": "срок жизни, по умолчанию 8"},
        },
        ["instrument_id", "condition", "value", "note"],
    ),
)
def tool_watch(
    instrument_id: str, condition: str, value: float, note: str, hours: float = 8.0
) -> dict:
    if not str(note).strip():
        raise ValueError("заметка обязательна: будущий ты должен понять, что сработало")
    instrument = _resolve(instrument_id)
    threshold = float(value)
    base = None
    if condition == "pct_move":
        prices = client().last_price([instrument_id])
        if not prices or not prices[0].get("price"):
            raise ValueError("нет текущей цены, от которой считать движение")
        base = float(prices[0]["price"])
        threshold = abs(threshold)
    hours = max(0.1, min(float(hours or 8), 72))
    watch_id = journal.add_watch(
        instrument_id,
        instrument["ticker"],
        condition,
        threshold,
        str(note)[:400],
        time.time() + hours * 3600,
        base_price=base,
    )
    return {
        "id": watch_id,
        "ticker": instrument["ticker"],
        "condition": condition,
        "value": threshold,
        "base_price": base,
        "expires_msk": datetime.fromtimestamp(
            time.time() + hours * 3600, MSK
        ).strftime("%Y-%m-%d %H:%M"),
    }


@tool(
    "watch_url",
    "Следить за страницей: раз в несколько минут код скачивает её и сверяет "
    "хеш видимого текста. Изменилась — тебя разбудят.\n"
    "Так проверяют раскрытие отчётности, решения совета директоров, "
    "сообщения эмитента. Не спрашивай поиск «вышел ли отчёт» по таймеру: за "
    "прошлый прогон один и тот же вопрос был задан тринадцать раз за шесть "
    "часов, каждый раз за десятки тысяч токенов квоты, и все тринадцать "
    "ответов были «пока нет».",
    _obj(
        {
            "url": {"type": "string", "description": "страница раскрытия"},
            "note": {"type": "string", "description": "чего ждёшь и что делать"},
            "hours": {"type": "number", "description": "срок жизни, по умолчанию 24"},
        },
        ["url", "note"],
    ),
)
def tool_watch_url(url: str, note: str, hours: float = 24.0) -> dict:
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("нужен http- или https-адрес")
    if not str(note).strip():
        raise ValueError("заметка обязательна")
    current = marketdata.page_hash(url)
    hours = max(0.1, min(float(hours or 24), 168))
    watch_id = journal.add_watch(
        url,
        "",
        "url_changed",
        0.0,
        str(note)[:400],
        time.time() + hours * 3600,
        url=url,
        content_hash=current,
    )
    return {
        "id": watch_id,
        "url": url,
        "baseline_hash": current[:12],
        "expires_msk": datetime.fromtimestamp(
            time.time() + hours * 3600, MSK
        ).strftime("%Y-%m-%d %H:%M"),
    }


@tool("watches", "Активные условия наблюдения.", _obj({}))
def tool_watches() -> list[dict]:
    return [
        {
            "id": row["id"],
            "ticker": row["ticker"] or row["url"],
            "condition": row["kind"],
            "value": row["threshold"],
            "note": row["note"],
            "expires_msk": datetime.fromtimestamp(row["expires_ts"], MSK).strftime(
                "%Y-%m-%d %H:%M"
            )
            if row["expires_ts"]
            else None,
        }
        for row in journal.active_watches()
    ]


@tool(
    "unwatch",
    "Снять условие наблюдения.",
    _obj({"id": {"type": "integer"}}, ["id"]),
)
def tool_unwatch(id: int) -> dict:  # noqa: A002 — имя поля схемы
    return {"cancelled": journal.cancel_watch(int(id))}


@tool(
    "notify",
    "Написать оператору в Telegram. Для важного: заметная сделка, "
    "неожиданный результат, вопрос, на который нужен ответ человека. "
    "Ответ не ждётся — если он придёт, ты получишь его как сообщение.",
    _obj({"text": {"type": "string"}}, ["text"]),
)
def tool_notify(text: str) -> dict:
    delivered = _telegram(text)
    journal.log_event("notify", {"text": text, "delivered": delivered})
    return {"delivered": delivered}


@tool(
    "send_report",
    "Отправить оператору дневной отчёт: короткая выжимка текстом и полный "
    "разбор файлом. Файл готовь заранее в notes/reports/ГГГГ-ММ-ДД.md. "
    "В выжимке — результат дня, главное решение и что планируешь завтра; "
    "подробности в файле.",
    _obj(
        {
            "summary": {
                "type": "string",
                "description": "3-6 строк: итог дня, ключевое решение, план",
            },
            "path": {"type": "string", "description": "путь к markdown-файлу"},
        },
        ["summary", "path"],
    ),
)
def tool_send_report(summary: str, path: str) -> dict:
    portfolio = client().portfolio()
    result = portfolio["total"] - config.STARTING_CAPITAL
    header = (
        f"Отчёт за {datetime.now(MSK):%d.%m.%Y}\n"
        f"Портфель: {portfolio['total']:,.0f} ₽  "
        f"({result:+,.0f} ₽, {result / config.STARTING_CAPITAL * 100:+.2f}%)\n\n"
    ).replace(",", " ")
    sent_text = _telegram(header + summary)
    # Отказ файла не должен терять уже отправленную выжимку и запись в журнале.
    sent_file, file_error = False, ""
    try:
        sent_file = _telegram_document(
            path, f"Подробный отчёт за {datetime.now(MSK):%d.%m.%Y}"
        )
    except (ValueError, OSError) as exc:
        file_error = str(exc)
    journal.log_event(
        "daily_report",
        {
            "summary": summary,
            "path": path,
            "text": sent_text,
            "file": sent_file,
            "file_error": file_error,
        },
    )
    result = {"text_delivered": sent_text, "file_delivered": sent_file}
    if file_error:
        result["file_error"] = file_error
    return result


# --- цикл JSON-RPC --------------------------------------------------------


_WRITE_LOCK = threading.Lock()

# Поиск занимает в среднем сорок секунд и до трёх минут по таймауту. При
# строго последовательной обработке всё это время тот же процесс не
# обслуживал ни портфель, ни продажу, ни снятие заявки.
POOL_SIZE = 6


def _send(message: dict) -> None:
    with _WRITE_LOCK:
        sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _render(tool: str, payload: Any) -> str:
    """Текст ответа инструмента. Объёмное уезжает в файл, а не в контекст.

    Отступов нет намеренно: `indent=2` добавляет к любому ответу около 40%
    объёма и ничего не даёт модели. Потолок — последняя линия обороны: даже
    инструмент, который завтра начнёт возвращать таблицу на тысячу строк, не
    сможет высыпать её в сессию текстом.
    """
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text.encode("utf-8")) <= config.MAX_TOOL_RESULT_BYTES:
        return text
    try:
        stub = marketdata.spill(tool, payload, text)
    except OSError as exc:  # диск недоступен — лучше обрезать, чем потерять ответ
        journal.log_event("spill_failed", {"tool": tool, "error": str(exc)[:200]})
        return text[: config.MAX_TOOL_RESULT_BYTES] + "\n…обрезано"
    journal.log_event("tool_spilled", {"tool": tool, "file": stub["file"]})
    return json.dumps(stub, ensure_ascii=False, separators=(",", ":"), default=str)


def _call_tool(name: str, arguments: dict) -> tuple[bool, Any]:
    handler = HANDLERS.get(name)
    if handler is None:
        return False, f"неизвестный инструмент: {name}"
    started = time.monotonic()
    try:
        result = handler(**arguments)
        ok, payload = True, result
    except guards.GuardRejection as exc:
        ok, payload = False, str(exc)
    except TInvestError as exc:
        ok, payload = False, f"{exc} (код {exc.code})" if exc.code else str(exc)
    except TypeError as exc:
        ok, payload = False, f"неверные аргументы: {exc}"
    except Exception as exc:  # noqa: BLE001 — агенту нужен текст любой ошибки
        ok, payload = False, f"{type(exc).__name__}: {exc}"
        journal.log_event("tool_crash", {"tool": name, "trace": traceback.format_exc()})
    duration = int((time.monotonic() - started) * 1000)
    journal.log_tool_call(
        name,
        arguments,
        ok,
        result=payload if ok else None,
        error=None if ok else str(payload),
        duration_ms=duration,
    )
    return ok, payload


def _handle_call(request_id, params: dict) -> None:
    name = params.get("name", "")
    arguments = params.get("arguments") or {}
    ok, payload = _call_tool(name, arguments)
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": _render(name, payload)}],
                "isError": not ok,
            },
        }
    )


def main() -> None:
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=POOL_SIZE, thread_name_prefix="tool"
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue

        method = request.get("method")
        request_id = request.get("id")

        if method == "initialize":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "tinvest-agent", "version": "1.0.0"},
                    },
                }
            )
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = request.get("params") or {}
            pool.submit(_handle_call, request_id, params)
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif request_id is not None:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"метод не поддерживается: {method}"},
                }
            )
    pool.shutdown(wait=False)


if __name__ == "__main__":
    main()
