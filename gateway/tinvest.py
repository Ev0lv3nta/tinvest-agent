"""Клиент песочницы T-Invest API поверх REST.

Почему REST, а не официальный gRPC SDK:
  * SDK держит три несовместимых набора схем, их легко перепутать;
  * SandboxPayIn через gRPC стабильно отдаёт INTERNAL 70001, через REST работает;
  * REST не тянет зависимостей — весь модуль на стандартной библиотеке.

TLS: сертификат *.tbank.ru подписан Russian Trusted Root CA, которого нет
в системных хранилищах. Корневой сертификат лежит в deploy/ и передаётся
явно — системное хранилище не трогаем.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import config

_SSL_CONTEXT = ssl.create_default_context(cafile=config.CA_BUNDLE)


# Коды, за которыми стоит не наша ошибка, а состояние сервера. Повтор
# осмысленен: тот же запрос через секунду обычно проходит.
RETRYABLE_CODES = {"70001", "70002", "80002"}


class TInvestError(RuntimeError):
    """Ошибка API. `code` — числовой код T-Invest (например, 30079).

    `answered` — ключевое поле для заявок. True означает, что брокер
    ответил по существу: запрос до него дошёл, был разобран и отклонён.
    False означает, что ответа мы не получили — и тогда судьба заявки
    неизвестна, потому что она могла быть принята.

    Разница не косметическая. Раньше обрыв связи заворачивался в тот же
    TInvestError, что и отказ брокера, шлюз объявлял заявку отклонённой и
    закрывал ключ идемпотентности. Следующая попытка уходила с новым
    ключом — и создавала вторую позицию поверх первой, о которой мы не
    знали. Ветка «связь оборвалась» при этом выглядела написанной, но до
    неё не доходило управление ни разу.
    """

    def __init__(
        self,
        message: str,
        code: str = "",
        http_status: int = 0,
        retryable: Optional[bool] = None,
        answered: Optional[bool] = None,
    ):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.answered = (
            answered if answered is not None else _looks_answered(code, http_status)
        )
        self.retryable = (
            retryable
            if retryable is not None
            else (code in RETRYABLE_CODES or http_status in (429, 500, 502, 503, 504))
        )

    @property
    def ambiguous(self) -> bool:
        """Судьба запроса неизвестна: повторять вслепую нельзя."""
        return not self.answered


def _looks_answered(code: str, http_status: int) -> bool:
    """Похоже ли это на ответ брокера по существу.

    Дефолт выбран в безопасную сторону: лишняя блокировка стоит паузы,
    лишнее разрешение — второй позиции поверх первой. Поэтому ответом
    считается только разобранный отказ, а не всякая ошибка.

    Внутренняя ошибка сервера ответом не является, даже когда у неё есть
    код: запрос до брокера дошёл, а был ли применён — из такого ответа не
    следует.
    """
    if code in RETRYABLE_CODES or http_status >= 500:
        return False
    return bool(code or 0 < http_status < 500)


class OrderNotFound(TInvestError):
    """Брокер ответил, что такой заявки у него нет. Не «мы не смогли спросить»."""


def journal_retry(service: str, method: str, exc: "TInvestError", attempt: int) -> None:
    """Повторы видны в журнале: если их станет много, это само по себе факт."""
    from . import journal

    journal.log_event(
        "api_retry",
        {"method": f"{service}/{method}", "attempt": attempt, "error": str(exc)[:160]},
    )


def _money(value: Optional[dict]) -> float:
    """MoneyValue/Quotation -> float. units приходит строкой, nano — миллиардные."""
    if not value:
        return 0.0
    return int(value.get("units") or 0) + int(value.get("nano") or 0) / 1e9


def _to_quotation(value: float) -> dict:
    units = int(value)
    nano = int(round((value - units) * 1e9))
    # Округление может вытолкнуть nano за границу — переносим в units.
    if abs(nano) >= 1_000_000_000:
        units += nano // 1_000_000_000
        nano %= 1_000_000_000
    return {"units": str(units), "nano": nano}


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SandboxClient:
    def __init__(self, token: str, account_id: str = ""):
        if not token:
            raise TInvestError("не задан TINVEST_TOKEN")
        self.token = token
        self.account_id = account_id

    # --- транспорт ---------------------------------------------------------

    def call(
        self,
        service: str,
        method: str,
        payload: Optional[dict] = None,
        retries: int = config.API_RETRIES,
    ) -> dict:
        """Вызов с повтором на временных отказах песочницы.

        Песочница регулярно отвечает `Internal error` (код 70001) на
        совершенно корректные запросы — замер 24.08 дал девять отказов из
        двадцати на одном и том же чтении, включая портфель и заявки.
        Без повторов половина сверок и котировок просто не доезжает, а
        отказ выглядит как содержательный ответ API.

        Повтор безопасен и для отправки заявки: `orderId` — клиентский ключ
        идемпотентности, брокер по нему вернёт ту же заявку, а не создаст
        вторую.
        """
        delay = config.API_RETRY_DELAY
        for attempt in range(retries + 1):
            try:
                return self._call_once(service, method, payload)
            except TInvestError as exc:
                if attempt >= retries or not exc.retryable:
                    raise
                journal_retry(service, method, exc, attempt + 1)
                time.sleep(delay)
                delay *= 2
        raise TInvestError(
            f"{service}/{method}: не удалось после {retries} повторов", answered=False
        )

    def _call_once(self, service: str, method: str, payload: Optional[dict] = None) -> dict:
        url = f"{config.TINVEST_SANDBOX_URL}/{config.TINVEST_NS}.{service}/{method}"
        body = json.dumps(payload or {}).encode()
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=config.HTTP_TIMEOUT, context=_SSL_CONTEXT
            ) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
                message = parsed.get("message", raw)
                code = str(parsed.get("description") or parsed.get("code") or "")
            except ValueError:
                message, code = raw[:400], ""
            # 4xx — брокер разобрал запрос и отказал: ответ содержательный.
            # 5xx — сбой на его стороне уже после приёма запроса, и был ли
            # запрос применён, из ответа не следует.
            raise TInvestError(
                f"{service}/{method}: {message}",
                code=code,
                http_status=exc.code,
                answered=exc.code < 500,
            ) from exc
        except urllib.error.URLError as exc:
            raise TInvestError(
                f"{service}/{method}: сеть недоступна ({exc.reason})",
                retryable=True,
                answered=False,
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise TInvestError(
                f"{service}/{method}: обрыв связи ({exc})",
                retryable=True,
                answered=False,
            ) from exc

    def _sandbox(self, method: str, payload: Optional[dict] = None) -> dict:
        data = dict(payload or {})
        data.setdefault("accountId", self.account_id)
        return self.call("SandboxService", method, data)

    # --- управление счётом -------------------------------------------------

    def open_account(self, name: str) -> str:
        return self.call("SandboxService", "OpenSandboxAccount", {"name": name})["accountId"]

    def close_account(self, account_id: str) -> None:
        self.call("SandboxService", "CloseSandboxAccount", {"accountId": account_id})

    def list_accounts(self) -> list[dict]:
        return self.call("SandboxService", "GetSandboxAccounts", {}).get("accounts", [])

    def pay_in(self, rubles: float, account_id: str = "") -> float:
        amount = _to_quotation(rubles)
        amount["currency"] = "rub"
        response = self.call(
            "SandboxService",
            "SandboxPayIn",
            {"accountId": account_id or self.account_id, "amount": amount},
        )
        return _money(response.get("balance"))

    # --- состояние ---------------------------------------------------------

    def portfolio(self) -> dict:
        raw = self._sandbox("GetSandboxPortfolio")
        positions = []
        for item in raw.get("positions", []):
            quantity = _money(item.get("quantity"))
            if item.get("instrumentType") == "currency":
                continue
            positions.append(
                {
                    "instrument_id": item.get("instrumentUid") or item.get("figi"),
                    "figi": item.get("figi"),
                    "ticker": item.get("ticker"),
                    "type": item.get("instrumentType"),
                    "quantity": quantity,
                    "lots": _money(item.get("quantityLots")),
                    "average_price": _money(item.get("averagePositionPrice")),
                    "current_price": _money(item.get("currentPrice")),
                    "yield": _money(item.get("expectedYield")),
                }
            )
        return {
            "total": _money(raw.get("totalAmountPortfolio")),
            "cash": _money(raw.get("totalAmountCurrencies")),
            "shares": _money(raw.get("totalAmountShares")),
            "bonds": _money(raw.get("totalAmountBonds")),
            "etf": _money(raw.get("totalAmountEtf")),
            "futures": _money(raw.get("totalAmountFutures")),
            "expected_yield": _money(raw.get("expectedYield")),
            "positions": positions,
        }

    def withdraw_limits(self) -> dict:
        raw = self._sandbox("GetSandboxWithdrawLimits")
        return {
            "money": [
                {"currency": m.get("currency"), "amount": _money(m)}
                for m in raw.get("money", [])
            ],
            "blocked": [
                {"currency": m.get("currency"), "amount": _money(m)}
                for m in raw.get("blocked", [])
            ],
        }

    def max_lots(self, instrument_id: str, price: Optional[float] = None) -> dict:
        payload: dict[str, Any] = {"instrumentId": instrument_id}
        if price is not None:
            payload["price"] = _to_quotation(price)
        raw = self._sandbox("GetSandboxMaxLots", payload)
        buy = raw.get("buyLimits") or {}
        sell = raw.get("sellLimits") or {}
        return {
            "currency": raw.get("currency"),
            "buy_money": _money(buy.get("buyMoneyAmount")),
            "buy_max_lots": int(buy.get("buyMaxLots") or 0),
            "buy_max_market_lots": int(buy.get("buyMaxMarketLots") or 0),
            "sell_max_lots": int(sell.get("sellMaxLots") or 0),
        }

    def operations(self, days: int = 7) -> list[dict]:
        now = datetime.now(timezone.utc)
        raw = self._sandbox(
            "GetSandboxOperations",
            {"from": _utc(now - timedelta(days=days)), "to": _utc(now)},
        )
        return [
            {
                "date": item.get("date"),
                "type": item.get("type"),
                "state": item.get("state"),
                "figi": item.get("figi"),
                "quantity": int(item.get("quantity") or 0),
                "payment": _money(item.get("payment")),
                "price": _money(item.get("price")),
            }
            for item in raw.get("operations", [])
        ]

    # --- заявки ------------------------------------------------------------

    def post_order(
        self,
        instrument_id: str,
        lots: int,
        direction: str,
        price: Optional[float] = None,
        order_id: str = "",
    ) -> dict:
        payload: dict[str, Any] = {
            "instrumentId": instrument_id,
            "quantity": str(int(lots)),
            "direction": direction,
            "orderId": order_id or str(uuid.uuid4()),
            "orderType": "ORDER_TYPE_LIMIT" if price is not None else "ORDER_TYPE_MARKET",
        }
        if price is not None:
            payload["price"] = _to_quotation(price)
        raw = self._sandbox("PostSandboxOrder", payload)
        state = self._order_state(raw)
        state["message"] = raw.get("message")
        return state

    def order_state(self, order_id: str, by_request_id: bool = False) -> dict:
        """Точное состояние одной заявки.

        Единственный способ узнать судьбу заявки. Раньше вывод делался из
        отсутствия в списке активных: заявка исчезла — значит отменена. Но
        из активных исчезает и исполненная, и отклонённая, и та, о которой
        брокер просто не ответил в этот раз.

        `by_request_id` спрашивает по нашему собственному ключу, который мы
        передавали в `orderId` при отправке. Это разные идентификаторы:
        брокер возвращает свой, а ищет по обоим — но только если явно
        сказать, какой из них передан.
        """
        payload: dict[str, Any] = {
            "accountId": self.account_id,
            "orderId": order_id,
            "orderIdType": (
                "ORDER_ID_TYPE_REQUEST" if by_request_id else "ORDER_ID_TYPE_EXCHANGE"
            ),
        }
        try:
            raw = self.call("SandboxService", "GetSandboxOrderState", payload)
        except TInvestError as exc:
            # Содержательный отказ по существующему запросу означает, что
            # такой заявки у брокера нет. Всё остальное — незнание.
            if exc.answered and 400 <= exc.http_status < 500:
                raise OrderNotFound(str(exc), code=exc.code, http_status=exc.http_status)
            raise
        return self._order_state(raw)

    @staticmethod
    def _order_state(raw: dict) -> dict:
        """Разбор состояния заявки.

        Комиссия берётся из `executedCommission`: `initialCommission` — это
        оценка на момент выставления, а не то, что списали.
        """
        stages = raw.get("stages") or []
        return {
            "order_id": raw.get("orderId"),
            "request_id": raw.get("orderRequestId") or "",
            "status": raw.get("executionReportStatus") or "",
            "direction": raw.get("direction") or "",
            "instrument_id": raw.get("instrumentUid") or "",
            "figi": raw.get("figi") or "",
            "lots_requested": int(raw.get("lotsRequested") or 0),
            "lots_executed": int(raw.get("lotsExecuted") or 0),
            "price": _money(raw.get("executedOrderPrice")),
            "total": _money(raw.get("totalOrderAmount")),
            "commission": _money(
                raw.get("executedCommission") or raw.get("initialCommission")
            ),
            "stages": [
                {
                    "price": _money(stage.get("price")),
                    "quantity": int(stage.get("quantity") or 0),
                    "trade_id": stage.get("tradeId"),
                }
                for stage in stages
            ],
            "raw": raw,
        }

    def cancel_order(self, order_id: str, by_request_id: bool = False) -> str:
        """Снять заявку. Идентификатор бывает двух видов, и брокер должен
        знать, какой из них передан: наш ключ или его собственный."""
        raw = self._sandbox(
            "CancelSandboxOrder",
            {
                "orderId": order_id,
                "orderIdType": (
                    "ORDER_ID_TYPE_REQUEST" if by_request_id else "ORDER_ID_TYPE_EXCHANGE"
                ),
            },
        )
        return raw.get("time", "")

    def active_orders(self) -> list[dict]:
        raw = self._sandbox("GetSandboxOrders")
        return [
            {
                "order_id": item.get("orderId"),
                "request_id": item.get("orderRequestId") or "",
                "figi": item.get("figi"),
                "instrument_id": item.get("instrumentUid"),
                "direction": item.get("direction"),
                "status": item.get("executionReportStatus"),
                "lots_requested": int(item.get("lotsRequested") or 0),
                "lots_executed": int(item.get("lotsExecuted") or 0),
                "price": _money(item.get("initialSecurityPrice")),
            }
            for item in raw.get("orders", [])
        ]

    # --- рыночные данные ---------------------------------------------------

    def find_instrument(
        self, query: str, limit: int = 10, kind: str = ""
    ) -> list[dict]:
        """Поиск инструмента. `kind` — INSTRUMENT_TYPE_SHARE, _BOND, _ETF и т.п."""
        payload: dict[str, Any] = {"query": query, "apiTradeAvailableFlag": True}
        if kind:
            payload["instrumentKind"] = kind
        raw = self.call("InstrumentsService", "FindInstrument", payload)
        return [
            {
                "instrument_id": item.get("uid"),
                "figi": item.get("figi"),
                "ticker": item.get("ticker"),
                "name": item.get("name"),
                "type": item.get("instrumentType"),
                "class_code": item.get("classCode"),
                "lot": int(item.get("lot") or 0),
                "currency": item.get("currency"),
                "api_trade_available": item.get("apiTradeAvailableFlag"),
            }
            for item in raw.get("instruments", [])
        ][:limit]

    def instrument_by_uid(self, uid: str) -> dict:
        """Точное разрешение инструмента по идентификатору.

        Раньше тикер и FIGI для журнала брались из текстового поиска, куда
        передавался UID: обычно срабатывало, но кривая выдача записала бы в
        журнал чужую бумагу, а сверка операций матчится по FIGI.
        """
        raw = self.call(
            "InstrumentsService",
            "GetInstrumentBy",
            {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid},
        )
        item = raw.get("instrument") or {}
        return {
            "instrument_id": item.get("uid"),
            "figi": item.get("figi"),
            "ticker": item.get("ticker"),
            "name": item.get("name"),
            "lot": int(item.get("lot") or 1),
            "currency": item.get("currency"),
            "type": item.get("instrumentType"),
        }

    def last_price(self, instrument_ids: list[str]) -> list[dict]:
        raw = self.call(
            "MarketDataService", "GetLastPrices", {"instrumentId": instrument_ids}
        )
        return [
            {
                "instrument_id": item.get("instrumentUid"),
                "figi": item.get("figi"),
                "ticker": item.get("ticker"),
                "price": _money(item.get("price")),
                "time": item.get("time"),
            }
            for item in raw.get("lastPrices", [])
        ]

    def trading_status(self, instrument_id: str) -> dict:
        raw = self.call(
            "MarketDataService", "GetTradingStatus", {"instrumentId": instrument_id}
        )
        return {
            "status": raw.get("tradingStatus"),
            "order_available": bool(raw.get("apiTradeAvailableFlag")),
            "market_order_available": bool(raw.get("marketOrderAvailableFlag")),
            "limit_order_available": bool(raw.get("limitOrderAvailableFlag")),
        }

    def candles(self, instrument_id: str, interval: str, days: int) -> list[dict]:
        now = datetime.now(timezone.utc)
        return self.candles_between(
            instrument_id, interval, now - timedelta(days=days), now
        )

    def candles_between(
        self, instrument_id: str, interval: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Явный отрезок. Длинная история берётся окнами: API отдаёт
        ограниченный диапазон за запрос, и просить сразу год минутных свечей
        бессмысленно."""
        raw = self.call(
            "MarketDataService",
            "GetCandles",
            {
                "instrumentId": instrument_id,
                "from": _utc(start),
                "to": _utc(end),
                "interval": interval,
            },
        )
        return [
            {
                "time": item.get("time"),
                "open": _money(item.get("open")),
                "high": _money(item.get("high")),
                "low": _money(item.get("low")),
                "close": _money(item.get("close")),
                "volume": int(item.get("volume") or 0),
                # Незавершённый бар нельзя считать наблюдением: он ещё
                # изменится. Раньше признак терялся, и последний бар просто
                # отрезали — вместе с завершённым баром закрытого дня.
                "complete": bool(item.get("isComplete")),
            }
            for item in raw.get("candles", [])
        ]

    def orderbook(self, instrument_id: str, depth: int = 10) -> dict:
        raw = self.call(
            "MarketDataService",
            "GetOrderBook",
            {"instrumentId": instrument_id, "depth": depth},
        )
        return {
            "last_price": _money(raw.get("lastPrice")),
            "close_price": _money(raw.get("closePrice")),
            "limit_up": _money(raw.get("limitUp")),
            "limit_down": _money(raw.get("limitDown")),
            "bids": [
                {"price": _money(b.get("price")), "quantity": int(b.get("quantity") or 0)}
                for b in raw.get("bids", [])
            ],
            "asks": [
                {"price": _money(a.get("price")), "quantity": int(a.get("quantity") or 0)}
                for a in raw.get("asks", [])
            ],
        }
