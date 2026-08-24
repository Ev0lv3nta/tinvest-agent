"""Данные: сводка вместо серии, кеш, страховка транспорта."""

import json
import os
import unittest
from pathlib import Path

from gateway import config, marketdata, server
from tests.support import FakeBroker, JournalCase, flat_bars


class Summary(unittest.TestCase):
    def test_сводка_вместо_серии(self):
        broker = FakeBroker(bars=flat_bars(980, price=1000.0, spread=2.0))
        summary = marketdata.candles(
            broker, "uid", "CANDLE_INTERVAL_1_MIN", 1, name="TEST", refresh=True
        )
        text = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        # Сырая серия — сотни килобайт; сводка обязана быть на порядки меньше.
        self.assertLess(len(text), 1500)
        self.assertEqual(summary["bars"], 980)
        self.assertTrue(Path(summary["file"]).is_file())
        self.assertEqual(summary["columns"], "time,open,high,low,close,volume")

    def test_файл_читается_обратно(self):
        broker = FakeBroker(bars=flat_bars(30))
        summary = marketdata.candles(
            broker, "uid", "CANDLE_INTERVAL_15_MIN", 1, name="RT", refresh=True
        )
        bars = marketdata.read_csv(Path(summary["file"]))
        self.assertEqual(len(bars), 30)
        self.assertEqual(bars[0]["open"], 100.0)

    def test_повтор_не_ходит_в_api(self):
        broker = FakeBroker(bars=flat_bars(30))
        marketdata.candles(broker, "uid", "CANDLE_INTERVAL_HOUR", 1, name="CA", refresh=True)
        calls = len(broker.posted)  # posted не растёт, считаем по-другому

        class Counting(FakeBroker):
            hits = 0

            def candles(self, *args, **kwargs):
                Counting.hits += 1
                return flat_bars(30)

        counter = Counting()
        marketdata.candles(counter, "uid", "CANDLE_INTERVAL_HOUR", 1, name="CB", refresh=True)
        self.assertEqual(Counting.hits, 1)
        summary = marketdata.candles(counter, "uid", "CANDLE_INTERVAL_HOUR", 1, name="CB")
        self.assertEqual(Counting.hits, 1)
        self.assertTrue(summary["cached"])

    def test_refresh_обходит_кеш(self):
        class Counting(FakeBroker):
            hits = 0

            def candles(self, *args, **kwargs):
                Counting.hits += 1
                return flat_bars(10)

        broker = Counting()
        marketdata.candles(broker, "uid", "CANDLE_INTERVAL_HOUR", 1, name="RF", refresh=True)
        marketdata.candles(broker, "uid", "CANDLE_INTERVAL_HOUR", 1, name="RF", refresh=True)
        self.assertEqual(Counting.hits, 2)

    def test_пустая_серия_не_ломается(self):
        broker = FakeBroker(bars=[])
        summary = marketdata.candles(
            broker, "uid", "CANDLE_INTERVAL_DAY", 5, name="EMPTY", refresh=True
        )
        self.assertEqual(summary["bars"], 0)

    def test_неизвестный_интервал_отклоняется(self):
        with self.assertRaises(ValueError):
            marketdata.candles(FakeBroker(), "uid", "CANDLE_INTERVAL_3_SEC", 1)

    def test_хвост_ограничен(self):
        broker = FakeBroker(bars=flat_bars(200))
        summary = marketdata.candles(
            broker, "uid", "CANDLE_INTERVAL_5_MIN", 1, name="TL", tail=500, refresh=True
        )
        self.assertLessEqual(len(summary["tail"]), 50)


class Indicators(unittest.TestCase):
    def test_atr_на_ровных_свечах(self):
        self.assertAlmostEqual(marketdata.atr(flat_bars(60, spread=0.4)), 0.4, places=6)

    def test_vwap_на_ровных_свечах(self):
        self.assertAlmostEqual(marketdata.vwap(flat_bars(10, price=50.0)), 50.0, places=6)

    def test_пустые_входы(self):
        self.assertEqual(marketdata.atr([]), 0.0)
        self.assertEqual(marketdata.vwap([]), 0.0)
        self.assertIsNone(marketdata.opening_range([]))

    def test_диапазон_открытия(self):
        window = marketdata.opening_range(flat_bars(40, spread=1.0))
        self.assertEqual(window["window"], "10:00-10:30")
        self.assertEqual(window["bars"], 2)  # 15-минутки: 10:00 и 10:15


class Spill(unittest.TestCase):
    def test_большая_таблица_уезжает_в_csv(self):
        rows = [{"a": i, "b": "x" * 60} for i in range(400)]
        text = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        stub = marketdata.spill("operations", rows, text)
        self.assertTrue(stub["spilled"])
        self.assertEqual(stub["rows"], 400)
        self.assertTrue(stub["file"].endswith(".csv"))

    def test_неоднородное_уезжает_в_json(self):
        payload = {"text": "y" * 20000}
        text = json.dumps(payload, ensure_ascii=False)
        stub = marketdata.spill("web_search", payload, text)
        self.assertTrue(stub["file"].endswith(".json"))

    def test_рендер_режет_только_большое(self):
        small = server._render("quote", {"price": 100})
        self.assertNotIn("spilled", small)
        big = server._render("operations", [{"a": i, "b": "x" * 60} for i in range(400)])
        self.assertIn("spilled", big)
        self.assertLess(len(big), config.MAX_TOOL_RESULT_BYTES)

    def test_рендер_без_отступов(self):
        self.assertEqual(server._render("t", {"a": 1, "b": 2}), '{"a":1,"b":2}')


class ReportPath(JournalCase):
    def test_только_из_каталога_отчётов(self):
        good = config.REPORTS_DIR / "2026-08-24.md"
        good.parent.mkdir(parents=True, exist_ok=True)
        good.write_text("отчёт")
        self.assertEqual(server._report_path(str(good)), good.resolve())
        self.assertEqual(server._report_path("2026-08-24.md"), good.resolve())

    def test_выход_за_каталог_отклоняется(self):
        with self.assertRaises(ValueError):
            server._report_path("/etc/passwd")
        with self.assertRaises(ValueError):
            server._report_path("../../../.agent.env")

    def test_симлинк_наружу_отклоняется(self):
        secret = config.WORKDIR / "secret.md"
        secret.write_text("токен")
        link = config.REPORTS_DIR / "link.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(secret, link)
        with self.assertRaises(ValueError):
            server._report_path(str(link))

    def test_не_markdown_отклоняется(self):
        other = config.REPORTS_DIR / "data.csv"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("a,b")
        with self.assertRaises(ValueError):
            server._report_path(str(other))

    def test_отсутствующий_файл(self):
        with self.assertRaises(ValueError):
            server._report_path("нет-такого.md")


if __name__ == "__main__":
    unittest.main()
