"""Путь заявки: карточка, идемпотентность, поведение при обрыве связи."""

import unittest
import unittest.mock

from gateway import config, guards, journal, server
from gateway.tinvest import TInvestError
from tests.support import FakeBroker, JournalCase, flat_bars

CARD = {"stop": 97.0, "target": 110.0, "playbook": "test", "base_rate": "3 из 5"}


class OrderPath(JournalCase):
    def setUp(self) -> None:
        super().setUp()
        self.broker = FakeBroker(bars=flat_bars())
        server._client = self.broker

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

    def test_намерение_пишется_до_сети(self):
        # Брокер падает сетевой ошибкой: заявка могла быть принята.
        self.broker.post_error = ConnectionResetError("оборвалось")
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy()
        self.assertIn("оборвалась", str(caught.exception))
        intent = journal.connect().execute("SELECT * FROM order_intents").fetchone()
        self.assertEqual(intent["state"], "ambiguous")
        self.assertIsNotNone(intent["request_id"])

    def test_повтор_после_обрыва_блокируется(self):
        self.broker.post_error = ConnectionResetError("оборвалось")
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        self.broker.post_error = None
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy()
        self.assertIn("не закрыта", str(caught.exception))
        self.assertEqual(self.broker.posted, [])

    def test_висящая_заявка_блокирует_и_помечается(self):
        self.broker.post_error = ConnectionResetError("оборвалось")
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        request_id = journal.connect().execute(
            "SELECT request_id FROM order_intents"
        ).fetchone()["request_id"]
        self.broker.post_error = None
        self.broker.active = [{"order_id": request_id}]
        with self.assertRaises(guards.GuardRejection) as caught:
            self.buy()
        self.assertIn("висит активной", str(caught.exception))
        self.assertEqual(
            journal.intent_by_request(request_id)["state"], "live"
        )

    def test_снятие_заявки_разблокирует_бумагу(self):
        self.broker.post_error = ConnectionResetError("оборвалось")
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        request_id = journal.connect().execute(
            "SELECT request_id FROM order_intents"
        ).fetchone()["request_id"]
        self.broker.post_error = None
        server.tool_cancel_order(request_id)
        self.assertEqual(journal.intent_by_request(request_id)["state"], "cancelled")
        self.buy()
        self.assertEqual(len(self.broker.posted), 1)

    def test_исполнившаяся_заявка_разблокирует_бумагу(self):
        self.broker.post_error = ConnectionResetError("оборвалось")
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        self.broker.post_error = None
        # Заявки нет среди активных, но в операциях есть исполнение.
        from datetime import datetime, timezone

        self.broker.ops = [
            {
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "type": "OPERATION_TYPE_BUY",
                "state": "OPERATION_STATE_EXECUTED",
                "figi": "FIGI1",
                "quantity": 3,
            }
        ]
        self.buy()
        self.assertEqual(len(self.broker.posted), 1)

    def test_потерянная_заявка_разблокирует_по_истечении(self):
        self.broker.post_error = ConnectionResetError("оборвалось")
        with self.assertRaises(guards.GuardRejection):
            self.buy()
        self.broker.post_error = None
        # Прошло больше пяти минут, заявка не появилась нигде.
        conn = journal.connect()
        conn.execute(
            "UPDATE order_intents SET ts = ts - ?", (server.LOST_AFTER + 60,)
        )
        conn.commit()
        self.buy()
        self.assertEqual(len(self.broker.posted), 1)

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
        self.broker.post_error = ConnectionResetError("оборвалось")
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
                {"stop": 97.0, "target": 101.0, "playbook": "p", "base_rate": "b"},
            )
        self.assertEqual(journal.orders_last_hour(), 0)

    def test_попытка_с_обрывом_тратит_лимит_частоты(self):
        self.broker.post_error = ConnectionResetError("оборвалось")
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
