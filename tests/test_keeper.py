"""Сторож позиций: выход исполняет код, а не пробуждение модели.

Проверяется главное свойство — решение выйти принимается и переживает всё,
что может случиться между принятием и исполнением: обрыв связи, отказ
брокера, перезапуск процесса и вернувшуюся назад цену.
"""

import time
import unittest

from gateway import journal, server
from supervisor import keeper
from tests.support import FakeBroker, JournalCase, add_playbook, flat_bars


class KeeperCase(JournalCase):
    def setUp(self):
        super().setUp()
        self.broker = FakeBroker(bars=flat_bars())
        self.broker.positions = [
            {"instrument_id": "uid", "ticker": "TEST", "quantity": 10, "lots": 10}
        ]
        server._client = self.broker
        self.notified = []
        self.keeper = keeper.Keeper(
            client_factory=lambda: self.broker, notify=self.notified.append
        )
        add_playbook()

    def mandate(self, stop=95.0, target=120.0, deadline=None) -> int:
        return journal.add_mandate(
            {
                "instrument_id": "uid", "ticker": "TEST", "request_id": "r1",
                "stop": stop, "target": target,
                "deadline_ts": deadline if deadline is not None else time.time() + 3600,
            }
        )


class Triggers(KeeperCase):
    def test_стоп_закрывает_позицию(self):
        self.mandate(stop=95.0)
        self.broker.price = 94.0
        done = self.keeper.tick()
        self.assertEqual(len(done), 1)
        self.assertEqual(len(self.broker.posted), 1)
        self.assertEqual(self.broker.posted[0]["direction"], "ORDER_DIRECTION_SELL")
        self.assertEqual(self.broker.posted[0]["lots"], 10)
        self.assertEqual(journal.open_mandates(), [])

    def test_цель_закрывает_позицию(self):
        self.mandate(target=120.0)
        self.broker.price = 121.0
        self.assertEqual(len(self.keeper.tick()), 1)
        self.assertEqual(self.broker.posted[0]["direction"], "ORDER_DIRECTION_SELL")

    def test_истёкший_срок_закрывает_позицию(self):
        self.mandate(deadline=time.time() - 1)
        self.broker.price = 100.0
        done = self.keeper.tick()
        self.assertEqual(len(done), 1)
        self.assertIn("срок", done[0])

    def test_внутри_коридора_ничего_не_происходит(self):
        self.mandate(stop=95.0, target=120.0)
        self.broker.price = 100.0
        self.assertEqual(self.keeper.tick(), [])
        self.assertEqual(self.broker.posted, [])
        self.assertEqual(len(journal.open_mandates()), 1)

    def test_молчание_источника_не_повод_продавать(self):
        """Отсутствие цены — это незнание, а не сигнал."""
        self.mandate()
        self.broker.last_price = lambda ids: []
        self.assertEqual(self.keeper.tick(), [])
        self.assertEqual(self.broker.posted, [])
        self.assertEqual(len(journal.open_mandates()), 1)

    def test_нет_позиции_нет_обязательства(self):
        self.mandate()
        self.broker.positions = []
        self.assertEqual(self.keeper.tick(), [])
        self.assertEqual(journal.open_mandates(), [])


class Latching(KeeperCase):
    def refuse(self):
        """Явный отказ брокера: судьба заявки известна, повтор разрешён."""
        from gateway.tinvest import TInvestError

        self.broker.post_error = TInvestError("вне торгов", code="30079", http_status=400)

    def test_требование_защёлкивается_до_заявки(self):
        """Между решением и исполнением может случиться что угодно.

        Сбой на отправке не должен означать, что о решении забыли: цена
        вернулась выше стопа, а выходить всё равно надо.
        """
        self.mandate(stop=95.0)
        self.broker.price = 94.0
        self.refuse()
        self.assertEqual(self.keeper.tick(), [])
        row = journal.open_mandates()[0]
        self.assertIsNotNone(row["latched_ts"])
        self.assertIn("ниже стопа", row["latched_why"])
        self.assertTrue(self.notified)

    def test_вернувшаяся_цена_не_отменяет_решение(self):
        self.mandate(stop=95.0)
        self.broker.price = 94.0
        self.refuse()
        self.keeper.tick()
        # Цена отскочила, но решение уже принято.
        self.broker.price = 110.0
        self.broker.post_error = None
        self.keeper.last_failure = 0.0
        done = self.keeper.tick()
        self.assertEqual(len(done), 1)
        self.assertEqual(self.broker.posted[0]["direction"], "ORDER_DIRECTION_SELL")

    def test_защёлка_переживает_перезапуск(self):
        self.mandate(stop=95.0)
        self.broker.price = 94.0
        self.refuse()
        self.keeper.tick()
        # Новый процесс: состояние берётся только из базы.
        fresh = keeper.Keeper(client_factory=lambda: self.broker, notify=lambda t: None)
        self.broker.post_error = None
        self.broker.price = 100.0
        self.assertEqual(len(fresh.tick()), 1)

    def test_неизвестная_судьба_продажи_блокирует_повтор(self):
        """Оборванная продажа могла исполниться: вторая продала бы дважды.

        Требование при этом остаётся защёлкнутым — оно снимется, когда
        состояние прежней заявки станет известно.
        """
        from gateway.tinvest import TInvestError

        self.mandate(stop=95.0)
        self.broker.price = 94.0
        self.broker.post_error = TInvestError("обрыв", retryable=True, answered=False)
        self.keeper.tick()
        self.broker.post_error = None
        self.keeper.last_failure = 0.0
        self.assertEqual(self.keeper.tick(), [])
        self.assertEqual(self.broker.posted, [])
        self.assertIsNotNone(journal.open_mandates()[0]["latched_ts"])


class Mandates(KeeperCase):
    def test_стоп_можно_только_подтянуть(self):
        self.mandate(stop=95.0)
        self.assertEqual(journal.tighten_mandate("uid", 97.0)["stop"], 97.0)
        with self.assertRaises(ValueError) as caught:
            journal.tighten_mandate("uid", 96.0)
        self.assertIn("только подтянуть", str(caught.exception))

    def test_покупка_создаёт_обязательство(self):
        self.broker.positions = []
        server._order(
            "ORDER_DIRECTION_BUY", "uid", 3, None, "тезис",
            {"stop": 97.0, "target": 110.0, "playbook": "test"},
        )
        mandate = journal.mandate_for("uid")
        self.assertEqual(mandate["stop"], 97.0)
        self.assertEqual(mandate["target"], 110.0)
        self.assertIsNotNone(mandate["deadline_ts"])

    def test_новый_вход_заменяет_старое_обязательство(self):
        self.mandate(stop=95.0)
        self.broker.positions = []
        server._order(
            "ORDER_DIRECTION_BUY", "uid", 3, None, "тезис",
            {"stop": 97.0, "target": 110.0, "playbook": "test"},
        )
        self.assertEqual(len(journal.open_mandates()), 1)
        self.assertEqual(journal.mandate_for("uid")["stop"], 97.0)

    def test_продажа_снимает_обязательство(self):
        self.mandate()
        self.broker.positions = []
        server._order("ORDER_DIRECTION_SELL", "uid", 10, None, "выхожу")
        self.assertEqual(journal.open_mandates(), [])

    def test_обязательство_видно_в_портфеле(self):
        self.mandate(stop=95.0)
        data = server.tool_portfolio()
        self.assertEqual(data["positions"][0]["mandate"]["stop"], 95.0)


if __name__ == "__main__":
    unittest.main()


class Backoff(KeeperCase):
    def test_пауза_между_попытками_растёт(self):
        """Вне торгов брокер отбивает заявку, и упорство здесь — только шум."""
        self.assertEqual(self.keeper.backoff(), keeper.RETRY_DELAY)
        self.keeper.failures = 3
        self.assertEqual(self.keeper.backoff(), keeper.RETRY_DELAY * 4)
        self.keeper.failures = 99
        self.assertEqual(self.keeper.backoff(), keeper.RETRY_MAX)

    def test_оператора_не_будят_каждые_полминуты(self):
        from gateway.tinvest import TInvestError

        self.mandate(stop=95.0)
        self.broker.price = 94.0
        self.broker.post_error = TInvestError("вне торгов", code="30079", http_status=400)
        self.keeper.tick()
        self.assertEqual(len(self.notified), 1)
        self.keeper.last_failure = 0.0
        self.keeper.tick()
        self.assertEqual(len(self.notified), 1)

    def test_после_успеха_счётчик_сбрасывается(self):
        self.mandate(stop=95.0)
        self.broker.price = 94.0
        self.keeper.failures = 5
        self.keeper.tick()
        self.assertEqual(self.keeper.failures, 0)
