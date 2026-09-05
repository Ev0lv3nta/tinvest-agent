"""Путь заявки: карточка, идемпотентность, поведение при обрыве связи.

Про обрыв связи важно, чем именно он изображается. Настоящий транспорт
заворачивает любой сетевой сбой в `TInvestError` с `answered=False` — а не
бросает голый `ConnectionResetError`. Тест, кидающий голое исключение,
проверял ветку, в которую боевой код не попадал ни разу, и зелёным
подтверждал контракт, которого не существует.
"""

import time
import unittest
import unittest.mock

from gateway import config, guards, journal, server
from gateway.tinvest import TInvestError
from tests.support import FakeBroker, JournalCase, add_playbook, flat_bars

CARD = {"stop": 97.0, "target": 110.0, "playbook": "test"}


class OrderPath(JournalCase):
    def setUp(self) -> None:
        super().setUp()
        self.broker = FakeBroker(bars=flat_bars())
        server._client = self.broker
        add_playbook()

    def buy(self, **kwargs):
        card = {**CARD, **kwargs.pop("card", {})}
        return server._order(
            "ORDER_DIRECTION_BUY", "uid", kwargs.pop("lots", 3), kwargs.pop("price", None),
            "тезис", card,
        )

    def test_покупка_проходит_и_пишет_карточку(self):
        result = self.buy()
        self.assertEqual(len(self.broker.posted), 1)
        row = journal.connect().execute("SELECT * FROM orders").fetchone()
        self.assertEqual(row["ticker"], "TEST")
        self.assertEqual(row["figi"], "FIGI1")
        self.assertIn("playbook", row["card"])
        self.assertEqual(result["checks"]["reward_risk"], 3.33)

    def test_ключ_идемпотентности_уходит_брокеру(self):
        self.buy()
        sent = self.broker.posted[0]["order_id"]
        intent = journal.connect().execute("SELECT * FROM order_intents").fetchone()
        self.assertEqual(intent["request_id"], sent)
        self.assertEqual(intent["state"], "sent")

    def drop(self):
        """Сетевой сбой в том виде, в каком его создаёт настоящий транспорт."""
        self.broker.post_error = TInvestError(
            "PostSandboxOrder: обрыв связи", retryable=True, answered=False
        )

    def request_id(self) -> str:
        return journal.connect().execute(
            "SELECT request_id FROM order_intents"
        ).fetchone()["request_id"]

    def settle(self) -> None:
        """Отодвинуть намерение за период устаканивания операций у брокера."""
        conn = journal.connect()
        conn.execute("UPDATE order_intents SET ts = ts - ?", (server.SETTLE_SECONDS + 1,))
        conn.commit()

    def test_намерение_пишется_до_сети(self):
        # Ответа не было: заявка могла быть принята брокером.
        self.drop()
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy()
        self.assertIn("прервалась", str(caught.exception))
        intent = journal.connect().execute("SELECT * FROM order_intents").fetchone()
        self.assertEqual(intent["state"], "unknown")
        self.assertIsNotNone(intent["request_id"])

    def test_обрыв_не_объявляется_отказом(self):
        """Главный дефект: сеть отваливалась, а заявка помечалась rejected.

        Ключ идемпотентности при этом закрывался, и следующая попытка уходила
        с новым ключом — поверх, возможно, уже исполненной первой.
        """
        self.drop()
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        self.assertNotEqual(self.request_id(), "")
        self.assertEqual(
            journal.connect().execute("SELECT state FROM order_intents").fetchone()["state"],
            "unknown",
        )
        # Пока брокер молчит, повтор запрещён.
        self.broker.post_error = None
        self.broker.state_error = TInvestError("недоступно", retryable=True, answered=False)
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy()
        self.assertIn("не читается", str(caught.exception))
        self.assertEqual(self.broker.posted, [])

    def test_точный_запрос_снимает_неизвестность(self):
        self.drop()
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        request_id = self.request_id()
        # Брокер отвечает, что такой заявки не знает, и операций по счёту нет.
        self.broker.post_error = None
        self.settle()
        self.buy()
        self.assertEqual(len(self.broker.posted), 1)
        self.assertEqual(journal.intent_by_request(request_id)["state"], "not_placed")

    def test_свежая_неизвестность_не_закрывается_сразу(self):
        """Операция появляется у брокера не мгновенно.

        Сразу после отправки «заявки нет и операции нет» неотличимо от
        «только что исполнилась, и операция ещё не доехала».
        """
        self.drop()
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        self.broker.post_error = None
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy()
        self.assertIn("слишком мало времени", str(caught.exception))
        self.assertEqual(self.broker.posted, [])

    def test_неизвестность_не_истекает_по_времени(self):
        """Пауза не является ответом брокера.

        Раньше через пять минут намерение объявлялось потерянным, и вход
        разрешался — то есть недоступность API работала как разрешение.
        """
        self.drop()
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        self.broker.post_error = None
        self.broker.state_error = TInvestError("недоступно", retryable=True, answered=False)
        conn = journal.connect()
        conn.execute("UPDATE order_intents SET ts = ts - ?", (24 * 3600,))
        conn.commit()
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        self.assertEqual(self.broker.posted, [])

    def test_висящая_заявка_блокирует_и_помечается(self):
        self.drop()
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        request_id = self.request_id()
        self.broker.post_error = None
        self.broker.states[request_id] = {
            "status": "EXECUTION_REPORT_STATUS_NEW", "order_id": "exch-1",
            "lots_requested": 3, "lots_executed": 0,
        }
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy()
        self.assertIn("висит активной", str(caught.exception))
        row = journal.intent_by_request(request_id)
        self.assertEqual(row["state"], "live")
        # Свой ключ и биржевой идентификатор хранятся раздельно.
        self.assertEqual(row["order_id"], "exch-1")

    def test_частичное_исполнение_остаётся_активным(self):
        self.drop()
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        request_id = self.request_id()
        self.broker.post_error = None
        self.broker.states[request_id] = {
            "status": "EXECUTION_REPORT_STATUS_PARTIALLYFILL", "order_id": "exch-2",
            "lots_requested": 3, "lots_executed": 1, "price": 100.0,
        }
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy()
        self.assertIn("исполнено 1 из 3", str(caught.exception))
        self.assertEqual(journal.intent_by_request(request_id)["state"], "live")

    def test_снятие_заявки_разблокирует_бумагу(self):
        self.drop()
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        request_id = self.request_id()
        self.broker.post_error = None
        self.broker.states[request_id] = {
            "status": "EXECUTION_REPORT_STATUS_CANCELLED", "order_id": "exch-3",
            "lots_requested": 3, "lots_executed": 0,
        }
        result = server.tool_cancel_order(request_id)
        self.assertEqual(result["lots_executed"], 0)
        self.assertEqual(journal.intent_by_request(request_id)["state"], "cancelled")
        self.buy()
        self.assertEqual(len(self.broker.posted), 1)

    def test_отмена_не_доказывает_нулевое_исполнение(self):
        """Подтверждение отмены — не доказательство, что не исполнилось ничего."""
        self.drop()
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        request_id = self.request_id()
        self.broker.post_error = None
        self.broker.states[request_id] = {
            "status": "EXECUTION_REPORT_STATUS_FILL", "order_id": "exch-4",
            "lots_requested": 3, "lots_executed": 3, "price": 100.0,
        }
        result = server.tool_cancel_order(request_id)
        self.assertEqual(result["lots_executed"], 3)
        self.assertIn("исполниться 3", result["note"])
        self.assertEqual(journal.intent_by_request(request_id)["state"], "filled")

    def test_операция_в_другую_сторону_не_закрывает_покупку(self):
        """Исполненная продажа не является доказательством судьбы покупки."""
        from datetime import datetime, timezone

        self.drop()
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        request_id = self.request_id()
        self.broker.post_error = None
        self.broker.ops = [
            {
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "type": "OPERATION_TYPE_SELL",
                "state": "OPERATION_STATE_EXECUTED",
                "figi": "FIGI1",
                "quantity": 3,
            }
        ]
        # Продажа не относится к покупке, поэтому намерение закрывается как
        # неотправленное — но именно по отсутствию покупки, а не по наличию
        # хоть какой-нибудь операции.
        self.settle()
        self.buy()
        self.assertEqual(journal.intent_by_request(request_id)["state"], "not_placed")

    def test_недоступность_операций_не_разблокирует(self):
        """Отсутствие ответа — не ответ «операции не было»."""
        from gateway import marketdata

        self.drop()
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        self.broker.post_error = None
        marketdata.remember_instrument(
            {"ticker": "TEST", "instrument_id": "uid", "figi": "FIGI1", "lot": 1}
        )

        def broken(days=7):
            raise TInvestError("операции недоступны", retryable=True, answered=False)

        self.broker.operations = broken
        self.settle()
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy()
        self.assertIn("не читаются", str(caught.exception))
        self.assertEqual(self.broker.posted, [])

    def test_явный_отказ_брокера_закрывает_ключ(self):
        self.broker.post_error = TInvestError("вне торгов", code="30079")
        with self.assertRaises(TInvestError):
            self.buy()
        intent = journal.connect().execute("SELECT * FROM order_intents").fetchone()
        self.assertEqual(intent["state"], "rejected")
        # Отказ однозначен, поэтому следующая заявка не блокируется.
        self.broker.post_error = None
        self.buy()
        self.assertEqual(len(self.broker.posted), 1)

    def test_недоступный_atr_отбивает_вход(self):
        """Сбой данных — причина отказать, а не работать без барьера.

        Раньше ошибка свечей возвращала ноль, а ноль означал «ATR не задан,
        проверку пропускаем»: недоступность источника выключала обязательный
        фильтр и пропускала стоп внутри шума.
        """
        def broken(*args, **kwargs):
            raise TInvestError("свечи недоступны", retryable=True, answered=False)

        self.broker.candles = broken
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy(card={"stop": 99.9, "target": 110.0})
        self.assertIn("ATR", str(caught.exception))
        self.assertEqual(self.broker.posted, [])

    def test_коротких_свечей_недостаточно(self):
        self.broker.bars = flat_bars(5)
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy()
        self.assertIn("ATR", str(caught.exception))
        self.assertEqual(self.broker.posted, [])

    def test_незакрытые_свечи_не_считаются_за_atr(self):
        self.broker.bars = [{**bar, "complete": False} for bar in flat_bars(60)]
        with self.assertRaises(guards.GuardRejection):
            self.buy()

    def test_вход_по_чужому_сетапу_отбивается(self):
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy(card={"playbook": "импульс-на-глаз"})
        self.assertIn("не зарегистрирован", str(caught.exception))
        self.assertEqual(self.broker.posted, [])

    def test_базовая_ставка_попадает_в_карточку_из_реестра(self):
        result = self.buy()
        self.assertEqual(result["checks"]["playbook_status"], "working")
        self.assertEqual(result["checks"]["base_rate"]["trades"], 40)
        row = journal.connect().execute("SELECT card FROM orders").fetchone()
        self.assertIn("measured_on", row["card"])

    def test_плохая_карточка_не_доходит_до_брокера(self):
        with self.assertRaises(guards.GuardRejection):
            self.buy(card={"target": 101.0})
        self.assertEqual(self.broker.posted, [])
        self.assertIsNone(journal.connect().execute("SELECT * FROM order_intents").fetchone())

    def test_продажа_проходит_при_остановке(self):
        guards.halt("тест")
        server._order("ORDER_DIRECTION_SELL", "uid", 1, None, "выхожу")
        self.assertEqual(len(self.broker.posted), 1)

    def test_покупка_не_проходит_при_остановке(self):
        guards.halt("тест")
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        self.assertEqual(self.broker.posted, [])

    def test_продажа_проходит_ниже_порога_капитала(self):
        self.broker.total = config.CAPITAL_FLOOR - 5_000
        server._order("ORDER_DIRECTION_SELL", "uid", 1, None, "спасаю остаток")
        self.assertEqual(len(self.broker.posted), 1)

    def test_покупка_ниже_порога_останавливает_прогон(self):
        self.broker.total = config.CAPITAL_FLOOR - 1
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        self.assertIn("ниже порога", guards.halted())
        self.assertEqual(self.broker.posted, [])

    def test_рыночная_заявка_режется_рыночным_лимитом(self):
        self.broker.limits = {
            "buy_max_lots": 50, "buy_max_market_lots": 2, "sell_max_lots": 0,
            "buy_money": 100_000.0,
        }
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy(lots=3)
        self.assertIn("рыночной", str(caught.exception))

    def test_лимитная_цена_сохраняется(self):
        self.buy(price=99.0, card={"stop": 96.0, "target": 108.0})
        row = journal.connect().execute("SELECT * FROM orders").fetchone()
        self.assertEqual(row["requested_price"], 99.0)


if __name__ == "__main__":
    unittest.main()


class Loopholes(JournalCase):
    """Обходные пути, которые лимиты должны закрывать."""

    def setUp(self) -> None:
        super().setUp()
        self.broker = FakeBroker(bars=flat_bars())
        server._client = self.broker
        add_playbook()

    def buy(self, **kwargs):
        return server._order(
            "ORDER_DIRECTION_BUY", kwargs.pop("uid", "uid"), 3, None, "тезис",
            dict(CARD),
        )

    def test_стопка_лимиток_не_обходит_лимит_позиций(self):
        # Позиций нет, но две заявки на вход уже висят.
        self.broker.active = [
            {"order_id": "1", "instrument_id": "a", "direction": "ORDER_DIRECTION_BUY"},
            {"order_id": "2", "instrument_id": "b", "direction": "ORDER_DIRECTION_BUY"},
        ]
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy()
        self.assertIn("заявок на вход", str(caught.exception))

    def test_заявка_на_продажу_место_не_занимает(self):
        self.broker.active = [
            {"order_id": "1", "instrument_id": "a", "direction": "ORDER_DIRECTION_SELL"},
            {"order_id": "2", "instrument_id": "b", "direction": "ORDER_DIRECTION_SELL"},
        ]
        self.buy()
        self.assertEqual(len(self.broker.posted), 1)

    def test_незакрытая_покупка_не_мешает_продать(self):
        # Покупка с неизвестной судьбой блокирует новую покупку, но не выход.
        self.broker.post_error = TInvestError("обрыв", retryable=True, answered=False)
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        self.broker.post_error = None
        server._order("ORDER_DIRECTION_SELL", "uid", 1, None, "выхожу")
        self.assertEqual(len(self.broker.posted), 1)

    def test_отказ_шлюза_не_тратит_лимит_частоты(self):
        # Карточка не прошла барьер — обращения к брокеру не было.
        with self.assertRaises(guards.GuardRejection):
            server._order(
                "ORDER_DIRECTION_BUY", "uid", 3, None, "тезис",
                {"stop": 97.0, "target": 101.0, "playbook": "test"},
            )
        self.assertEqual(journal.orders_last_hour(), 0)

    def test_лимит_частоты_не_мешает_продать(self):
        """Ограничитель темпа стоял общим и отбивал выход наравне со входом."""
        from tests.support import add_order

        for i in range(config.MAX_ORDERS_PER_HOUR):
            add_order(order_id=f"o{i}", ts=time.time() - 60)
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        server._order("ORDER_DIRECTION_SELL", "uid", 1, None, "выхожу")
        self.assertEqual(len(self.broker.posted), 1)

    def test_совокупный_риск_ограничен(self):
        """Риск складывается по портфелю, а не проверяется по одной заявке.

        Каждая идея по отдельности укладывается в 1R, но вместе они выносят
        на стол больше, чем прогон готов потерять за раз.
        """
        self.broker.positions = [
            {"instrument_id": "held", "ticker": "HELD", "quantity": 10}
        ]
        self.broker.limits = {
            "buy_max_lots": 1000, "buy_max_market_lots": 1000,
            "sell_max_lots": 0, "buy_money": 100_000.0,
        }
        journal.log_snapshot(100_000.0, 50_000.0, self.broker.positions)
        conn = journal.connect()
        conn.execute(
            "INSERT INTO orders (ts, order_id, instrument_id, direction, order_type,"
            " lots, lots_executed, price, card) VALUES (?, 'h1', 'held', 'buy',"
            " 'market', 1, 1, 100.0, ?)",
            (time.time() - 3600, '{"risk_rub": 1200.0}'),
        )
        conn.commit()
        self.assertEqual(journal.open_risk(), 1200.0)
        # Вход на 300 лотов при стопе в 3 ₽ — ровно 900 ₽ риска, то есть
        # меньше 1R. Вместе с уже открытыми 1200 ₽ это 2100 ₽ при потолке 2000.
        with self.assertRaises(guards.GuardRejection) as caught:
            server._order(
                "ORDER_DIRECTION_BUY", "uid", 300, None, "тезис",
                {"stop": 97.0, "target": 110.0, "playbook": "test"},
            )
        self.assertIn("под риском", str(caught.exception))
        self.assertEqual(self.broker.posted, [])

    def test_долив_в_открытую_позицию_запрещён(self):
        self.broker.positions = [
            {"instrument_id": "uid", "ticker": "TEST", "quantity": 10}
        ]
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy()
        self.assertIn("уже есть", str(caught.exception))

    def test_попытка_с_обрывом_тратит_лимит_частоты(self):
        self.broker.post_error = TInvestError("обрыв", retryable=True, answered=False)
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        self.assertEqual(journal.orders_last_hour(), 1)


class ApiRetries(JournalCase):
    """Песочница отвечает Internal error примерно на половину запросов."""

    def client(self, failures, code="70001"):
        from gateway import tinvest

        client = tinvest.SandboxClient("token", "acc")
        calls = {"n": 0}

        def flaky(service, method, payload=None):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise tinvest.TInvestError(f"{service}/{method}: Internal error", code=code)
            return {"operations": [{"figi": "F"}]}

        client._call_once = flaky
        return client, calls

    def test_временный_отказ_переживается(self):
        import gateway.tinvest as t

        client, calls = self.client(failures=3)
        with unittest.mock.patch.object(t.time, "sleep"):
            result = client.call("SandboxService", "GetSandboxOperations", {})
        self.assertEqual(len(result["operations"]), 1)
        self.assertEqual(calls["n"], 4)

    def test_повторы_видны_в_журнале(self):
        import gateway.tinvest as t

        client, _ = self.client(failures=2)
        with unittest.mock.patch.object(t.time, "sleep"):
            client.call("SandboxService", "GetSandboxOperations", {})
        rows = journal.connect().execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='api_retry'"
        ).fetchone()
        self.assertEqual(rows["n"], 2)

    def test_содержательная_ошибка_не_повторяется(self):
        import gateway.tinvest as t

        # 30079 — вне торговой сессии. Повторять бессмысленно.
        client, calls = self.client(failures=99, code="30079")
        with unittest.mock.patch.object(t.time, "sleep"):
            with self.assertRaises(t.TInvestError):
                client.call("SandboxService", "PostSandboxOrder", {})
        self.assertEqual(calls["n"], 1)

    def test_исчерпание_повторов_даёт_ошибку(self):
        import gateway.tinvest as t

        client, calls = self.client(failures=99)
        with unittest.mock.patch.object(t.time, "sleep"):
            with self.assertRaises(t.TInvestError):
                client.call("SandboxService", "GetSandboxOperations", {})
        self.assertEqual(calls["n"], config.API_RETRIES + 1)
