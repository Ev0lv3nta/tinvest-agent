"""Супервизор: доставка без потерь, наблюдатели, сверка."""

import time
import unittest

from gateway import journal
from supervisor import main as supervisor_main
from supervisor import reconcile, watcher
from supervisor.appserver import AppServerError, Busy
from tests.support import JournalCase, add_order, msk


class FakeCodex:
    def __init__(self, fail=None):
        self.delivered: list[tuple[str, bool]] = []
        self.fail = fail
        self.busy = False
        self.turn_started = 0.0
        self.last_inbound = time.time()
        self.thread_id = "T"

    def deliver(self, text, urgent=False):
        if self.fail:
            raise self.fail
        self.delivered.append((text, urgent))
        return "turn"

    def alive(self):
        return True

    def interrupt(self):
        pass


class FakeBot:
    def __init__(self):
        self.sent: list[str] = []
        self.token = "x"

    def send(self, text, keyboard=True):
        self.sent.append(text)
        return True

    def set_commands(self):
        pass


class Delivery(JournalCase):
    def setUp(self):
        super().setUp()
        supervisor_main._HEALTH_CACHE.update({"ts": time.time(), "ok": True, "detail": "ok"})
        self.supervisor = supervisor_main.Supervisor.__new__(supervisor_main.Supervisor)
        self.supervisor.codex = FakeCodex()
        self.supervisor.bot = FakeBot()
        self.supervisor.last_activity = time.time()
        self.supervisor.last_delivery = time.time()
        self.supervisor.last_check = time.time()
        self.supervisor.last_report_date = ""
        self.supervisor.last_reconcile = time.time()
        self.supervisor.watcher = None
        self.supervisor._seen_methods = set()
        self.supervisor._reasoning = {}

    def test_будильник_не_гаснет_при_обрыве(self):
        supervisor_main._HEALTH_CACHE.update({"ts": time.time(), "ok": False, "detail": "HTTP 530"})
        journal.schedule_wakeup(time.time() - 10, "проверить позицию")
        self.supervisor.tick_wakeup()
        # Повод остался активным и сдвинулся вперёд.
        self.assertEqual(len(journal.active_wakeups()), 1)
        self.assertEqual(journal.due_wakeups(), [])

    def test_будильник_гаснет_после_доставки(self):
        journal.schedule_wakeup(time.time() - 10, "проверить позицию")
        self.supervisor.tick_wakeup()
        self.assertEqual(journal.active_wakeups(), [])
        self.assertEqual(len(self.supervisor.codex.delivered), 1)

    def test_сообщение_остаётся_в_очереди_при_обрыве(self):
        supervisor_main._HEALTH_CACHE.update({"ts": time.time(), "ok": False, "detail": "HTTP 530"})
        journal.enqueue_message("почему бездействуешь")
        self.supervisor.tick_inbox()
        self.assertEqual(len(journal.peek_messages()), 1)

    def test_сообщение_оператора_срочное(self):
        journal.enqueue_message("почему бездействуешь")
        self.supervisor.tick_inbox()
        self.assertEqual(journal.peek_messages(), [])
        self.assertTrue(self.supervisor.codex.delivered[0][1])

    def test_служебное_не_срочное(self):
        journal.enqueue_message("процесс перезапускался", source="system")
        self.supervisor.tick_inbox()
        self.assertFalse(self.supervisor.codex.delivered[0][1])

    def test_занятый_ход_не_теряет_рутину(self):
        self.supervisor.codex.fail = Busy("занят")
        journal.schedule_wakeup(time.time() - 10, "повод")
        self.supervisor.tick_wakeup()
        self.assertEqual(len(journal.active_wakeups()), 1)

    def test_отчёт_не_отмечается_при_отказе(self):
        self.supervisor.codex.fail = AppServerError("нет ответа")
        self.supervisor.last_report_date = ""
        moment = supervisor_main.now_msk().replace(hour=19, minute=0)
        supervisor_main.now_msk = lambda: moment
        self.supervisor.tick_report()
        self.assertEqual(self.supervisor.last_report_date, "")

    def test_блок_состояния_содержит_цифры(self):
        journal.log_snapshot(99_500.0, 50_000.0, [{"ticker": "SBER", "lots": 2, "yield": -50}])
        add_order(ts=time.time(), direction="buy", lots_executed=1)
        block = self.supervisor.state_block()
        self.assertIn("99 500", block)
        self.assertIn("SBER", block)
        self.assertIn("входов 1", block)


class Watches(JournalCase):
    def test_условия_срабатывают(self):
        rows = []

        class Row(dict):
            def __getitem__(self, key):
                return dict.__getitem__(self, key)

        above = Row(kind="price_above", threshold=105.0, base_price=None)
        below = Row(kind="price_below", threshold=95.0, base_price=None)
        move = Row(kind="pct_move", threshold=2.0, base_price=100.0)
        self.assertTrue(watcher.satisfied(above, 105.0))
        self.assertFalse(watcher.satisfied(above, 104.9))
        self.assertTrue(watcher.satisfied(below, 94.0))
        self.assertFalse(watcher.satisfied(below, 96.0))
        self.assertTrue(watcher.satisfied(move, 102.5))
        self.assertTrue(watcher.satisfied(move, 97.5))
        self.assertFalse(watcher.satisfied(move, 101.0))
        self.assertFalse(watcher.satisfied(Row(kind="wat", threshold=1, base_price=None), 5))

    def test_срабатывание_кладётся_в_очередь(self):
        journal.add_watch("uid", "TRNFP", "price_below", 1025.0, "стоп", time.time() + 3600)

        class Client:
            def last_price(self, ids):
                return [{"instrument_id": i, "price": 1024.0} for i in ids]

        worker = watcher.Watcher(
            on_fire=supervisor_main.Supervisor._watches_fired,
            market_open=lambda: True,
            client_factory=Client,
        )
        fired = worker.tick()
        self.assertEqual(len(fired), 1)
        self.assertIn("1025", fired[0])
        self.assertEqual(journal.active_watches(), [])
        self.assertEqual(len(journal.peek_messages()), 1)

    def test_вне_торгов_не_срабатывает(self):
        journal.add_watch("uid", "T", "price_below", 1025.0, "стоп", time.time() + 3600)
        worker = watcher.Watcher(lambda t: None, lambda: False, lambda: None)
        self.assertEqual(worker.tick(), [])
        self.assertEqual(len(journal.active_watches()), 1)


class Reconcile(JournalCase):
    def setUp(self):
        super().setUp()
        reconcile._client = self.broker = FakeReconcileBroker()

    def test_нулевое_исполнение_не_объясняет_операцию(self):
        # Отменённая лимитка не должна прикрывать настоящую покупку.
        add_order(ts=msk(11, 23), figi="F", direction="buy", lots_executed=0, price=0.0)
        self.broker.ops = [op(msk(11, 25), "OPERATION_TYPE_BUY", "F", 4)]
        self.assertEqual(len(reconcile.unexplained()), 1)

    def test_наша_сделка_объясняется(self):
        add_order(ts=msk(11, 23), figi="F", direction="buy", lots_executed=4)
        self.broker.ops = [op(msk(11, 25), "OPERATION_TYPE_BUY", "F", 4)]
        self.assertEqual(reconcile.unexplained(), [])

    def test_частичное_исполнение_не_даёт_ложных_тревог(self):
        add_order(ts=msk(11, 23), figi="F", direction="buy", lots_executed=6)
        self.broker.ops = [
            op(msk(11, 24), "OPERATION_TYPE_BUY", "F", 2),
            op(msk(11, 25), "OPERATION_TYPE_BUY", "F", 3),
            op(msk(11, 26), "OPERATION_TYPE_BUY", "F", 1),
        ]
        self.assertEqual(reconcile.unexplained(), [])

    def test_лишний_объём_остаётся_сиротой(self):
        add_order(ts=msk(11, 23), figi="F", direction="buy", lots_executed=4)
        self.broker.ops = [
            op(msk(11, 24), "OPERATION_TYPE_BUY", "F", 4),
            op(msk(11, 30), "OPERATION_TYPE_BUY", "F", 5),
        ]
        orphans = reconcile.unexplained()
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["quantity"], 5)

    def test_другая_бумага_не_объясняет(self):
        add_order(ts=msk(11, 23), figi="F", direction="buy", lots_executed=4)
        self.broker.ops = [op(msk(11, 25), "OPERATION_TYPE_BUY", "OTHER", 4)]
        self.assertEqual(len(reconcile.unexplained()), 1)

    def test_противоположное_направление_не_объясняет(self):
        add_order(ts=msk(11, 23), figi="F", direction="buy", lots_executed=4)
        self.broker.ops = [op(msk(11, 25), "OPERATION_TYPE_SELL", "F", 4)]
        self.assertEqual(len(reconcile.unexplained()), 1)

    def test_далеко_по_времени_не_объясняет(self):
        add_order(ts=msk(11, 23), figi="F", direction="buy", lots_executed=4)
        self.broker.ops = [op(msk(15, 0), "OPERATION_TYPE_BUY", "F", 4)]
        self.assertEqual(len(reconcile.unexplained()), 1)

    def test_лоты_пересчитываются_в_штуки(self):
        # В журнале лоты, в операциях штуки: при лоте 10 четыре лота это 40 штук.
        self.broker.lot = 10
        add_order(ts=msk(11, 23), figi="F", direction="buy", lots_executed=4)
        self.broker.ops = [op(msk(11, 25), "OPERATION_TYPE_BUY", "F", 40)]
        self.assertEqual(reconcile.unexplained(), [])

    def test_пополнение_не_считается_сделкой(self):
        self.broker.ops = [op(msk(9, 0), "OPERATION_TYPE_INPUT", "", 0)]
        self.assertEqual(reconcile.unexplained(), [])

    def test_синхронизация_закрывает_исчезнувшую_заявку(self):
        add_order(ts=time.time(), order_id="A", lots_executed=0, lots=4, price=0.0)
        self.broker.orders = []
        self.assertEqual(reconcile.sync_orders(), 1)
        row = journal.connect().execute("SELECT * FROM orders WHERE order_id='A'").fetchone()
        self.assertIn("CANCELLED", row["status"])
        self.assertIsNotNone(row["closed_ts"])

    def test_синхронизация_подхватывает_исполнение(self):
        add_order(ts=time.time(), order_id="B", lots_executed=0, lots=4, price=0.0)
        self.broker.orders = [
            {"order_id": "B", "status": "EXECUTION_REPORT_STATUS_FILL",
             "lots_executed": 4, "price": 101.0}
        ]
        reconcile.sync_orders()
        row = journal.connect().execute("SELECT * FROM orders WHERE order_id='B'").fetchone()
        self.assertEqual(row["lots_executed"], 4)


def op(ts, kind, figi, quantity):
    from datetime import datetime, timezone
    return {
        "date": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": kind, "state": "OPERATION_STATE_EXECUTED", "figi": figi,
        "quantity": quantity, "payment": -100.0, "price": 25.0,
    }


class FakeReconcileBroker:
    def __init__(self):
        self.ops: list[dict] = []
        self.orders: list[dict] = []
        self.lot = 1

    def operations(self, days=7):
        return list(self.ops)

    def active_orders(self):
        return list(self.orders)

    def instrument_by_uid(self, uid):
        return {"lot": self.lot, "ticker": "TEST", "figi": "F"}


if __name__ == "__main__":
    unittest.main()


class UrlWatches(JournalCase):
    def test_изменение_страницы_будит(self):
        from gateway import marketdata

        pages = {"u": "первая версия"}
        marketdata.page_hash = lambda url, timeout=20: __import__("hashlib").sha256(
            pages[url].encode()
        ).hexdigest()

        journal.add_watch(
            "u", "", "url_changed", 0.0, "ждём МСФО", time.time() + 3600,
            url="u", content_hash=marketdata.page_hash("u"),
        )
        worker = watcher.Watcher(
            supervisor_main.Supervisor._watches_fired, lambda: False, lambda: None
        )
        # Ничего не изменилось — тишина, и наблюдатель остаётся.
        self.assertEqual(worker.tick_urls(), [])
        self.assertEqual(len(journal.active_watches()), 1)

        pages["u"] = "опубликован отчёт за первое полугодие"
        fired = worker.tick_urls()
        self.assertEqual(len(fired), 1)
        self.assertIn("ждём МСФО", fired[0])
        self.assertEqual(journal.active_watches(), [])
        self.assertEqual(len(journal.peek_messages()), 1)

    def test_недоступная_страница_не_ломает_наблюдение(self):
        from gateway import marketdata

        def broken(url, timeout=20):
            raise OSError("сайт лежит")

        marketdata.page_hash = broken
        journal.add_watch(
            "u", "", "url_changed", 0.0, "ждём", time.time() + 3600,
            url="u", content_hash="старый",
        )
        worker = watcher.Watcher(lambda t: None, lambda: False, lambda: None)
        self.assertEqual(worker.tick_urls(), [])
        self.assertEqual(len(journal.active_watches()), 1)

    def test_разметка_не_влияет_на_хеш(self):
        import importlib

        from gateway import marketdata as md

        importlib.reload(md)
        a = md._SPACE.sub(" ", md._MARKUP.sub(" ", md._TAGS.sub(" ", "<div>Отчёт <b>вышел</b></div>")))
        b = md._SPACE.sub(" ", md._MARKUP.sub(" ", md._TAGS.sub(" ", "<p>Отчёт  <i>вышел</i></p><script>x=1</script>")))
        self.assertEqual(a.strip(), b.strip())
