"""Ограничители: то, что должно останавливать сделки до рынка."""

import unittest

from gateway import config, guards, journal
from tests.support import JournalCase, add_order, add_playbook, msk


class TradeCard(JournalCase):
    """Три барьера входа на числах разобранных сделок."""

    def test_ozon_отклоняется_по_соотношению(self):
        # Реальная сделка 24.08: вход 2430.5, стоп 2330, цель 2500 → R:R 0.69.
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_trade_card(2430.5, 2330.0, 2500.0, 1, 1, "OZON")
        self.assertIn("прибыль/риск", str(caught.exception))

    def test_trnfp_отклоняется(self):
        # Реальная сделка 24.08: вход 1027.8, стоп 1022, цель 1033 → R:R 0.9.
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_trade_card(1027.8, 1022.0, 1033.0, 1, 1, "TRNFP")
        self.assertIn("прибыль/риск", str(caught.exception))

    def test_скальпинг_отклоняется_по_издержкам(self):
        # Соотношение хорошее (3.0), но цель 0.9% при круговых издержках 0.1%:
        # комиссия съедает больше десятой части движения.
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_trade_card(100.0, 99.7, 100.9, 1, 1, "TEST")
        self.assertIn("издержк", str(caught.exception))

    def test_стоп_внутри_шума_отклоняется(self):
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_trade_card(100.0, 99.0, 110.0, 1, 1, "TEST", atr=2.0)
        self.assertIn("ATR", str(caught.exception))

    def test_размер_считается_от_стопа(self):
        # Риск 5 ₽ на акцию, лот 10 → 30 лотов это 1500 ₽ при потолке 1000 ₽.
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_trade_card(100.0, 95.0, 120.0, 30, 10, "TEST")
        self.assertIn("20 лот", str(caught.exception))

    def test_хорошая_сделка_проходит(self):
        checks = guards.check_trade_card(100.0, 97.0, 110.0, 3, 1, "TEST", atr=1.0)
        self.assertEqual(checks["reward_risk"], 3.33)
        self.assertEqual(checks["risk_rub"], 9.0)
        self.assertEqual(checks["stop_atr"], 3.0)

    def test_стоп_выше_входа_отклоняется(self):
        with self.assertRaises(guards.GuardRejection):
            guards.check_trade_card(100.0, 101.0, 120.0, 1, 1, "TEST")

    def test_цель_ниже_входа_отклоняется(self):
        with self.assertRaises(guards.GuardRejection):
            guards.check_trade_card(100.0, 95.0, 99.0, 1, 1, "TEST")


class CardValidation(JournalCase):
    """Карточка — это измерение. Числа в ней обязаны быть числами."""

    def test_бесконечная_цель_отбивается(self):
        # Бесконечность проходит любое сравнение «больше»: отношение
        # прибыли к риску бесконечно, доля цели бесконечна, и все три
        # барьера отвечают «да» на бессмысленный вход.
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_trade_card(100.0, 97.0, float("inf"), 1, 1, "TEST")
        self.assertIn("конечным", str(caught.exception))

    def test_nan_отбивается(self):
        with self.assertRaises(guards.GuardRejection):
            guards.check_trade_card(100.0, float("nan"), 110.0, 1, 1, "TEST")

    def test_бесконечный_стоп_отбивается(self):
        with self.assertRaises(guards.GuardRejection):
            guards.check_trade_card(100.0, float("-inf"), 110.0, 1, 1, "TEST")

    def test_дробные_лоты_отбиваются(self):
        with self.assertRaises(guards.GuardRejection):
            guards.check_trade_card(100.0, 97.0, 110.0, 1.5, 1, "TEST")


class Playbooks(JournalCase):
    """Сетап — запись с числами, сделанная до сделки, а не строка в заявке."""

    def test_незарегистрированный_отбивается(self):
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_playbook("выглядит-перспективно", 100.0)
        self.assertIn("не зарегистрирован", str(caught.exception))

    def test_проверенный_торгуется_полным_размером(self):
        add_playbook(trades=40, wins=24, avg_r=0.4)
        record = guards.check_playbook("test", config.MAX_RISK_PER_TRADE)
        self.assertEqual(record["status"], "working")

    def test_новый_сетап_только_четвертью(self):
        add_playbook(trades=3, wins=2, avg_r=0.5)
        limit = config.MAX_RISK_PER_TRADE * config.PROBATION_RISK_FRACTION
        guards.check_playbook("test", limit)
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_playbook("test", limit + 1)
        self.assertIn("на проверке", str(caught.exception))

    def test_свои_слова_не_дают_полного_размера(self):
        """Числа, записанные агентом, остаются на проверке навсегда.

        Реестр хранит и то, откуда взялась статистика. Запись со слов не
        становится измерением от того, что цифры выглядят убедительно.
        """
        add_playbook(trades=200, wins=140, avg_r=0.9, source="manual")
        record = journal.playbook("test")
        self.assertEqual(record["status"], "probation")
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_playbook("test", config.MAX_RISK_PER_TRADE)
        self.assertIn("с твоих слов", str(caught.exception))

    def test_неизвестный_источник_отбивается(self):
        with self.assertRaises(ValueError):
            journal.register_playbook(
                {"name": "x", "entry": "a", "invalidation": "b", "measured_on": "c",
                 "trades": 1, "wins": 1, "avg_r": 0.1, "source": "внушает-доверие"}
            )

    def test_отрицательная_база_не_даёт_полного_размера(self):
        # Сделок хватает, но средний результат отрицательный.
        add_playbook(trades=40, wins=10, avg_r=-0.3)
        with self.assertRaises(guards.GuardRejection):
            guards.check_playbook("test", config.MAX_RISK_PER_TRADE)

    def test_снятый_сетап_не_торгуется(self):
        add_playbook()
        journal.retire_playbook("test", "проверка на истории дала отрицательную базу")
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_playbook("test", 1.0)
        self.assertIn("снят с торговли", str(caught.exception))

    def test_статистика_обновляется_без_потери_даты(self):
        first = add_playbook(trades=3, wins=1, avg_r=-0.1)
        self.assertEqual(first["status"], "probation")
        second = add_playbook(trades=20, wins=12, avg_r=0.5)
        self.assertEqual(second["status"], "working")
        self.assertEqual(len(journal.playbooks()), 1)


class PortfolioHeat(JournalCase):
    def test_две_идеи_складываются(self):
        # Каждая по отдельности меньше 1R, вместе — больше потолка портфеля.
        guards.check_portfolio_heat(900.0, 900.0, "TEST")
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_portfolio_heat(900.0, 1_200.0, "TEST")
        self.assertIn("под риском", str(caught.exception))

    def test_первый_вход_проходит(self):
        guards.check_portfolio_heat(config.MAX_RISK_PER_TRADE, 0.0, "TEST")


class HaltSemantics(JournalCase):
    """Остановка закрывает вход, но не выход."""

    def test_остановка_запрещает_вход(self):
        guards.halt("тест")
        with self.assertRaises(guards.GuardRejection):
            guards.check_entry_allowed()

    def test_порог_капитала_ставит_остановку(self):
        with self.assertRaises(guards.GuardRejection):
            guards.check_capital_floor(config.CAPITAL_FLOOR - 1)
        self.assertTrue(guards.halted())

    def test_порог_не_трогает_продажу(self):
        # Ниже порога продавать можно: выход ограничителями не режется.
        with self.assertRaises(guards.GuardRejection):
            guards.check_capital_floor(1000.0)
        guards.check_sell(1, {"sell_max_lots": 5}, "TEST")


class Limits(JournalCase):
    def test_рыночная_заявка_режется_рыночным_лимитом(self):
        limits = {"buy_max_lots": 10, "buy_max_market_lots": 4, "buy_money": 1000.0}
        guards.check_buy(4, limits, "TEST", market=True)
        with self.assertRaises(guards.GuardRejection):
            guards.check_buy(5, limits, "TEST", market=True)
        # Лимитной те же пять лотов доступны.
        guards.check_buy(5, limits, "TEST", market=False)

    def test_шорт_запрещён(self):
        with self.assertRaises(guards.GuardRejection):
            guards.check_sell(10, {"sell_max_lots": 3}, "TEST")

    def test_дневной_стоп(self):
        journal.log_snapshot(100_000.0, 100_000.0, [])
        guards.check_daily_loss(99_000.0)
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_daily_loss(100_000.0 - config.DAILY_LOSS_LIMIT - 1)
        self.assertIn("дневного стопа", str(caught.exception))

    def test_дневной_стоп_молчит_без_опоры(self):
        guards.check_daily_loss(1.0)

    def test_лимит_входов_за_день(self):
        for i in range(config.MAX_ENTRIES_PER_DAY):
            add_order(ts=msk(11) + i, direction="buy", lots_executed=1)
        with self.assertRaises(guards.GuardRejection):
            guards.check_entries_today()

    def test_лимит_позиций(self):
        portfolio = {"positions": [
            {"instrument_id": f"uid{i}", "quantity": 1} for i in range(config.MAX_POSITIONS)
        ]}
        with self.assertRaises(guards.GuardRejection):
            guards.check_positions(portfolio, "новая")
        # Долив в уже открытую позицию лимитом не режется.
        guards.check_positions(portfolio, "uid0")


class Cooldown(JournalCase):
    def test_возврат_после_убытка_блокируется(self):
        import time
        now = time.time()
        add_order(ts=now - 600, instrument_id="uid", direction="buy", price=100.0)
        add_order(ts=now - 300, instrument_id="uid", direction="sell", price=98.0)
        with self.assertRaises(guards.GuardRejection) as caught:
            guards.check_cooldown("uid", "TEST")
        self.assertIn("отыгрывание", str(caught.exception))

    def test_прибыльный_выход_не_блокирует(self):
        import time
        now = time.time()
        add_order(ts=now - 600, instrument_id="uid", direction="buy", price=100.0)
        add_order(ts=now - 300, instrument_id="uid", direction="sell", price=104.0)
        guards.check_cooldown("uid", "TEST")

    def test_после_паузы_вход_открыт(self):
        import time
        now = time.time() - config.LOSS_COOLDOWN - 60
        add_order(ts=now - 60, instrument_id="uid", direction="buy", price=100.0)
        add_order(ts=now, instrument_id="uid", direction="sell", price=98.0)
        guards.check_cooldown("uid", "TEST")


if __name__ == "__main__":
    unittest.main()
