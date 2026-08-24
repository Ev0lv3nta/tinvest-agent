"""Супервизор: держит сессию агента живой и доставляет в неё события.

Часами владеет супервизор, агент только выражает намерение через
schedule_wakeup. Так расписание переживает ошибки агента: даже если он
перестал ставить будильники, дедмен разбудит его сам.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gateway import config, journal, limits
from supervisor import reconcile
from supervisor.appserver import AppServer, AppServerError, Busy
from supervisor.telegram import Bot
from supervisor.watcher import Watcher

MSK = ZoneInfo("Europe/Moscow")

WORKDIR = os.environ.get("AGENT_WORKDIR", "/home/agent/work")
CODEX_BIN = os.environ.get("CODEX_BIN", "/usr/local/bin/codex")

# Регулярная проверка «нужно ли что-то делать» поверх собственных будильников
# агента. В сессию идёт время, поэтому чаще смысла нет.
CHECK_INTERVAL_MARKET = 60 * 60
CHECK_INTERVAL_CLOSED = 4 * 60 * 60

# Если агент не подал признаков жизни столько времени — будим принудительно.
DEADMAN_TIMEOUT = 3 * 60 * 60

# Сверка операций с журналом: во время торгов чаще, вне — реже.
RECONCILE_INTERVAL_MARKET = 8 * 60
RECONCILE_INTERVAL_CLOSED = 60 * 60

DAILY_REPORT_HOUR = 18
DAILY_REPORT_MINUTE = 45

# Не удалось доставить — вернёмся к поводу через столько секунд.
RETRY_DELAY = 120

# Ход идёт, а событий нет столько секунд — считаем зависшим.
STUCK_SILENCE = 12 * 60
MAX_TURN_SECONDS = 40 * 60

# Как называется занятие агента для панели наблюдения.
ACTIVITY = {
    "reasoning": "думает",
    "mcpToolCall": "инструмент",
    "commandExecution": "команда",
    "webSearch": "ищет",
    "collabToolCall": "субагенты",
    "collabAgentToolCall": "субагенты",
    "fileChange": "правит файлы",
    "agentMessage": "отвечает",
}


# Фазы торгового дня МосБиржи. Раньше здесь было одно окно 10:00-23:50, и
# в 08:52 агенту приходило «биржа закрыта» при статусе NORMAL_TRADING, а
# вечером между сессиями — «торги идут». Название фазы честнее булева флага;
# окончательную правду о конкретной бумаге всё равно даёт её trading_status.
SESSIONS = (
    ("утренняя сессия", (6, 50), (9, 50)),
    ("основная сессия", (10, 0), (18, 40)),
    ("вечерняя сессия", (19, 5), (23, 50)),
)


def _spaced(value: float, sign: bool = False) -> str:
    """Число с разделёнными разрядами: 99 724, −276.

    Обычный пробел, а не типографский: текст уходит в модель, и экзотические
    символы там ни к чему.
    """
    text = f"{value:+,.0f}" if sign else f"{value:,.0f}"
    return text.replace(",", " ").replace("-", "−")


def now_msk() -> datetime:
    return datetime.now(MSK)


def _minutes(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def session_name(moment: datetime | None = None) -> str:
    moment = moment or now_msk()
    if moment.weekday() >= 5:
        return "выходной"
    minutes = _minutes(moment)
    for name, start, end in SESSIONS:
        if start[0] * 60 + start[1] <= minutes <= end[0] * 60 + end[1]:
            return name
    return "вне торгов"


def market_open(moment: datetime | None = None) -> bool:
    return session_name(moment) not in ("выходной", "вне торгов")


def main_session(moment: datetime | None = None) -> bool:
    return session_name(moment) == "основная сессия"


_HEALTH_CACHE: dict = {"ts": 0.0, "ok": False, "detail": ""}
HEALTH_TTL = 45.0


def omniroute_healthy(force: bool = False) -> tuple[bool, str]:
    """Будить агента в неработающий канал бессмысленно — он потеряет ход.

    Результат кешируется: раньше проверка с таймаутом 20 секунд выполнялась
    синхронно на каждую доставку и подвешивала главный цикл.
    """
    if not force and time.time() - _HEALTH_CACHE["ts"] < HEALTH_TTL:
        return _HEALTH_CACHE["ok"], _HEALTH_CACHE["detail"]
    ok, detail = _probe_omniroute()
    _HEALTH_CACHE.update({"ts": time.time(), "ok": ok, "detail": detail})
    return ok, detail


def _probe_omniroute() -> tuple[bool, str]:
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
        # Сообщения оператора идут в базу, а не в очередь процесса: очередь
        # в памяти исчезала при перезапуске уже после ответа «передал агенту».
        self.bot = Bot(on_message=lambda text: journal.enqueue_message(text, "operator"))
        self.codex: AppServer | None = None
        self.running = True
        self.last_activity = time.time()
        self.last_delivery = time.time()
        # Не ноль: иначе первый же проход цикла после каждого рестарта
        # создаёт лишнее пробуждение, а рестартов за день бывает пять.
        self.last_check = time.time()
        self.last_report_date = journal.kv_get("last_report_date", "")
        self.last_reconcile = 0.0
        self.last_deadman = 0.0
        self.watcher: Watcher | None = None
        # Экземплярные, а не классовые: изменяемые атрибуты класса — ловушка.
        self._seen_methods: set[str] = set()
        self._reasoning: dict[str, dict] = {}

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

    def reset_live_state(self) -> None:
        """После старта агент ничего не делает, пока его не разбудят.

        Без этого панель показывала «работает» с прошлого запуска: значение
        писалось при доставке и никем не сбрасывалось.
        """
        journal.kv_set("agent_state", "спит")
        journal.kv_set("live_text", "")
        journal.kv_set("live_activity", "")

    # --- доставка сообщений ------------------------------------------------

    def deliver(self, text: str, kind: str, urgent: bool = False) -> bool:
        """Доставить событие агенту. False — не дошло, повод не гасить.

        Раньше метод ничего не возвращал, а вызывающий помечал будильник
        сработавшим и сообщение доставленным ДО вызова. В журнале первого
        прогона есть ровно такой случай: сообщение оператора помечено
        доставленным в 09:05:53, а в 09:05:56 записан пропуск по HTTP 530.
        Оператору бот при этом ответил, что передал.
        """
        healthy, detail = omniroute_healthy()
        if not healthy:
            journal.log_event("wake_skipped", {"kind": kind, "reason": detail})
            # Пока канал лежит, пробуждения сыплются одно за другим.
            # Сообщаем о начале сбоя и потом не чаще раза в полчаса.
            last = float(journal.kv_get("outage_notified", "0") or 0)
            if time.time() - last > 1800:
                journal.kv_set("outage_notified", str(time.time()))
                self.bot.send(
                    f"⚠️ <b>Канал к модели недоступен</b> ({detail})\n\n"
                    f"Агент не может работать. Проверить туннель:\n"
                    f"<code>systemctl status omniroute-tunnel</code>\n"
                    f"События не теряются — они будут доставлены, когда канал "
                    f"вернётся."
                )
            return False
        if journal.kv_get("outage_notified", ""):
            journal.kv_set("outage_notified", "")
            self.bot.send("✅ Канал к модели восстановлен, агент продолжает работу.")
        try:
            mode = self.codex.deliver(text, urgent=urgent)
        except Busy:
            # Рутина ждёт конца хода, это не отказ.
            return False
        except AppServerError as exc:
            journal.log_event("wake_failed", {"kind": kind, "error": str(exc)[:300]})
            self.bot.send(f"⚠️ Не удалось разбудить агента: {exc}", keyboard=False)
            return False
        journal.log_event("wake", {"kind": kind, "mode": mode, "text": text[:400]})
        journal.log_transcript("wake", title=kind, body=text)
        journal.kv_set("agent_state", "работает")
        journal.kv_set("live_text", "")
        journal.kv_set("live_activity", "думает")
        self.last_delivery = time.time()
        return True

    def state_block(self) -> str:
        """Готовый срез состояния в текст пробуждения.

        Всё это уже лежит в журнале, и собрать его стоит ноль токенов. Без
        него агент тратил первый ход на восстановление: за прошлый прогон
        все 44 запуска команд были перечитыванием собственных заметок.
        """
        lines: list[str] = []
        conn = journal.connect()

        snapshot = conn.execute(
            "SELECT * FROM snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if snapshot:
            total = float(snapshot["total"])
            start = config.STARTING_CAPITAL
            day = journal.day_result(total)
            piece = f"Портфель {_spaced(total)} ₽ ({_spaced(total - start, sign=True)} к старту"
            if day is not None:
                piece += f", {_spaced(day, sign=True)} за сегодня"
            lines.append(piece + ")")
            positions = json.loads(snapshot["positions"] or "[]")
            if positions:
                for position in positions[:5]:
                    lines.append(
                        f"  {position.get('ticker') or '?'}: "
                        f"{position.get('lots', 0):g} лот, "
                        f"{position.get('yield', 0):+.0f} ₽"
                    )
            else:
                lines.append("  позиций нет")

        entries = journal.entries_today()
        lines.append(
            f"Сегодня входов {entries} из {config.MAX_ENTRIES_PER_DAY}, "
            f"дневной стоп −{config.DAILY_LOSS_LIMIT:.0f} ₽"
        )

        open_orders = journal.open_orders()
        if open_orders:
            lines.append(f"Незакрытых заявок: {len(open_orders)}")

        watches = journal.active_watches()
        if watches:
            shown = ", ".join(
                f"{row['ticker']} {row['kind'].replace('price_', '')} {row['threshold']:g}"
                for row in watches[:4]
            )
            lines.append(f"Следит код: {shown}")

        alarms = journal.active_wakeups()
        if alarms:
            when = datetime.fromtimestamp(alarms[0]["due_ts"], MSK).strftime("%H:%M")
            lines.append(f"Ближайший будильник {when}: {alarms[0]['reason'][:80]}")

        usage = journal.usage_since(24 * 3600)
        if usage["turns"]:
            piece = f"За сутки ходов {usage['turns']}"
            if usage["total"]:
                piece += f", токенов {_spaced(usage['total'])}"
            if usage["context_used"] and usage["context_window"]:
                share = usage["context_used"] / usage["context_window"] * 100
                piece += f"; контекст заполнен на {share:.0f}%"
            lines.append(piece)

        quota = self.quota_line()
        if quota:
            lines.append(quota)

        failure = journal.kv_get("mcp_failed:trading", "")
        if failure:
            lines.append(
                f"⚠️ Торговые инструменты недоступны ({failure[:120]}). "
                f"Торговать нечем — сообщи оператору."
            )

        return "\n".join(lines)

    @staticmethod
    def quota_line() -> str:
        try:
            data = limits.fetch()
        except Exception:  # noqa: BLE001 — квота не повод ломать пробуждение
            return ""
        accounts = data.get("accounts") or []
        if not accounts:
            return ""
        parts = [f"{account['percent']:.0f}%" for account in accounts]
        return "Квота аккаунтов: " + " / ".join(parts) + " использовано"

    def wake_text(self, kind: str, reason: str = "") -> str:
        stamp = now_msk().strftime("%d.%m.%Y %H:%M")
        if kind == "alarm":
            head = f"Сработал твой будильник. Ты просил разбудить: {reason}"
        elif kind == "watch":
            head = f"Сработало условие, которое ты поставил.\n{reason}"
        elif kind == "check":
            head = (
                "Регулярная проверка. Если по заметкам действовать не нужно — "
                "сверься с обстановкой минимумом вызовов, обнови state.md и "
                "поставь наблюдатели под то, чего ждёшь. Пустое пробуждение "
                "стоит дёшево; дорого стоит пустое пробуждение с полным сканом."
            )
        elif kind == "report":
            head = (
                "Основная сессия закрыта. Время дневного отчёта: напиши разбор "
                "в notes/reports/ и отправь его через send_report."
            )
        elif kind == "deadman":
            head = (
                "Ты давно не подавал признаков жизни и не оставил ни будильника, "
                "ни наблюдателей. Проверь состояние и реши, чего ждёшь дальше."
            )
        elif kind == "restart":
            head = (
                "Супервизор перезапускался, твой последний ход мог оборваться "
                "на середине. Сверь состояние: портфель, активные заявки, "
                "наблюдатели — и продолжай."
            )
        else:
            head = reason

        state = self.state_block()
        header = f"[{stamp} МСК · {session_name()}]"
        return f"{header}\n{state}\n\n{head}" if state else f"{header}\n\n{head}"

    # --- периодические задачи ----------------------------------------------

    def tick_wakeup(self) -> None:
        """Все просроченные будильники. Гасим только после доставки."""
        for pending in journal.due_wakeups():
            delivered = self.deliver(
                self.wake_text("alarm", pending["reason"]), "alarm", urgent=False
            )
            if delivered:
                journal.mark_wakeup_fired(pending["id"])
            else:
                # Повод не исчез — вернёмся к нему через пару минут.
                journal.postpone_wakeup(pending["id"], RETRY_DELAY)
            return  # за проход отдаём не больше одного пробуждения

    def tick_regular(self) -> None:
        interval = CHECK_INTERVAL_MARKET if market_open() else CHECK_INTERVAL_CLOSED
        if time.time() - self.last_check < interval:
            return
        # Если агент сам назначил себе повод на ближайшее время или поставил
        # наблюдатели, регулярная проверка только мешает.
        pending = journal.pending_wakeup()
        if pending and pending["due_ts"] - time.time() < interval / 2:
            self.last_check = time.time()
            return
        if journal.active_watches() and market_open():
            self.last_check = time.time()
            return
        if self.deliver(self.wake_text("check"), "check"):
            self.last_check = time.time()
        else:
            # Не дошло (канал лёг или ход занят) — вернёмся через пару минут,
            # а не на каждом проходе цикла.
            self.last_check = time.time() - interval + RETRY_DELAY

    def tick_deadman(self) -> None:
        if time.time() - self.last_progress() < DEADMAN_TIMEOUT:
            return
        if time.time() - self.last_deadman < RETRY_DELAY:
            return
        # Будильник в далёком будущем не считается признаком жизни: раньше
        # повод на «+3 дня» глушил сторожок на трое суток.
        pending = journal.pending_wakeup()
        if pending and pending["due_ts"] - time.time() < DEADMAN_TIMEOUT:
            return
        if journal.active_watches() and market_open():
            return
        self.last_deadman = time.time()
        self.deliver(self.wake_text("deadman"), "deadman")

    def last_progress(self) -> float:
        """Признак жизни — входящее событие, а не наша доставка.

        Иначе периодическая доставка «оживляла» зависший ход и сторожок
        не срабатывал никогда.
        """
        return max(self.codex.last_inbound, self.last_activity)

    def tick_stuck_turn(self) -> None:
        """Ход идёт, событий нет — вероятно, провайдер перестал стримить."""
        if not self.codex.busy or not self.codex.turn_started:
            return
        silent = time.time() - self.codex.last_inbound
        running = time.time() - self.codex.turn_started
        if silent < STUCK_SILENCE and running < MAX_TURN_SECONDS:
            return
        journal.log_event(
            "turn_stuck", {"silent_sec": int(silent), "running_sec": int(running)}
        )
        self.codex.interrupt()
        self.bot.send(
            f"⚠️ Ход агента прерван: молчание {int(silent / 60)} мин, "
            f"длительность {int(running / 60)} мин.",
            keyboard=False,
        )
        journal.enqueue_message(
            "Твой предыдущий ход был прерван супервизором: он шёл слишком долго "
            "или перестал подавать признаки жизни. Сверь состояние и продолжай "
            "короче.",
            source="system",
        )

    def tick_report(self) -> None:
        moment = now_msk()
        today = moment.strftime("%Y-%m-%d")
        if self.last_report_date == today or moment.weekday() >= 5:
            return
        if (moment.hour, moment.minute) < (DAILY_REPORT_HOUR, DAILY_REPORT_MINUTE):
            return
        if self.deliver(self.wake_text("report"), "report"):
            self.last_report_date = today
            journal.kv_set("last_report_date", today)

    @staticmethod
    def _record_usage(params: dict) -> None:
        """Расход хода из thread/tokenUsage/updated.

        Форма взята из схемы протокола: ThreadTokenUsage = {last, total,
        modelContextWindow}, где last и total — разбивка по видам токенов.
        Поля вложены, а не лежат сверху; у turn/completed поля usage нет
        вовсе — там только completedAt, durationMs, error, id, items,
        startedAt и status.
        """
        usage = params.get("tokenUsage")
        if not isinstance(usage, dict):
            return
        last = usage.get("last") if isinstance(usage.get("last"), dict) else {}
        total = usage.get("total") if isinstance(usage.get("total"), dict) else {}

        def num(source: dict, name: str):
            value = source.get(name)
            return int(value) if isinstance(value, (int, float)) else None

        window = usage.get("modelContextWindow")
        # Сколько занято в окне: последний ход целиком пересылает разговор,
        # поэтому его вход плюс выход и есть текущее наполнение контекста.
        used = None
        if isinstance(last, dict):
            parts = [num(last, "inputTokens"), num(last, "outputTokens")]
            if any(p is not None for p in parts):
                used = sum(p or 0 for p in parts)

        journal.log_usage(
            params.get("turnId") or "",
            {
                "input": num(last, "inputTokens"),
                "cached": num(last, "cachedInputTokens"),
                "output": num(last, "outputTokens"),
                "reasoning": num(last, "reasoningOutputTokens"),
                "total": num(last, "totalTokens") or num(total, "totalTokens"),
                "context_used": used,
                "context_window": int(window) if isinstance(window, (int, float)) else None,
            },
        )

    def _mcp_status(self, params: dict) -> None:
        """Отказ шлюза не должен проходить незамеченным.

        Иначе агент получает сессию без единого торгового инструмента и
        рассуждает о рынке словами, а супервизор об этом не знает.
        """
        name = params.get("name") or "?"
        status = str(params.get("status") or "")
        journal.kv_set(f"mcp_status:{name}", status)

        # Отказом считаем только явный отказ: набор статусов у разных версий
        # Codex отличается, и принимать промежуточный за поломку — значит
        # будить оператора на каждом старте.
        reason = params.get("failureReason") or params.get("error")
        broken = bool(reason) or any(
            word in status.lower() for word in ("fail", "error", "crash")
        )
        if not broken:
            if journal.kv_get(f"mcp_failed:{name}", ""):
                journal.kv_set(f"mcp_failed:{name}", "")
                self.bot.send(f"✅ MCP-сервер {name} поднялся.", keyboard=False)
            return
        detail = str(reason or status)[:300]
        journal.log_event("mcp_failed", {"name": name, "detail": detail})
        if journal.kv_get(f"mcp_failed:{name}", "") == detail:
            return
        journal.kv_set(f"mcp_failed:{name}", detail)
        self.bot.send(
            f"⚠️ MCP-сервер {name} не поднялся: {detail}\n"
            f"Агент остался без торговых инструментов.",
            keyboard=False,
        )

    def tick_inbox(self) -> None:
        """Очередь сообщений в базе. Пометка — только после доставки."""
        for row in journal.peek_messages():
            source = row["source"]
            if source == "operator":
                prefix, urgent = "Сообщение от оператора", True
            elif source == "watch":
                prefix, urgent = "", True
            else:
                prefix, urgent = "Служебное", False
            text = (
                self.wake_text("watch", row["text"])
                if source == "watch"
                else f"{prefix}: {row['text']}"
            )
            if not self.deliver(text, source, urgent=urgent):
                return  # порядок сообщений важнее скорости
            journal.mark_message_delivered(row["id"])

    def tick_halt(self) -> bool:
        """True — прогон остановлен, новые пробуждения не нужны."""
        reason = journal.kv_get("halted", "")
        if reason and journal.kv_get("halt_notified", "") != reason:
            journal.kv_set("halt_notified", reason)
            self.bot.send(
                f"⛔ <b>Прогон остановлен</b>\n{reason}\n\n"
                f"Новые позиции запрещены, закрыть имеющиеся агент может. "
                f"Снять: /resume"
            )
        return bool(reason)

    # Как элемент ленты выглядит для читателя: заголовок и тело.
    # Полная нагрузка всё равно сохраняется отдельно.
    @staticmethod
    def _describe(item: dict) -> tuple[str, str, str]:
        kind = item.get("type") or "unknown"

        def first(*keys, default=""):
            for key in keys:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value
                if isinstance(value, list) and value:
                    parts = [
                        p.get("text", "") if isinstance(p, dict) else str(p) for p in value
                    ]
                    joined = "\n".join(x for x in parts if x)
                    if joined.strip():
                        return joined
            return default

        if kind in ("agentMessage", "userMessage"):
            return kind, "", first("text", "content")
        if kind == "reasoning":
            return kind, "", first("text", "summary", "content")
        if kind == "commandExecution":
            command = first("command", "commandLine")
            output = first("aggregatedOutput", "output", "stdout")
            return kind, command[:400], output
        if kind == "mcpToolCall":
            tool = item.get("tool") or item.get("name") or ""
            server = item.get("server") or ""
            title = f"{server}.{tool}" if server else str(tool)

            # Результат приходит конвертом MCP: {"content": [{"type": "text", ...}]}.
            # Для чтения нужен сам текст, а не структура вокруг него.
            result = item.get("result")
            text = ""
            if isinstance(result, dict):
                for block in result.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text += block.get("text", "")
            elif result is not None:
                text = str(result)

            arguments = item.get("arguments") or {}
            head = json.dumps(arguments, ensure_ascii=False) if arguments else ""
            body = f"{head}\n\n{text}".strip() if head else text
            return kind, title, body
        if kind == "webSearch":
            action = item.get("action") or {}
            query = item.get("query") or ""
            if not query and isinstance(action, dict):
                queries = action.get("queries")
                query = ", ".join(queries) if queries else action.get("url", "")
            return kind, str(query)[:400], ""
        if kind in ("collabToolCall", "collabAgentToolCall"):
            return kind, str(item.get("tool") or ""), first("prompt")
        if kind == "fileChange":
            changes = item.get("changes") or []
            names = [c.get("path", "") for c in changes] if isinstance(changes, list) else []
            return kind, ", ".join(n for n in names if n)[:400], ""
        if kind == "error":
            return kind, "", first("message", "text")
        if kind == "contextCompaction":
            # Сжатие — важное событие: детали разговора после него теряются.
            return kind, "сжатие контекста", ""
        return kind, "", first("text", "message")

    def tick_reconcile(self) -> None:
        interval = RECONCILE_INTERVAL_MARKET if market_open() else RECONCILE_INTERVAL_CLOSED
        if time.time() - self.last_reconcile < interval:
            return
        self.last_reconcile = time.time()

        # Срез портфеля снимаем сами, а не ждём, пока агент вызовет portfolio.
        # Иначе кривая эквити строится по случайным моментам, дневная опора
        # может не появиться вовсе, а падение ниже порога у неторгующего
        # агента никто не заметит.
        try:
            reconcile.sync_orders()
            portfolio = reconcile.client().portfolio()
            journal.log_snapshot(
                portfolio["total"], portfolio["cash"], portfolio["positions"]
            )
            if portfolio["total"] < config.CAPITAL_FLOOR:
                self.trip_floor(portfolio["total"])
        except Exception as exc:  # noqa: BLE001 — сверка не должна ронять цикл
            journal.log_event("snapshot_failed", {"error": str(exc)[:200]})

        found = reconcile.check_and_alert(lambda text: self.bot.send(text))
        if found:
            journal.log_event("reconcile_alerted", {"count": found})

    def trip_floor(self, total: float) -> None:
        from gateway import guards

        if guards.halted():
            return
        guards.halt(
            f"стоимость портфеля {total:.2f} ₽ опустилась ниже порога "
            f"{config.CAPITAL_FLOOR:.0f} ₽"
        )
        journal.enqueue_message(
            f"Портфель опустился ниже порога {config.CAPITAL_FLOOR:.0f} ₽ "
            f"(сейчас {total:.2f} ₽). Новые позиции заблокированы. Закрыть "
            f"имеющиеся ты можешь и должен решить, надо ли. Опиши в заметках, "
            f"что привело сюда.",
            source="system",
        )

    def tick_events(self) -> None:
        for event in self.codex.drain_events():
            # Незнакомое событие логируем один раз: так видно, что поток
            # отдаёт на самом деле, без шума на каждом кадре.
            method_name = event.get("method") or ""
            if method_name not in self._seen_methods:
                self._seen_methods.add(method_name)
                journal.log_event(
                    "method_seen",
                    {"method": method_name, "keys": sorted((event.get("params") or {}).keys())},
                )
            method = event.get("method")
            params = event.get("params") or {}
            thread_id = params.get("threadId") or ""
            own = not thread_id or thread_id == self.codex.thread_id

            if method == "turn/completed" and own:
                journal.kv_set("agent_state", "спит")
                journal.kv_set("live_text", "")
                journal.kv_set("live_activity", "")
                journal.kv_set("last_turn_end", str(time.time()))
                self.last_activity = time.time()
                # usage у turn/completed нет: ключи события — threadId и turn.
                # Настоящий расход приходит отдельным thread/tokenUsage/updated,
                # и до этой правки в журнал писались нули.
                turn = params.get("turn") or {}
                journal.log_event(
                    "turn_completed",
                    {
                        "turn_id": turn.get("id", ""),
                        "status": turn.get("status"),
                        "duration_sec": round((turn.get("durationMs") or 0) / 1000, 1),
                        "error": str(turn.get("error"))[:200] if turn.get("error") else None,
                    },
                )
                journal.log_transcript("turnEnd", thread_id=thread_id, is_own=own)
            elif method == "thread/tokenUsage/updated" and own:
                self._record_usage(params)
            elif method == "account/rateLimits/updated":
                journal.kv_set(
                    "rate_limits",
                    json.dumps(params.get("rateLimits") or {}, ensure_ascii=False),
                )
                journal.kv_set("rate_limits_ts", str(time.time()))
            elif method == "mcpServer/startupStatus/updated":
                self._mcp_status(params)
            elif method == "warning":
                journal.log_event("codex_warning", {"message": str(params.get("message"))[:400]})
            elif method == "turn/started":
                journal.log_transcript("turnStart", thread_id=thread_id, is_own=own)
            elif method == "item/reasoning/summaryTextDelta" and own:
                item_id = params.get("itemId") or ""
                index = params.get("summaryIndex", 0)
                chunk = params.get("delta") or ""
                if item_id and chunk:
                    parts = self._reasoning.setdefault(item_id, {})
                    parts[index] = parts.get(index, "") + chunk
                    journal.kv_set(
                        "live_reasoning",
                        "\n".join(parts[k] for k in sorted(parts))[-1200:],
                    )
            elif method == "item/agentMessage/delta" and own:
                # Поток ответа: копим текст, панель показывает его по мере
                # появления. Без этого пауза в минуту выглядит как зависание.
                chunk = params.get("delta") or params.get("text") or ""
                if chunk:
                    journal.kv_set(
                        "live_text", (journal.kv_get("live_text", "") or "") + chunk
                    )
                    journal.kv_set("live_activity", "")
            elif method == "item/started" and own:
                item = params.get("item") or {}
                kind, title, _ = self._describe(item)
                label = ACTIVITY.get(kind)
                # userMessage и прочая служебная мелочь — не занятие агента.
                if label:
                    journal.kv_set(
                        "live_activity", label + (f": {title[:80]}" if title else "")
                    )
            elif method == "item/completed":
                item = params.get("item") or {}
                kind, title, body = self._describe(item)
                if kind == "reasoning":
                    parts = self._reasoning.pop(item.get("id") or "", {})
                    full = "\n".join(parts[k] for k in sorted(parts)).strip()
                    # Собранный текст содержательнее заголовков, но если
                    # поток не пришёл — оставляем что есть.
                    if len(full) > len(body):
                        body = full
                    journal.kv_set("live_reasoning", "")
                journal.log_transcript(
                    kind,
                    title=title,
                    body=body,
                    payload=item,
                    thread_id=thread_id,
                    turn_id=params.get("turnId") or "",
                    is_own=own,
                )
                if kind == "agentMessage":
                    journal.log_event(
                        "agent_message" if own else "subagent_message",
                        {"text": body[:4000]},
                    )
                    self.last_activity = time.time()
                elif kind == "error":
                    journal.log_event("item_error", {"message": body[:500]})
            elif method == "error":
                detail = (params.get("error") or {}).get("message", str(params))
                journal.log_event("turn_error", {"error": str(detail)[:500]})
                journal.log_transcript("error", body=str(detail)[:2000], thread_id=thread_id)
                self.bot.send(f"⚠️ Ошибка хода агента:\n{str(detail)[:500]}", keyboard=False)

    # --- главный цикл ------------------------------------------------------

    def restart_codex(self, reason: str) -> None:
        """Перезапуск с уборкой за старым процессом.

        Без stop() читатели старого процесса продолжали крутиться на мёртвых
        трубах, а сам он не reap-ался.
        """
        journal.log_event("codex_restart", {"reason": reason})
        try:
            self.codex.stop()
        except Exception as exc:  # noqa: BLE001
            journal.log_event("codex_stop_failed", {"error": str(exc)[:200]})
        self.start_codex()
        # Недоигранный ход исчезает молча — агент должен об этом узнать.
        journal.enqueue_message(
            "Процесс Codex перезапускался, твой последний ход мог оборваться. "
            "Сверь состояние: портфель, активные заявки, наблюдатели.",
            source="system",
        )

    def run(self) -> None:
        journal.log_event("supervisor_start", {})
        self.bot.set_commands()
        self.start_codex()
        self.reset_live_state()
        self.bot.send("▶️ Супервизор запущен, сессия агента активна.")

        poller = threading.Thread(target=self._poll_telegram, daemon=True)
        poller.start()
        self.watcher = Watcher(
            on_fire=self._watches_fired,
            market_open=market_open,
            client_factory=reconcile.client,
        )
        self.watcher.start()

        while self.running:
            try:
                if not self.codex.alive():
                    self.bot.send("⚠️ Процесс Codex завершился, перезапускаю.", keyboard=False)
                    self.restart_codex("процесс завершился")

                self.tick_events()
                self.tick_stuck_turn()
                self.tick_reconcile()
                # Сообщения (в том числе сработавшие наблюдатели) идут первыми:
                # это события, а не расписание.
                self.tick_inbox()
                if not self.tick_halt():
                    self.tick_wakeup()
                    self.tick_report()
                    self.tick_regular()
                    self.tick_deadman()
            except Exception as exc:  # noqa: BLE001 — цикл не должен падать
                journal.log_event("supervisor_error", {"error": repr(exc)[:500]})
            time.sleep(5)

        if self.watcher:
            self.watcher.stop()
        self.codex.stop()
        journal.log_event("supervisor_stop", {})

    @staticmethod
    def _watches_fired(texts: list[str]) -> None:
        """Срабатывание кладётся в durable-очередь, а не доставляется сразу.

        Поток наблюдателя не должен зависеть от состояния канала: если он
        лежит, событие подождёт в базе и уйдёт позже.
        """
        for text in texts:
            journal.enqueue_message(text, source="watch")

    def _poll_telegram(self) -> None:
        """Long-polling с отступом при отказах.

        Bot._api превращает любую сетевую ошибку в обычный ответ, а poll
        быстро возвращает ноль. Без задержки при пустом токене или быстром
        отказе поток крутился бы вхолостую на полной скорости.
        """
        backoff = 5.0
        while self.running:
            try:
                handled = self.bot.poll(timeout=25)
                backoff = 5.0
                if handled == 0 and not self.bot.token:
                    time.sleep(60)
            except Exception as exc:  # noqa: BLE001
                journal.log_event("telegram_poll_error", {"error": repr(exc)[:300]})
                time.sleep(backoff)
                backoff = min(backoff * 2, 300.0)

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
