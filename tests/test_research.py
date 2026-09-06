"""Измерительный контур: то, что должно ломаться, если бэктест врёт.

Проверяется не «считает ли», а «не подглядывает ли»: заглядывание вперёд,
двойной счёт издержек, удобный порядок событий внутри бара и объявление
победителем случайного шума — четыре способа нарисовать прибыль, которой
не было.
"""

import unittest
from datetime import datetime, timedelta, timezone

from research import trials
from research.bars import Bar, load_csv
from research.evaluate import Protocol, block_bootstrap, evaluate, passive, split
from research.replay import Costs, Rules, run
from research.signals import generate
from research.spec import Spec, grid
from research.synthetic import random_walk

START = datetime(2024, 3, 4, 7, 0, tzinfo=timezone.utc)
STEP = timedelta(hours=1)


def series(closes, symbol="TEST", volume=10_000, highs=None, lows=None, opens=None):
    bars = []
    for i, close in enumerate(closes):
        moment = START + STEP * i
        open_price = opens[i] if opens else close
        bars.append(
            Bar(
                symbol, moment, moment + STEP,
                open_price,
                highs[i] if highs else max(open_price, close),
                lows[i] if lows else min(open_price, close),
                close, volume, True,
            )
        )
    return bars


def rising(count=80, base=100.0, step=0.5):
    return [base + step * i for i in range(count)]


class NoLookAhead(unittest.TestCase):
    def test_незакрытый_бар_не_попадает_в_историю(self):
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "x.csv"
        path.write_text(
            "time,open,high,low,close,volume,complete\n"
            "2024-03-04T07:00:00Z,100,101,99,100,10,True\n"
            "2024-03-04T08:00:00Z,100,200,99,200,10,False\n",
            encoding="utf-8",
        )
        bars = load_csv(path, "TEST", 60)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 100.0)

    def test_битый_бар_отбрасывается(self):
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "y.csv"
        path.write_text(
            "time,open,high,low,close,volume,complete\n"
            "2024-03-04T07:00:00Z,100,99,101,100,10,True\n",  # максимум ниже минимума
            encoding="utf-8",
        )
        self.assertEqual(load_csv(path, "TEST", 60), [])

    def test_вход_не_раньше_следующего_бара(self):
        """Сигнал считается на закрытии, исполняется по открытию следующего.

        Исполнение по цене того же бара — самый дешёвый способ получить
        прибыль в бэктесте и не получить её на бирже.
        """
        closes = rising(80)
        bars = series(closes)
        outcome = run([Spec("breakout", volume_multiple=0.0)], bars)
        self.assertTrue(outcome.trades)
        first = outcome.trades[0]
        signal_bars = [b for b in bars if b.start < first.entered_at]
        self.assertTrue(signal_bars)
        # Цена входа берётся из открытия своего бара, а не из закрытия сигнального.
        entry_bar = next(b for b in bars if b.start == first.entered_at)
        self.assertAlmostEqual(first.entry, entry_bar.open * (1 + Costs().per_side), 6)

    def test_сигнал_видит_только_прошлое(self):
        bars = series(rising(80))
        spec = Spec("breakout", volume_multiple=0.0)
        # Обрезаем историю на 60-м баре: сигнал не должен отличаться от
        # посчитанного по полному ряду в тот же момент.
        short = generate(spec, bars[:60])
        full = generate(spec, bars[:60])
        self.assertEqual(short, full)


class Execution(unittest.TestCase):
    def position(self, bars, **kwargs):
        spec = Spec("breakout", volume_multiple=0.0, **kwargs)
        return run([spec], bars)

    def test_издержки_не_вычитаются_дважды(self):
        bars = series(rising(80))
        outcome = self.position(bars)
        trade = outcome.trades[0]
        # net считается по ценам исполнения, в которых издержки уже сидят.
        self.assertAlmostEqual(trade.net, (trade.exit - trade.entry) * trade.shares, 6)
        self.assertAlmostEqual(trade.gross, trade.net + trade.cost, 6)
        self.assertGreater(trade.cost, 0)

    def test_разрыв_через_стоп_исполняется_по_открытию(self):
        """Цены стопа в момент разрыва не существовало."""
        closes = rising(70) + [60.0, 60.0, 60.0]
        opens = rising(70) + [60.0, 60.0, 60.0]
        bars = series(closes, opens=opens)
        outcome = self.position(bars)
        gapped = [t for t in outcome.trades if t.reason == "разрыв через стоп"]
        self.assertTrue(gapped)
        trade = gapped[0]
        self.assertLess(trade.exit, trade.stop)

    def test_стоп_и_цель_в_одном_баре_считаются_стопом(self):
        closes = rising(70)
        highs = [c + 0.2 for c in closes]
        lows = [c - 0.2 for c in closes]
        # Бар, который задевает и то и другое: широкий диапазон.
        closes.append(closes[-1])
        highs.append(closes[-1] + 40)
        lows.append(closes[-1] - 40)
        bars = series(closes, highs=highs, lows=lows,
                      opens=[c for c in closes])
        outcome = self.position(bars)
        stopped = [t for t in outcome.trades if t.reason == "стоп"]
        self.assertTrue(stopped, "порядок внутри бара неизвестен — считаем худший")

    def test_размер_считается_от_стопа(self):
        bars = series(rising(80))
        rules = Rules(capital=100_000.0, risk_unit_fraction=0.01)
        outcome = run([Spec("breakout", volume_multiple=0.0)], bars, Costs(), rules)
        trade = outcome.trades[0]
        risk = (trade.entry - trade.stop) * trade.shares
        self.assertLessEqual(risk, 1_000.0 + trade.entry)

    def test_объём_бара_ограничивает_размер(self):
        bars = series(rising(80), volume=100)
        rules = Rules(volume_share=0.02)
        outcome = run([Spec("breakout", volume_multiple=0.0)], bars, Costs(), rules)
        for trade in outcome.trades:
            self.assertLessEqual(trade.shares, 2)

    def test_открытая_в_конце_не_считается_закрытой(self):
        # Цель недостижима, стоп не задет, срок не истёк: позиция доживает
        # до конца периода и получает оценку, а не выдуманную сделку.
        bars = series(rising(80))
        outcome = self.position(bars, max_hold_bars=10_000, target_atr=100.0)
        self.assertTrue(any(t.open_at_end for t in outcome.trades))
        self.assertLess(outcome.closed, len(outcome.trades))


class PortfolioRules(unittest.TestCase):
    def bars(self):
        out = []
        for i in range(3):
            out.extend(series(rising(80), symbol=f"S{i}"))
        return out

    def test_больше_лимита_позиций_не_открывается(self):
        rules = Rules(max_positions=1, max_entries_per_day=99)
        outcome = run([Spec("breakout", volume_multiple=0.0)], self.bars(), Costs(), rules)
        moments = {}
        for trade in outcome.trades:
            moments.setdefault(trade.entered_at, []).append(trade)
        for group in moments.values():
            self.assertEqual(len(group), 1)

    def test_долив_в_ту_же_бумагу_не_проходит(self):
        outcome = run([Spec("breakout", volume_multiple=0.0)], series(rising(200)))
        opened = []
        for trade in sorted(outcome.trades, key=lambda t: t.entered_at):
            self.assertFalse(
                any(o.exited_at is None or o.exited_at > trade.entered_at
                    for o in opened if o.symbol == trade.symbol),
                "вторая позиция в той же бумаге поверх первой",
            )
            opened.append(trade)

    def test_лимит_входов_за_день(self):
        rules = Rules(max_entries_per_day=1, max_positions=5)
        outcome = run([Spec("breakout", volume_multiple=0.0)], self.bars(), Costs(), rules)
        from research.bars import MSK

        days = {}
        for trade in outcome.trades:
            day = trade.entered_at.astimezone(MSK).date()
            days[day] = days.get(day, 0) + 1
        self.assertTrue(all(count <= 1 for count in days.values()), days)


class Statistics(unittest.TestCase):
    def test_бутстрап_расширяется_с_числом_попыток(self):
        daily = [10.0, -5.0, 8.0, -2.0, 12.0, 3.0, -7.0, 9.0] * 10
        one = block_bootstrap(daily, 1, Protocol())
        many = block_bootstrap(daily, 40, Protocol(bootstrap_samples=40_000))
        self.assertLess(many["lower_mean"], one["lower_mean"])

    def test_неразрешимый_хвост_честно_помечается(self):
        daily = [1.0] * 40
        interval = block_bootstrap(daily, 100, Protocol(bootstrap_samples=1000))
        self.assertFalse(interval["resolved"])
        self.assertIn("хвост", interval["why"])

    def test_мало_дней_не_даёт_интервала(self):
        interval = block_bootstrap([1.0, 2.0], 1, Protocol())
        self.assertFalse(interval["resolved"])
        self.assertIsNone(interval["lower_mean"])

    def test_попытки_обязательны(self):
        with self.assertRaises(ValueError):
            block_bootstrap([1.0] * 40, 0, Protocol())


class WalkForward(unittest.TestCase):
    def test_проверочные_отрезки_не_пересекаются(self):
        windows = split(900, Protocol(folds=3, embargo_bars=24))
        self.assertEqual(len(windows), 3)
        previous_end = -1
        for (train_start, train_end), (test_start, test_end) in windows:
            self.assertGreaterEqual(test_start, train_end + 24)
            self.assertGreater(test_start, previous_end)
            previous_end = test_end

    def test_короткая_история_не_делится(self):
        self.assertEqual(split(30, Protocol()), [])

    def test_шум_не_проходит_ворота(self):
        """Отрицательный контроль: на случайном блуждании ответ — «нет»."""
        bars = random_walk(symbols=4, bars=3000, seed=11)
        report = evaluate(
            grid("breakout", lookback=[20], stop_atr=[1.5, 2.0]),
            bars, trials=2,
            protocol=Protocol(min_oos_sessions=10, min_closed_trades=5),
        )
        self.assertFalse(report["eligible"])
        self.assertTrue(report["failed_gates"])
        # Даже при зелёных воротах торговля не разрешается автоматически.
        self.assertFalse(report["trading_allowed"])

    def test_пассивная_покупка_считается(self):
        bars = series(rising(80))
        reference = passive(bars, Rules(), Costs())
        self.assertGreater(reference.net, 0)


class Registry(unittest.TestCase):
    def setUp(self):
        conn = trials.connect()
        conn.execute("DELETE FROM trials")
        conn.execute("DELETE FROM campaigns")
        conn.commit()
        conn.close()

    def test_незавершённая_попытка_считается(self):
        """Брошенная попытка — это тоже попытка, и знаменатель у неё общий."""
        trials.open_campaign("c", "вопрос", ["A"], "период", {})
        trials.register("c", "v1", {"family": "breakout"})
        trials.register("c", "v2", {"family": "pullback"})
        trials.finish("c", "v1", {"net": 1.0})
        self.assertEqual(trials.count("c"), 2)
        rows = trials.trials("c")
        self.assertEqual([r["finished"] for r in rows], [True, False])

    def test_повтор_не_плодит_записей(self):
        trials.open_campaign("c", "вопрос", ["A"], "период", {})
        trials.register("c", "v1", {"family": "breakout"})
        trials.register("c", "v1", {"family": "breakout"})
        self.assertEqual(trials.count("c"), 1)

    def test_кампания_закрывается_решением(self):
        trials.open_campaign("c", "вопрос", ["A"], "период", {})
        trials.close_campaign("c", "гипотеза-закрыта")
        self.assertEqual(trials.campaign("c")["verdict"], "гипотеза-закрыта")


class Specs(unittest.TestCase):
    def test_версия_зависит_от_параметров(self):
        self.assertNotEqual(Spec("breakout").version, Spec("breakout", lookback=21).version)
        self.assertEqual(Spec("breakout").version, Spec("breakout").version)

    def test_заметка_версию_не_меняет(self):
        self.assertEqual(
            Spec("breakout", note="первая").version, Spec("breakout", note="вторая").version
        )

    def test_бессмысленные_параметры_отбиваются(self):
        for kwargs in (
            {"family": "нет-такого"},
            {"family": "breakout", "stop_atr": 0.0},
            {"family": "breakout", "risk_fraction": 0.5},
            {"family": "breakout", "lookback": 2},
            {"family": "breakout", "target_atr": float("inf")},
        ):
            with self.assertRaises(ValueError, msg=kwargs):
                Spec(**kwargs)


if __name__ == "__main__":
    unittest.main()


class GatewayBridge(unittest.TestCase):
    """Связь контуров: измеряет код, торгует шлюз по результату измерения."""

    def setUp(self):
        from tests.support import JournalCase

        JournalCase.setUp(self)
        conn = trials.connect()
        conn.execute("DELETE FROM trials")
        conn.execute("DELETE FROM campaigns")
        conn.commit()
        conn.close()

    def broker(self):
        from gateway import marketdata, server
        from tests.support import FakeBroker

        # Ряд заканчивается «сейчас»: инструмент качает историю окнами от
        # текущего момента назад, и данные из 2024 года в них не попадут.
        from datetime import datetime, timedelta, timezone

        generated = random_walk(symbols=1, bars=900, seed=5)
        shift = datetime.now(timezone.utc) - timedelta(hours=1) - generated[-1].start
        bars = []
        for bar in generated:
            bars.append({
                "time": (bar.start + shift).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "open": bar.open, "high": bar.high, "low": bar.low,
                "close": bar.close, "volume": bar.volume, "complete": True,
            })
        fake = FakeBroker(bars=bars)
        server._client = fake
        marketdata.remember_instrument(
            {"ticker": "SYN1", "instrument_id": "uid", "figi": "F", "lot": 1}
        )
        return fake

    def test_прогон_регистрирует_попытку_и_сетап(self):
        from gateway import journal as gj
        from gateway import server

        self.broker()
        report = server.tool_backtest(
            tickers=["SYN1"], family="breakout", days=40, register_as="проба"
        )
        self.assertEqual(report["registered_trials"], 1)
        self.assertFalse(report["trading_allowed"])
        record = gj.playbook("проба")
        self.assertEqual(record["source"], "evaluator")
        # Числа в реестре — те, что посчитал код, а не те, что назвал агент.
        self.assertEqual(record["trades"], report["oos"]["trades"])

    def test_каждый_вариант_ужесточает_порог(self):
        """Перебор не бесплатен: знаменатель растёт с числом попыток."""
        from gateway import server

        self.broker()
        first = server.tool_backtest(tickers=["SYN1"], family="breakout", days=40)
        second = server.tool_backtest(
            tickers=["SYN1"], family="breakout", days=40, stop_atr=2.0
        )
        self.assertEqual(first["registered_trials"], 1)
        self.assertEqual(second["registered_trials"], 2)

    def test_неизвестный_тикер_отбивается(self):
        from gateway import server

        self.broker()
        with self.assertRaises(ValueError) as caught:
            server.tool_backtest(tickers=["НЕТТАКОЙ"], family="breakout")
        self.assertIn("справочнике", str(caught.exception))


class Legibility(unittest.TestCase):
    """Ответ «ничего не нашлось» должен объяснять, что именно не нашлось."""

    def test_ноль_сделок_объясняется(self):
        from research.bars import Bar
        from datetime import datetime, timedelta, timezone

        # Ровный ряд: условие входа не выполняется никогда.
        begin = datetime(2024, 1, 3, 7, 0, tzinfo=timezone.utc)
        bars = [
            Bar("FLAT", begin + timedelta(hours=i), begin + timedelta(hours=i + 1),
                100.0, 100.1, 99.9, 100.0, 1000, True)
            for i in range(900)
        ]
        report = evaluate(grid("breakout", lookback=[20]), bars, trials=1,
                          protocol=Protocol(min_oos_sessions=5, min_closed_trades=1))
        self.assertEqual(report["closed_trades"], 0)
        self.assertIn("отсутствие сделок", report["why"])
