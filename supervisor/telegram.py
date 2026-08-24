"""Telegram-бот оператора.

Кнопки отвечают данными из журнала, а не через агента: состояние видно,
даже когда он занят длинным ходом или спит. Свободный текст, наоборот,
уходит агенту как сообщение.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from gateway import config, journal, limits

MSK = ZoneInfo("Europe/Moscow")
API = "https://api.telegram.org/bot{token}/{method}"

BTN_PORTFOLIO = "📊 Портфель"
BTN_RESULT = "📈 Итог"
BTN_TRADES = "📝 Сделки"
BTN_STATUS = "🧠 Статус"
BTN_REPORT = "📄 Отчёт"
BTN_LIMITS = "⚡ Лимиты"
BTN_HALT = "⏸ Стоп"

KEYBOARD = {
    "keyboard": [
        [{"text": BTN_PORTFOLIO}, {"text": BTN_RESULT}],
        [{"text": BTN_TRADES}, {"text": BTN_STATUS}],
        [{"text": BTN_REPORT}, {"text": BTN_LIMITS}],
        [{"text": BTN_HALT}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def _ago(ts: float) -> str:
    minutes = (time.time() - ts) / 60
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{int(minutes)} мин назад"
    if minutes < 24 * 60:
        return f"{int(minutes // 60)} ч назад"
    return f"{int(minutes // 1440)} дн назад"


class Bot:
    def __init__(self, on_message: Optional[Callable[[str], None]] = None):
        self.token = config.secret("TELEGRAM_BOT_TOKEN")
        self.chat_id = config.secret("TELEGRAM_CHAT_ID")
        self.on_message = on_message
        self.offset = int(journal.kv_get("tg_offset", "0") or 0)

    # --- транспорт ---------------------------------------------------------

    def _api(self, method: str, payload: dict, timeout: int = 30) -> dict:
        if not self.token:
            return {"ok": False}
        request = urllib.request.Request(
            API.format(token=self.token, method=method),
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError):
            return {"ok": False}

    def send(self, text: str, keyboard: bool = True) -> bool:
        payload = {
            "chat_id": self.chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = KEYBOARD
        return self._api("sendMessage", payload).get("ok", False)

    def set_commands(self) -> None:
        self._api(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "показать меню"},
                    {"command": "portfolio", "description": "состояние портфеля"},
                    {"command": "trades", "description": "последние сделки"},
                    {"command": "status", "description": "чем занят агент"},
                    {"command": "limits", "description": "квоты аккаунтов"},
                    {"command": "halt", "description": "остановить торговлю"},
                    {"command": "resume", "description": "снять остановку"},
                ]
            },
        )

    # --- отчёты из журнала -------------------------------------------------

    def render_portfolio(self) -> str:
        conn = journal.connect()
        row = conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return "Срезов портфеля пока нет — агент ещё не смотрел баланс."
        positions = json.loads(row["positions"])
        lines = [
            f"<b>Портфель</b> · {_ago(row['ts'])}",
            f"Всего: <b>{_money(row['total'])} ₽</b>",
            f"Свободно: {_money(row['cash'])} ₽",
        ]
        if positions:
            lines.append("")
            for position in positions:
                lines.append(
                    f"{position.get('ticker') or '?'} — {position.get('lots', 0):g} лот, "
                    f"{_money(position.get('current_price', 0) * position.get('quantity', 0))} ₽ "
                    f"({position.get('yield', 0):+.0f} ₽)"
                )
        else:
            lines.append("\nПозиций нет.")
        return "\n".join(lines)

    def render_result(self) -> str:
        conn = journal.connect()
        rows = conn.execute("SELECT ts, total FROM snapshots ORDER BY id").fetchall()
        if not rows:
            return "Данных ещё нет."
        start = config.STARTING_CAPITAL
        current = rows[-1]["total"]
        peak = max(r["total"] for r in rows)
        drawdown = (current / peak - 1) * 100 if peak else 0.0
        trades = conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
        halted = journal.kv_get("halted", "")
        lines = [
            "<b>Итог</b>",
            f"Сейчас: <b>{_money(current)} ₽</b>",
            f"К старту: {current - start:+,.0f} ₽ ({(current / start - 1) * 100:+.2f}%)".replace(",", " "),
            f"Максимум: {_money(peak)} ₽, просадка от него {drawdown:.2f}%",
            f"Порог остановки: {_money(config.CAPITAL_FLOOR)} ₽",
            f"Сделок: {trades}",
        ]
        if halted:
            lines.append(f"\n⛔ <b>Остановлен:</b> {halted}")
        return "\n".join(lines)

    def render_trades(self, limit: int = 8) -> str:
        conn = journal.connect()
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        if not rows:
            return "Сделок пока не было."
        lines = ["<b>Последние сделки</b>"]
        for row in rows:
            when = datetime.fromtimestamp(row["ts"], MSK).strftime("%d.%m %H:%M")
            side = "покупка" if row["direction"] == "buy" else "продажа"
            lines.append(
                f"\n<b>{when}</b> · {side} {row['ticker'] or '?'} × {row['lots']} лот"
                f"\n{_money(row['total'] or 0)} ₽ · {row['status'] or '?'}"
            )
            if row["rationale"]:
                lines.append(f"<i>{row['rationale'][:300]}</i>")
        return "\n".join(lines)

    def render_limits(self, force: bool = False) -> str:
        try:
            data = limits.fetch(force=force)
        except limits.LimitsError as exc:
            return f"Не удалось получить квоты: {exc}"
        if not data["accounts"]:
            return "Аккаунтов не найдено."

        lines = ["<b>Квоты аккаунтов</b>"]
        total_used = total_cap = 0
        for account in data["accounts"]:
            filled = round(account["percent"] / 10)
            bar = "█" * filled + "░" * (10 - filled)
            lines.append(
                f"\n<code>{account['id']}</code> · {account['plan']}"
                f"\n{bar} <b>{account['percent']:.0f}%</b>"
                f"\n{account['used']} из {account['total']} · осталось {account['remaining']}"
            )
            reset = limits.reset_in(account["reset_at"])
            if reset:
                lines.append(f"сброс {reset}")
            total_used += account["used"]
            total_cap += account["total"]

        if total_cap:
            lines.append(
                f"\n<b>Суммарно:</b> {total_used} из {total_cap} "
                f"({total_used / total_cap * 100:.0f}%)"
            )
        if data["age"]:
            lines.append(f"<i>данные {data['age']} с назад</i>")
        return "\n".join(lines)

    def render_status(self) -> str:
        conn = journal.connect()
        state = journal.kv_get("agent_state", "неизвестно")
        icon = "🟢" if state == "работает" else "😴"
        lines = [f"{icon} <b>Статус</b>: {state}"]

        last_call = conn.execute(
            "SELECT tool, ts FROM tool_calls ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last_call:
            when = datetime.fromtimestamp(last_call["ts"], MSK).strftime("%d.%m %H:%M:%S")
            lines.append(f"Последнее действие: <b>{last_call['tool']}</b>")
            lines.append(f"{when} ({_ago(last_call['ts'])})")

        last_end = journal.kv_get("last_turn_end", "")
        if last_end and state != "работает":
            lines.append(f"Закончил ход: {_ago(float(last_end))}")

        wake = conn.execute(
            "SELECT * FROM events WHERE kind = 'wake' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if wake:
            lines.append(f"Последнее пробуждение: {_ago(wake['ts'])}")

        lines.append("")
        pending = journal.pending_wakeup()
        if pending:
            when = datetime.fromtimestamp(pending["due_ts"], MSK).strftime("%d.%m %H:%M")
            minutes = (pending["due_ts"] - time.time()) / 60
            through = f"{minutes:.0f} мин" if minutes < 120 else f"{minutes / 60:.1f} ч"
            lines.append(f"⏰ Следующее пробуждение: <b>{when}</b> (через {through})")
            lines.append(f"<i>{pending['reason'][:400]}</i>")
        else:
            lines.append("⏰ Будильник не назначен — разбужу сам по расписанию.")

        calls_day = conn.execute(
            "SELECT COUNT(*) AS n FROM tool_calls WHERE ts > ?", (time.time() - 86400,)
        ).fetchone()["n"]
        calls_hour = conn.execute(
            "SELECT COUNT(*) AS n FROM tool_calls WHERE ts > ?", (time.time() - 3600,)
        ).fetchone()["n"]
        lines.append(f"\nВызовов инструментов: {calls_hour} за час, {calls_day} за сутки")

        subagents = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind = 'subagent_message' AND ts > ?",
            (time.time() - 3600,),
        ).fetchone()["n"]
        if subagents:
            lines.append(f"Отчётов субагентов за час: {subagents}")
        return "\n".join(lines)

    def render_last_report(self) -> str:
        conn = journal.connect()
        row = conn.execute(
            "SELECT * FROM events WHERE kind = 'daily_report' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return "Отчётов пока не было. Первый придёт после закрытия торгов."
        payload = json.loads(row["payload"] or "{}")
        when = datetime.fromtimestamp(row["ts"], MSK).strftime("%d.%m %H:%M")
        return f"<b>Отчёт от {when}</b>\n\n{payload.get('summary', '')}"

    # --- обработка входящих ------------------------------------------------

    def handle(self, text: str) -> None:
        command = text.strip()

        if command in ("/start", "/help"):
            self.send(
                "Пульт торгового агента.\n\n"
                "Кнопки показывают состояние из журнала — работают, даже когда "
                "агент занят.\n\n"
                "<b>Любой другой текст уходит агенту сообщением.</b> Он получит "
                "его в работе и ответит, когда сочтёт нужным.",
            )
        elif command in (BTN_PORTFOLIO, "/portfolio"):
            self.send(self.render_portfolio())
        elif command in (BTN_RESULT, "/result"):
            self.send(self.render_result())
        elif command in (BTN_TRADES, "/trades"):
            self.send(self.render_trades())
        elif command in (BTN_STATUS, "/status"):
            self.send(self.render_status())
        elif command in (BTN_REPORT, "/report"):
            self.send(self.render_last_report())
        elif command in (BTN_LIMITS, "/limits"):
            # По кнопке обновляем принудительно: смотрят её тогда, когда
            # хотят знать положение дел сейчас, а не пять минут назад.
            self.send(self.render_limits(force=True))
        elif command in (BTN_HALT, "/halt"):
            journal.kv_set("halted", "остановлено оператором из Telegram")
            journal.log_event("halt", {"reason": "оператор"})
            self.send("⛔ Торговля остановлена. Снять: /resume")
        elif command == "/resume":
            journal.kv_set("halted", "")
            journal.log_event("resume", {"by": "оператор"})
            self.send("▶️ Остановка снята, агент снова может торговать.")
        elif self.on_message:
            self.on_message(text)
            self.send("Передал агенту.", keyboard=False)

    def poll(self, timeout: int = 25) -> int:
        """Один цикл long-polling. Возвращает число обработанных сообщений."""
        if not self.token:
            return 0
        response = self._api(
            "getUpdates",
            {"offset": self.offset + 1, "timeout": timeout, "allowed_updates": ["message"]},
            timeout=timeout + 15,
        )
        handled = 0
        for update in response.get("result", []):
            self.offset = max(self.offset, update["update_id"])
            message = update.get("message") or {}
            text = message.get("text")
            # Чужие чаты игнорируем: бот обслуживает одного оператора.
            if not text or str(message.get("chat", {}).get("id")) != str(self.chat_id):
                continue
            try:
                self.handle(text)
            except Exception as exc:  # noqa: BLE001
                journal.log_event("telegram_error", {"text": text, "error": str(exc)})
            handled += 1
        if handled or response.get("result"):
            journal.kv_set("tg_offset", str(self.offset))
        return handled
