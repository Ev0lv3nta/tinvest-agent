"""MCP-сервер с торговыми инструментами.

Транспорт — stdio, протокол JSON-RPC 2.0 построчно. Codex запускает этот
процесс сам и глушит вместе с сессией; отдельного демона не требуется.

Окружение до подпроцесса MCP не доезжает, поэтому секреты читаются из
файлов через gateway.config.
"""

from __future__ import annotations

import json
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import config, guards, journal, search
from .tinvest import SandboxClient, TInvestError

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


def _telegram_document(path: str, caption: str) -> bool:
    """Отправка файла в Telegram: multipart собираем вручную, без зависимостей."""
    token = config.secret("TELEGRAM_BOT_TOKEN")
    chat_id = config.secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError(f"файл не найден: {path}")

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


def _order(direction: str, instrument_id: str, lots: int, price, rationale: str) -> dict:
    api = client()
    guards.check_not_halted()
    guards.check_rate_limit()

    # Порог проверяется перед каждой сделкой по свежему портфелю, а не по
    # кешу: между вызовами цена позиций могла уехать.
    guards.check_capital_floor(api.portfolio()["total"])

    limits = api.max_lots(instrument_id, price)
    info = api.find_instrument(instrument_id, limit=1)
    ticker = info[0]["ticker"] if info else instrument_id[:8]

    if direction == "ORDER_DIRECTION_BUY":
        guards.check_buy(lots, limits, ticker)
    else:
        guards.check_sell(lots, limits, ticker)

    result = api.post_order(instrument_id, lots, direction, price)
    journal.log_order(
        {
            "order_id": result["order_id"],
            "instrument_id": instrument_id,
            "ticker": ticker,
            "direction": "buy" if direction.endswith("BUY") else "sell",
            "order_type": "limit" if price is not None else "market",
            "lots": lots,
            "lots_executed": result["lots_executed"],
            "price": result["price"],
            "total": result["total"],
            "status": result["status"],
            "raw": result["raw"],
        },
        rationale=rationale,
    )
    return result


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
    "'Сбер Банк', поэтому запрос 'Сбербанк' слитно её не находит.",
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
    return client().find_instrument(query, limit=limit, kind=kind)


@tool(
    "quote",
    "Последняя цена и статус торгов. Если статус не "
    "SECURITY_TRADING_STATUS_NORMAL_TRADING, заявки будут отклонены с кодом 30079.",
    _obj({"instrument_id": {"type": "string"}}, ["instrument_id"]),
)
def tool_quote(instrument_id: str) -> dict:
    prices = client().last_price([instrument_id])
    status = client().trading_status(instrument_id)
    return {"price": prices[0]["price"] if prices else None, **status}


@tool(
    "candles",
    "Исторические свечи. interval: CANDLE_INTERVAL_1_MIN, _5_MIN, _15_MIN, "
    "_HOUR, _DAY, _WEEK, _MONTH.",
    _obj(
        {
            "instrument_id": {"type": "string"},
            "interval": {"type": "string"},
            "days": {"type": "integer", "description": "глубина в днях"},
        },
        ["instrument_id", "interval", "days"],
    ),
)
def tool_candles(instrument_id: str, interval: str, days: int) -> list[dict]:
    return client().candles(instrument_id, interval, days)


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
    "в ЛОТАХ, не в штуках. Обоснование обязательно и попадает в журнал "
    "рядом со сделкой. Покупка на сумму больше свободных денег отклоняется: "
    "плечо запрещено.",
    _obj(
        {
            "instrument_id": {"type": "string"},
            "lots": {"type": "integer"},
            "price": {"type": "number", "description": "цена лимитной заявки"},
            "rationale": {
                "type": "string",
                "description": "почему покупаешь: тезис, ожидание, что опровергнет",
            },
        },
        ["instrument_id", "lots", "rationale"],
    ),
)
def tool_buy(instrument_id: str, lots: int, rationale: str, price: float | None = None) -> dict:
    return _order("ORDER_DIRECTION_BUY", instrument_id, lots, price, rationale)


@tool(
    "sell",
    "Продать. Без price — по рынку, с price — лимитная заявка. Количество "
    "в ЛОТАХ. Обоснование обязательно. Продажа сверх позиции отклоняется: "
    "шорт запрещён.",
    _obj(
        {
            "instrument_id": {"type": "string"},
            "lots": {"type": "integer"},
            "price": {"type": "number"},
            "rationale": {
                "type": "string",
                "description": "почему продаёшь: сработал тезис, стоп, ребалансировка",
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
    return {"cancelled_at": client().cancel_order(order_id)}


@tool(
    "operations",
    "История операций по счёту за последние N дней.",
    _obj({"days": {"type": "integer"}}),
)
def tool_operations(days: int = 7) -> list[dict]:
    return client().operations(days)


@tool(
    "web_search",
    "Поиск в интернете с открытием страниц. Возвращает текст и ссылки на "
    "источники. Один запрос стоит десятки тысяч токенов на стороне поиска — "
    "формулируй конкретно, а не 'что нового на рынке'.",
    _obj({"query": {"type": "string"}}, ["query"]),
)
def tool_web_search(query: str) -> dict:
    return search.web_search(query)


@tool(
    "schedule_wakeup",
    "Назначить себе следующее пробуждение. Время: '+30m', '+2h' или "
    "'2026-08-24T16:45' по Москве. Причина попадёт в текст пробуждения — "
    "пиши так, чтобы будущий ты понял, зачем проснулся. "
    "Активный будильник ровно один: новый вызов заменяет предыдущий.",
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
    wakeup_id = journal.schedule_wakeup(due, reason)
    return {
        "id": wakeup_id,
        "at_msk": datetime.fromtimestamp(due, MSK).strftime("%Y-%m-%d %H:%M"),
        "in_minutes": round((due - time.time()) / 60, 1),
        "reason": reason,
    }


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
    sent_file = _telegram_document(path, f"Подробный отчёт за {datetime.now(MSK):%d.%m.%Y}")
    journal.log_event(
        "daily_report",
        {"summary": summary, "path": path, "text": sent_text, "file": sent_file},
    )
    return {"text_delivered": sent_text, "file_delivered": sent_file}


# --- цикл JSON-RPC --------------------------------------------------------


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


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


def main() -> None:
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
            ok, payload = _call_tool(params.get("name", ""), params.get("arguments") or {})
            text = payload if isinstance(payload, str) else json.dumps(
                payload, ensure_ascii=False, indent=2, default=str
            )
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": not ok,
                    },
                }
            )
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


if __name__ == "__main__":
    main()
