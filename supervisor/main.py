"""Супервизор: держит сессию агента живой и доставляет в неё события.

Часами владеет супервизор, агент только выражает намерение через
schedule_wakeup. Так расписание переживает ошибки агента: даже если он
перестал ставить будильники, дедмен разбудит его сам.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from gateway import config, journal
from supervisor.appserver import AppServer, AppServerError
from supervisor.telegram import Bot

MSK = ZoneInfo("Europe/Moscow")

WORKDIR = os.environ.get("AGENT_WORKDIR", "/home/agent/work")
CODEX_BIN = os.environ.get("CODEX_BIN", "/usr/local/bin/codex")

# Регулярная проверка «нужно ли что-то делать» поверх собственных будильников
# агента. В сессию идёт время, поэтому чаще смысла нет.
CHECK_INTERVAL_MARKET = 60 * 60
CHECK_INTERVAL_CLOSED = 4 * 60 * 60

# Если агент не подал признаков жизни столько времени — будим принудительно.
DEADMAN_TIMEOUT = 3 * 60 * 60

DAILY_REPORT_HOUR = 18
DAILY_REPORT_MINUTE = 45

MARKET_OPEN = (10, 0)
MARKET_CLOSE = (23, 50)


def now_msk() -> datetime:
    return datetime.now(MSK)


def market_open(moment: datetime | None = None) -> bool:
    moment = moment or now_msk()
    if moment.weekday() >= 5:
        return False
    minutes = moment.hour * 60 + moment.minute
    return MARKET_OPEN[0] * 60 + MARKET_OPEN[1] <= minutes <= MARKET_CLOSE[0] * 60 + MARKET_CLOSE[1]


def omniroute_healthy() -> tuple[bool, str]:
    """Туннель trycloudflare самопроизвольно отваливается; будить агента
    в неработающий канал бессмысленно — он просто потеряет ход."""
    base = config.secret("OMNIROUTE_BASE_URL").rstrip("/")
    key = config.secret("OMNIROUTE_API_KEY")
    if not base:
        return False, "не задан OMNIROUTE_BASE_URL"
    request = urllib.request.Request(
        f"{base}/models", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status == 200, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:120]


class Supervisor:
    def __init__(self) -> None:
        self.inbox: queue.Queue[str] = queue.Queue()
        self.bot = Bot(on_message=self.inbox.put)
        self.codex: AppServer | None = None
        self.running = True
        self.last_activity = time.time()
        self.last_check = 0.0
        self.last_report_date = journal.kv_get("last_report_date", "")

    # --- запуск ------------------------------------------------------------

    def start_codex(self) -> None:
        # Codex читает ключ провайдера из собственного окружения (env_key в
        # config.toml). systemd его туда не кладёт, поэтому подставляем сами
        # из файла секретов — иначе каждый ход падает с
        # "Missing environment variable".
        env = dict(os.environ)
        env.setdefault("HOME", "/home/agent")
        for name in ("OMNIROUTE_API_KEY", "OMNIROUTE_BASE_URL"):
            value = config.secret(name)
            if value:
                env[name] = value
        self.codex = AppServer([CODEX_BIN, "app-server"], cwd=WORKDIR, env=env)
        self.codex.start()

        stored = journal.kv_get("thread_id", "")
        if stored:
            try:
                self.codex.resume_thread(stored)
                journal.log_event("thread_resumed", {"thread_id": stored})
                return
            except AppServerError as exc:
                journal.log_event("thread_resume_failed", {"error": str(exc)[:300]})
        thread_id = self.codex.start_thread(WORKDIR)
        journal.kv_set("thread_id", thread_id)
        journal.log_event("thread_started", {"thread_id": thread_id})

    # --- доставка сообщений ------------------------------------------------

    def deliver(self, text: str, kind: str) -> None:
        healthy, detail = omniroute_healthy()
        if not healthy:
            journal.log_event("wake_skipped", {"kind": kind, "reason": detail})
            self.bot.send(
                f"⚠️ Канал к модели недоступен ({detail}). "
                f"Пробуждение «{kind}» пропущено, повторю позже.",
                keyboard=False,
            )
            return
        try:
            mode = self.codex.deliver(text)
            journal.log_event("wake", {"kind": kind, "mode": mode, "text": text[:400]})
            journal.kv_set("agent_state", "работает")
            self.last_activity = time.time()
        except AppServerError as exc:
            journal.log_event("wake_failed", {"kind": kind, "error": str(exc)[:300]})
            self.bot.send(f"⚠️ Не удалось разбудить агента: {exc}", keyboard=False)

    def wake_text(self, kind: str, reason: str = "") -> str:
        stamp = now_msk().strftime("%d.%m.%Y %H:%M")
        market = "торги идут" if market_open() else "биржа закрыта"
        if kind == "alarm":
            head = f"Сработал твой будильник. Ты просил разбудить: {reason}"
        elif kind == "check":
            head = (
                "Регулярная проверка. Оцени обстановку и реши, чем заняться. "
                "Если торговать не время — это не повод заканчивать ход: "
                "разбирайся в рынке, проверяй гипотезы, готовься к открытию. "
                "Спать имеет смысл, когда картина есть и ты ждёшь конкретного "
                "момента."
            )
        elif kind == "report":
            head = (
                "Основная сессия закрыта. Время дневного отчёта: напиши разбор "
                "в notes/reports/ и отправь его через send_report."
            )
        elif kind == "deadman":
            head = (
                "Ты не назначил следующее пробуждение и давно не подавал признаков "
                "жизни. Проверь состояние и обязательно поставь будильник."
            )
        else:
            head = reason
        return f"[{stamp} МСК, {market}]\n\n{head}"

    # --- периодические задачи ----------------------------------------------

    def tick_wakeup(self) -> None:
        pending = journal.pending_wakeup()
        if pending and pending["due_ts"] <= time.time():
            journal.mark_wakeup_fired(pending["id"])
            self.deliver(self.wake_text("alarm", pending["reason"]), "alarm")

    def tick_regular(self) -> None:
        interval = CHECK_INTERVAL_MARKET if market_open() else CHECK_INTERVAL_CLOSED
        if time.time() - self.last_check < interval:
            return
        self.last_check = time.time()
        # Собственный будильник агента важнее регулярной проверки: если он
        # уже назначен на ближайшее время, не мешаем.
        pending = journal.pending_wakeup()
        if pending and pending["due_ts"] - time.time() < interval / 2:
            return
        self.deliver(self.wake_text("check"), "check")

    def tick_deadman(self) -> None:
        if time.time() - self.last_activity < DEADMAN_TIMEOUT:
            return
        if journal.pending_wakeup():
            return
        self.deliver(self.wake_text("deadman"), "deadman")

    def tick_report(self) -> None:
        moment = now_msk()
        today = moment.strftime("%Y-%m-%d")
        if self.last_report_date == today or moment.weekday() >= 5:
            return
        if (moment.hour, moment.minute) < (DAILY_REPORT_HOUR, DAILY_REPORT_MINUTE):
            return
        self.last_report_date = today
        journal.kv_set("last_report_date", today)
        self.deliver(self.wake_text("report"), "report")

    def tick_events(self) -> None:
        for event in self.codex.drain_events():
            method = event.get("method")
            params = event.get("params") or {}

            thread_id = params.get("threadId") or ""
            own = not thread_id or thread_id == self.codex.thread_id

            if method == "turn/completed" and own:
                journal.kv_set("agent_state", "спит")
                journal.kv_set("last_turn_end", str(time.time()))
                self.last_activity = time.time()
                journal.log_event("turn_completed", {"usage": params.get("usage") or {}})
            elif method == "item/completed":
                item = params.get("item") or {}
                # app-server отдаёт типы в camelCase: agentMessage, userMessage.
                kind = item.get("type")
                if kind == "agentMessage":
                    text = (item.get("text") or "")[:4000]
                    journal.log_event(
                        "agent_message" if own else "subagent_message", {"text": text}
                    )
                    self.last_activity = time.time()
                elif kind == "error":
                    journal.log_event("item_error", {"message": str(item.get("message"))[:500]})
            elif method == "error":
                # Ошибки хода приходят нотификацией, а не в stderr: без этого
                # ход молча завершается пустым и причина не видна нигде.
                detail = (params.get("error") or {}).get("message", str(params))
                journal.log_event("turn_error", {"error": str(detail)[:500]})
                self.bot.send(f"⚠️ Ошибка хода агента:\n{str(detail)[:500]}", keyboard=False)

    def tick_inbox(self) -> None:
        while True:
            try:
                text = self.inbox.get_nowait()
            except queue.Empty:
                break
            self.deliver(f"Сообщение от оператора: {text}", "operator")

        # Очередь в базе: способ дотянуться до агента мимо Telegram, не трогая
        # его расписание. Служебное сообщение не должно стирать будильник.
        for row in journal.take_messages():
            prefix = "Сообщение от оператора" if row["source"] == "operator" else "Служебное"
            self.deliver(f"{prefix}: {row['text']}", row["source"])

    def tick_halt(self) -> bool:
        """True — прогон остановлен, будить агента больше не нужно."""
        reason = journal.kv_get("halted", "")
        if reason and journal.kv_get("halt_notified", "") != reason:
            journal.kv_set("halt_notified", reason)
            self.bot.send(f"⛔ <b>Прогон остановлен</b>\n{reason}\n\nСнять: /resume")
        return bool(reason)

    # --- главный цикл ------------------------------------------------------

    def run(self) -> None:
        journal.log_event("supervisor_start", {})
        self.bot.set_commands()
        self.start_codex()
        self.bot.send("▶️ Супервизор запущен, сессия агента активна.")

        poller = threading.Thread(target=self._poll_telegram, daemon=True)
        poller.start()

        while self.running:
            try:
                if not self.codex.alive():
                    journal.log_event("codex_died", {})
                    self.bot.send("⚠️ Процесс Codex завершился, перезапускаю.", keyboard=False)
                    self.start_codex()

                self.tick_events()
                self.tick_inbox()
                if not self.tick_halt():
                    self.tick_wakeup()
                    self.tick_report()
                    self.tick_regular()
                    self.tick_deadman()
            except Exception as exc:  # noqa: BLE001 — цикл не должен падать
                journal.log_event("supervisor_error", {"error": repr(exc)[:500]})
            time.sleep(5)

        self.codex.stop()
        journal.log_event("supervisor_stop", {})

    def _poll_telegram(self) -> None:
        while self.running:
            try:
                self.bot.poll(timeout=25)
            except Exception as exc:  # noqa: BLE001
                journal.log_event("telegram_poll_error", {"error": repr(exc)[:300]})
                time.sleep(5)

    def shutdown(self, *_: object) -> None:
        self.running = False


def main() -> None:
    Path(WORKDIR).mkdir(parents=True, exist_ok=True)
    supervisor = Supervisor()
    signal.signal(signal.SIGTERM, supervisor.shutdown)
    signal.signal(signal.SIGINT, supervisor.shutdown)
    supervisor.run()


if __name__ == "__main__":
    main()
