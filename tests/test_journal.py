"""Журнал: доставка, будильники, дневные опоры."""

import time
import unittest

from gateway import journal
from tests.support import JournalCase, add_order, msk


class Messages(JournalCase):
    def test_чтение_не_помечает_доставленным(self):
        journal.enqueue_message("привет")
        self.assertEqual(len(journal.peek_messages()), 1)
        # Канал лёг — сообщение должно остаться в очереди.
        self.assertEqual(len(journal.peek_messages()), 1)

    def test_пометка_убирает_из_очереди(self):
        message_id = journal.enqueue_message("привет")
        journal.mark_message_delivered(message_id)
        self.assertEqual(journal.peek_messages(), [])


class Wakeups(JournalCase):
    def test_несколько_будильников_живут_вместе(self):
        journal.schedule_wakeup(time.time() + 600, "отчёт RNFT")
        journal.schedule_wakeup(time.time() + 300, "проверить позицию")
        active = journal.active_wakeups()
        self.assertEqual(len(active), 2)
        # Ближайший — первый.
        self.assertEqual(active[0]["reason"], "проверить позицию")

    def test_лимит_активных(self):
        for i in range(journal.MAX_ACTIVE_WAKEUPS):
            journal.schedule_wakeup(time.time() + 60 + i, f"повод {i}")
        with self.assertRaises(ValueError):
            journal.schedule_wakeup(time.time() + 900, "лишний")

    def test_отмена_освобождает_место(self):
        first = journal.schedule_wakeup(time.time() + 60, "повод")
        self.assertTrue(journal.cancel_wakeup(first))
        self.assertFalse(journal.cancel_wakeup(first))
        self.assertEqual(journal.active_wakeups(), [])

    def test_просроченные_отдаются_все(self):
        journal.schedule_wakeup(time.time() - 10, "первый")
        journal.schedule_wakeup(time.time() - 5, "второй")
        journal.schedule_wakeup(time.time() + 600, "не пора")
        self.assertEqual(len(journal.due_wakeups()), 2)

    def test_перенос_сохраняет_повод(self):
        wakeup_id = journal.schedule_wakeup(time.time() - 10, "повод")
        journal.postpone_wakeup(wakeup_id, 120)
        self.assertEqual(journal.due_wakeups(), [])
        self.assertEqual(len(journal.active_wakeups()), 1)


class DayBaseline(JournalCase):
    def test_опора_берётся_с_первого_среза_дня(self):
        journal.connect().execute(
            "INSERT INTO snapshots (ts, total, cash, positions) VALUES (?,?,?,?)",
            (msk(10, 5), 99_000.0, 0.0, "[]"),
        )
        journal.connect().execute(
            "INSERT INTO snapshots (ts, total, cash, positions) VALUES (?,?,?,?)",
            (msk(15, 0), 95_000.0, 0.0, "[]"),
        )
        journal.connect().commit()
        self.assertEqual(journal.day_open_equity(), 99_000.0)
        self.assertEqual(journal.day_result(97_000.0), -2_000.0)

    def test_без_срезов_опоры_нет(self):
        self.assertIsNone(journal.day_open_equity())
        self.assertIsNone(journal.day_result(1.0))

    def test_вчерашний_срез_если_сегодня_пусто(self):
        journal.connect().execute(
            "INSERT INTO snapshots (ts, total, cash, positions) VALUES (?,?,?,?)",
            (msk(18, 0, day=23), 101_000.0, 0.0, "[]"),
        )
        journal.connect().commit()
        self.assertEqual(journal.day_open_equity(), 101_000.0)

    def test_полночь_считается_по_москве(self):
        # 23:30 МСК и 00:30 МСК — разные торговые дни.
        late = msk(23, 30, day=24)
        early = msk(0, 30, day=25)
        self.assertNotEqual(journal.day_start_ts(late), journal.day_start_ts(early))
        self.assertEqual(journal.day_start_ts(msk(10, 0)), msk(0, 0))


class Orders(JournalCase):
    def test_входы_считаются_только_исполненные(self):
        add_order(ts=msk(11), direction="buy", lots_executed=0)
        add_order(ts=msk(12), direction="buy", lots_executed=2)
        add_order(ts=msk(13), direction="sell", lots_executed=2)
        self.assertEqual(journal.entries_today(), 1)

    def test_статус_заявки_обновляется(self):
        add_order(ts=msk(11), order_id="A", lots_executed=0, price=0.0)
        journal.update_order_status("A", "EXECUTION_REPORT_STATUS_FILL", 4, 101.5)
        row = journal.connect().execute("SELECT * FROM orders WHERE order_id='A'").fetchone()
        self.assertEqual(row["lots_executed"], 4)
        self.assertEqual(row["price"], 101.5)
        self.assertIsNotNone(row["closed_ts"])

    def test_отмена_закрывает_заявку_не_трогая_цену(self):
        add_order(ts=msk(11), order_id="B", lots_executed=0, price=0.0)
        journal.update_order_status("B", "EXECUTION_REPORT_STATUS_CANCELLED", 0, 0.0)
        row = journal.connect().execute("SELECT * FROM orders WHERE order_id='B'").fetchone()
        self.assertEqual(row["price"], 0.0)
        self.assertIsNotNone(row["closed_ts"])
        self.assertEqual(journal.open_orders(), [])


class Watches(JournalCase):
    def test_добавление_и_отмена(self):
        watch_id = journal.add_watch("uid", "TEST", "price_above", 105.0, "пробой", time.time() + 3600)
        self.assertEqual(len(journal.active_watches()), 1)
        self.assertTrue(journal.cancel_watch(watch_id))
        self.assertEqual(journal.active_watches(), [])

    def test_неизвестное_условие_отклоняется(self):
        with self.assertRaises(ValueError):
            journal.add_watch("uid", "TEST", "price_maybe", 1.0, "x", time.time() + 60)

    def test_истёкшие_не_отдаются(self):
        journal.add_watch("uid", "TEST", "price_above", 105.0, "старое", time.time() - 1)
        self.assertEqual(journal.active_watches(), [])
        self.assertEqual(journal.expire_watches(), 1)

    def test_срабатывание_снимает_с_наблюдения(self):
        watch_id = journal.add_watch("uid", "TEST", "price_below", 95.0, "стоп", time.time() + 60)
        journal.mark_watch_fired(watch_id, 94.5)
        self.assertEqual(journal.active_watches(), [])


class Usage(JournalCase):
    def test_расход_суммируется(self):
        journal.log_usage("t1", {"input": 100, "output": 10, "total": 110,
                                 "context_used": 5000, "context_window": 200000})
        journal.log_usage("t2", {"input": 200, "output": 20, "total": 220,
                                 "context_used": 9000, "context_window": 200000})
        stats = journal.usage_since(3600)
        self.assertEqual(stats["turns"], 2)
        self.assertEqual(stats["total"], 330)
        self.assertEqual(stats["context_used"], 9000)


if __name__ == "__main__":
    unittest.main()
