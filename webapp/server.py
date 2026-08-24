"""HTTP-сервер панели наблюдения.

Только чтение: ничего не меняет, отдаёт срез журнала. Работает на
стандартной библиотеке, чтобы не тащить зависимости на машину агента.

Доступ: подпись Telegram Mini App либо ключ в адресе для браузера.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gateway import config

MSK = ZoneInfo("Europe/Moscow")
STATIC = Path(__file__).resolve().parent
PORT = 8080

# Ключ для входа из браузера. Подпись Telegram проверяется отдельно.
ACCESS_KEY = config.secret("WEBAPP_KEY", "")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def valid_telegram(init_data: str) -> bool:
    """Проверка подписи initData по алгоритму Telegram."""
    token = config.secret("TELEGRAM_BOT_TOKEN")
    if not token or not init_data:
        return False
    try:
        pairs = urllib.parse.parse_qsl(init_data, strict_parsing=True)
    except ValueError:
        return False
    received = dict(pairs).get("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs) if k != "hash")
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tinvest-panel"

    def log_message(self, *args) -> None:
        pass

    # --- вспомогательное ---------------------------------------------------

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(
            json.dumps(payload, ensure_ascii=False, default=str).encode(),
            "application/json; charset=utf-8",
            status,
        )

    def _authorized(self, query: dict) -> bool:
        if not ACCESS_KEY:
            return True
        if query.get("k", [""])[0] == ACCESS_KEY:
            return True
        return valid_telegram(self.headers.get("X-Telegram-Init-Data", ""))

    # --- данные ------------------------------------------------------------

    def api_state(self) -> dict:
        conn = db()

        def one(query, *args):
            row = conn.execute(query, args).fetchone()
            return row

        snapshot = one("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1")
        state = one("SELECT value FROM kv WHERE key='agent_state'")
        halted = one("SELECT value FROM kv WHERE key='halted'")
        wake = one(
            "SELECT * FROM wakeups WHERE fired_ts IS NULL AND cancelled=0"
            " ORDER BY due_ts LIMIT 1"
        )
        peak = one("SELECT MAX(total) AS m FROM snapshots")
        trades = one("SELECT COUNT(*) AS n FROM orders")
        last_call = one("SELECT tool, ts FROM tool_calls ORDER BY id DESC LIMIT 1")

        total = snapshot["total"] if snapshot else config.STARTING_CAPITAL
        peak_value = (peak["m"] if peak and peak["m"] else total) or total
        return {
            "total": total,
            "cash": snapshot["cash"] if snapshot else config.STARTING_CAPITAL,
            "start": config.STARTING_CAPITAL,
            "floor": config.CAPITAL_FLOOR,
            "delta": total - config.STARTING_CAPITAL,
            "delta_pct": (total / config.STARTING_CAPITAL - 1) * 100,
            "drawdown_pct": (total / peak_value - 1) * 100 if peak_value else 0.0,
            "positions": json.loads(snapshot["positions"]) if snapshot else [],
            "state": state["value"] if state else "неизвестно",
            "halted": halted["value"] if halted and halted["value"] else "",
            "trades": trades["n"] if trades else 0,
            "last_tool": last_call["tool"] if last_call else "",
            "last_tool_ts": last_call["ts"] if last_call else 0,
            "wake_at": wake["due_ts"] if wake else 0,
            "wake_reason": wake["reason"] if wake else "",
            "now": time.time(),
        }

    def api_transcript(self, query: dict) -> dict:
        conn = db()
        limit = min(int(query.get("limit", ["120"])[0]), 400)
        after = query.get("after", [""])[0]
        before = query.get("before", [""])[0]

        if after:
            rows = conn.execute(
                "SELECT * FROM transcript WHERE id > ? ORDER BY id LIMIT ?",
                (int(after), limit),
            ).fetchall()
        elif before:
            rows = conn.execute(
                "SELECT * FROM transcript WHERE id < ? ORDER BY id DESC LIMIT ?",
                (int(before), limit),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute(
                "SELECT * FROM (SELECT * FROM transcript ORDER BY id DESC LIMIT ?)"
                " ORDER BY id",
                (limit,),
            ).fetchall()

        oldest = conn.execute("SELECT MIN(id) AS m FROM transcript").fetchone()["m"] or 0
        items = [
            {
                "id": r["id"],
                "ts": r["ts"],
                "kind": r["kind"],
                "title": r["title"] or "",
                "body": r["body"] or "",
                "own": bool(r["is_own"]),
            }
            for r in rows
        ]
        return {"items": items, "oldest": oldest}

    def api_live(self) -> dict:
        conn = db()

        def kv(key, default=""):
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE delivered = 0"
        ).fetchone()["n"]
        return {
            "state": kv("agent_state", "неизвестно"),
            "activity": kv("live_activity"),
            "text": kv("live_text"),
            "queued": pending,
        }

    def api_trades(self) -> dict:
        conn = db()
        rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 50").fetchall()
        return {
            "trades": [
                {
                    "ts": r["ts"],
                    "ticker": r["ticker"],
                    "direction": r["direction"],
                    "lots": r["lots"],
                    "price": r["price"],
                    "total": r["total"],
                    "status": r["status"],
                    "rationale": r["rationale"] or "",
                }
                for r in rows
            ]
        }

    def api_equity(self) -> dict:
        conn = db()
        rows = conn.execute(
            "SELECT ts, total FROM snapshots ORDER BY id"
        ).fetchall()
        return {"points": [{"ts": r["ts"], "total": r["total"]} for r in rows]}

    # --- маршруты ----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path.startswith("/api/"):
            if not self._authorized(query):
                return self._json({"error": "доступ запрещён"}, 403)
            try:
                if path == "/api/state":
                    return self._json(self.api_state())
                if path == "/api/transcript":
                    return self._json(self.api_transcript(query))
                if path == "/api/trades":
                    return self._json(self.api_trades())
                if path == "/api/equity":
                    return self._json(self.api_equity())
                if path == "/api/live":
                    return self._json(self.api_live())
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": str(exc)}, 500)
            return self._json({"error": "нет такого метода"}, 404)

        if path in ("/", "/index.html"):
            body = (STATIC / "index.html").read_bytes()
            return self._send(body, "text/html; charset=utf-8")
        if path == "/healthz":
            return self._send(b"ok", "text/plain")
        self._send(b"not found", "text/plain", 404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path != "/api/say":
            return self._json({"error": "нет такого метода"}, 404)
        if not self._authorized(query):
            return self._json({"error": "доступ запрещён"}, 403)
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            text = (payload.get("text") or "").strip()
            if not text:
                return self._json({"error": "пустое сообщение"}, 400)
            # Панель только читает журнал, но отправка сообщения — исключение:
            # своя запись в очередь, которую разбирает супервизор.
            conn = sqlite3.connect(config.DB_PATH, timeout=10)
            conn.execute(
                "INSERT INTO messages (ts, source, text) VALUES (?, ?, ?)",
                (time.time(), "operator", text[:4000]),
            )
            conn.commit()
            conn.close()
            return self._json({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": str(exc)}, 500)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
