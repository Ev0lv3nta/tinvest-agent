"""Панель: доступ закрыт по умолчанию, initData проверяется целиком."""

import hashlib
import hmac
import json
import time
import unittest
import urllib.parse

from gateway import journal
from tests.support import JournalCase

TOKEN = "123456:TESTTOKEN"


def sign(fields: dict) -> str:
    pairs = sorted(fields.items())
    check = "\n".join(f"{k}={v}" for k, v in pairs)
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": digest})


class Auth(JournalCase):
    def setUp(self):
        super().setUp()
        from webapp import server as webapp

        self.webapp = webapp
        webapp.config._ENV["TELEGRAM_BOT_TOKEN"] = TOKEN
        webapp.ACCESS_KEY = "secret"
        webapp.OPERATOR_ID = "777"
        self.handler = webapp.Handler.__new__(webapp.Handler)
        self.handler.headers = {}

    def тело(self, init_data: str = "") -> dict:
        self.handler.headers = {"X-Telegram-Init-Data": init_data} if init_data else {}
        return {}

    def test_без_ключа_доступ_закрыт(self):
        self.webapp.ACCESS_KEY = ""
        self.tearDownKey = True
        self.assertFalse(self.handler._authorized({}))

    def test_ключ_в_заголовке_работает(self):
        self.handler.headers = {"X-Panel-Key": "secret"}
        self.assertTrue(self.handler._authorized({}))

    def test_чужой_ключ_отклоняется(self):
        self.assertFalse(self.handler._authorized({"k": ["другой"]}))

    def test_подпись_проверяется(self):
        good = sign({"auth_date": str(int(time.time())), "user": json.dumps({"id": 777})})
        self.assertTrue(self.webapp.valid_telegram(good))
        self.assertFalse(self.webapp.valid_telegram(good.replace("hash=", "hash=0")))

    def test_старый_пакет_отклоняется(self):
        stale = sign(
            {
                "auth_date": str(int(time.time()) - self.webapp.INIT_DATA_MAX_AGE - 60),
                "user": json.dumps({"id": 777}),
            }
        )
        self.assertFalse(self.webapp.valid_telegram(stale))

    def test_чужой_пользователь_отклоняется(self):
        other = sign({"auth_date": str(int(time.time())), "user": json.dumps({"id": 42})})
        self.assertFalse(self.webapp.valid_telegram(other))

    def test_без_auth_date_отклоняется(self):
        broken = sign({"user": json.dumps({"id": 777})})
        self.assertFalse(self.webapp.valid_telegram(broken))


class Bounds(JournalCase):
    def setUp(self):
        super().setUp()
        from webapp import server as webapp

        self.webapp = webapp
        self.handler = webapp.Handler.__new__(webapp.Handler)

    def test_отрицательный_лимит_не_отдаёт_всё(self):
        for i in range(5):
            journal.log_transcript("agentMessage", body=f"строка {i}")
        rows = self.handler.api_transcript({"limit": ["-1"]})["items"]
        self.assertEqual(len(rows), 1)

    def test_лимит_сверху_ограничен(self):
        for i in range(3):
            journal.log_transcript("agentMessage", body=f"строка {i}")
        rows = self.handler.api_transcript({"limit": ["100000"]})["items"]
        self.assertEqual(len(rows), 3)


class State(JournalCase):
    def setUp(self):
        super().setUp()
        from webapp import server as webapp

        self.handler = webapp.Handler.__new__(webapp.Handler)

    def test_сделками_считаются_исполненные(self):
        from tests.support import add_order

        add_order(ts=time.time(), order_id="A", lots_executed=0, price=0.0)
        add_order(ts=time.time(), order_id="B", lots_executed=4, price=100.0)
        state = self.handler.api_state()
        self.assertEqual(state["trades"], 1)
        self.assertEqual(state["submissions"], 2)


if __name__ == "__main__":
    unittest.main()
