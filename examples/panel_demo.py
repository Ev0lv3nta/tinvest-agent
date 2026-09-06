#!/usr/bin/env python3
"""Панель с вымышленными данными. Брокер и модель не запускаются."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080, help="локальный порт, 0 — свободный")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="tinvest-demo-") as directory:
        root = Path(directory)
        # Изолируем пути до импорта: даже при настроенном стенде демо пишет
        # только в собственную временную базу.
        for name, leaf in (("AGENT_DB", "agent.db"), ("AGENT_DATA_DIR", "data"),
                           ("AGENT_WORKDIR", "work"), ("RESEARCH_DB", "research.db")):
            os.environ[name] = str(root / leaf)

        from gateway import journal
        from webapp import server as panel

        panel.ACCESS_KEY = secrets.token_urlsafe(24)
        panel.OPERATOR_ID = ""
        now = time.time()
        connection = journal.connect()
        for index, total in enumerate((100000, 100080, 99870, 99920, 99780, 99840)):
            connection.execute(
                "INSERT INTO snapshots (ts, total, cash, positions) VALUES (?, ?, ?, ?)",
                (now - (5 - index) * 3600, total, total, json.dumps([])),
            )
        connection.commit()
        journal.kv_set("agent_state", "демо · вымышленные данные")
        for kind, title, body in (
            ("userMessage", "", "Проверить гипотезу на истории. Заявки не отправлять."),
            ("mcpToolCall", "research demo", "Синтетические данные; фиксированный seed; проверка вне обучения."),
            ("agentMessage", "", "Преимущество не подтверждено. На проверочной выборке результат отрицательный. Оснований для входа нет."),
            ("mcpToolCall", "portfolio", "Открытых позиций нет. Ограничения риска действуют."),
            ("agentMessage", "", "Это пример ленты, а не запись реальной сессии. Во вкладке «Итог» — вымышленная кривая счёта."),
        ):
            journal.log_transcript(kind, title, body)

        class DemoHandler(panel.Handler):
            def _authorized(self) -> bool:
                # Только одноразовый ключ этого демо, без Telegram и .env.
                import hmac
                return hmac.compare_digest(
                    self.headers.get("X-Panel-Key", "").encode(),
                    panel.ACCESS_KEY.encode(),
                )

            def api_limits(self, query: dict) -> dict:
                return {"accounts": [], "failed": 0, "age": 0}

            def do_POST(self) -> None:
                self.close_connection = True
                self._json({"error": "В демо отправка сообщений отключена."}, 405)

            def do_GET(self) -> None:
                if urlparse(self.path).path == "/":
                    html = (panel.STATIC / "index.html").read_text()
                    html = html.replace(
                        '<script src="https://telegram.org/js/telegram-web-app.js"></script>', ""
                    ).replace("<title>Торговый агент</title>", "<title>tinvest-agent · демо</title>")
                    html = html.replace("<body>", '<body><div style="padding:12px;text-align:center;color:#c8a15a">ДЕМО · вымышленные данные · брокер и модель не подключены</div>')
                    html = html.replace('placeholder="Написать агенту…"', 'disabled placeholder="В демо отправка сообщений отключена"')
                    self._send(html.encode(), "text/html; charset=utf-8")
                    return
                super().do_GET()

        with ThreadingHTTPServer(("127.0.0.1", args.port), DemoHandler) as http:
            http.daemon_threads = True
            print(f"http://127.0.0.1:{http.server_port}/?k={panel.ACCESS_KEY}", flush=True)
            print("Демо на вымышленных данных. Остановить: Ctrl+C.", flush=True)
            try:
                http.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                connection.close()


if __name__ == "__main__":
    main()
