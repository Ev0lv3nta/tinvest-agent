"""Путь заявки: карточка, идемпотентность, поведение при обрыве связи."""

import unittest

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
        self.assertIn("неизвестной судьбой", str(caught.exception))
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
