"""Клиент codex app-server.

Codex запускается дочерним процессом, общение — построчный JSON-RPC через
stdin/stdout. Веб-сокет в документации помечен экспериментальным, unix-сокет
требует HTTP Upgrade; для единственного локального клиента stdio проще и
надёжнее, а заодно супервизор владеет жизненным циклом процесса.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Any, Optional

from gateway import journal


# Запросы, на которые сервер ждёт решения человека. Человека здесь нет, а
# доступ уже выдан конфигурацией (approval_policy = never, полный доступ к
# файлам), поэтому отказ противоречил бы собственным настройкам стенда и
# оборвал бы ход на середине. Отвечаем согласием и записываем факт: при
# текущей конфигурации такие запросы приходить не должны вовсе.
APPROVAL_REQUESTS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "applyPatchApproval",
    "execCommandApproval",
}
INPUT_REQUESTS = {"item/tool/requestUserInput"}


def _turn_id(payload: dict) -> str:
    """Идентификатор хода. Протокол кладёт его то полем, то внутрь объекта."""
    return (payload.get("turn") or {}).get("id", "") or payload.get("turnId", "")


class AppServerError(RuntimeError):
    pass


class Busy(AppServerError):
    """Ход идёт, а событие не настолько срочное, чтобы в него врезаться."""


class AppServer:
    """Одна живая сессия Codex. Не потокобезопасен для параллельных запросов."""

    def __init__(self, command: list[str], cwd: str, env: dict[str, str]):
        self.command = command
        self.cwd = cwd
        self.env = env
        self.process: Optional[subprocess.Popen] = None
        self.events: queue.Queue[dict] = queue.Queue()

        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None

        self.thread_id: str = ""
        self.turn_id: str = ""
        self.busy: bool = False
        # Идентификаторы завершившихся ходов: короткая память против гонки
        # между ответом на turn/start и событием turn/completed.
        self._completed: list[str] = []
        # Момент последнего входящего события. Отдельно от времени доставки:
        # иначе периодическая доставка «оживляет» зависший ход, и сторожок
        # никогда не срабатывает.
        self.last_inbound: float = time.time()
        self.turn_started: float = 0.0

    # --- жизненный цикл ----------------------------------------------------

    def start(self) -> None:
        try:
            self._start()
        except Exception:
            # Половина запуска хуже, чем его отсутствие: дочерний процесс и
            # потоки-читатели остались бы жить на мёртвом соединении.
            self.stop()
            raise

    def _start(self) -> None:
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()

        self.request(
            "initialize",
            {"clientInfo": {"name": "tinvest-supervisor", "version": "1.0.0"}},
            timeout=60,
        )
        self.notify("initialized", {})

    def stop(self) -> None:
        process, self.process = self.process, None
        if process and process.poll() is None:
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        elif process:
            process.wait(timeout=5)
        self._fail_pending("app-server остановлен")
        for thread in (self._reader, self._stderr_reader):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2)
        self._reader = self._stderr_reader = None
        self.busy = False
        self.turn_id = ""

    def alive(self) -> bool:
        return bool(self.process) and self.process.poll() is None

    # --- транспорт ---------------------------------------------------------

    def _fail_pending(self, reason: str) -> None:
        """Разбудить всех, кто ждёт ответа.

        Без этого смерть процесса заставляла каждый висящий вызов ждать
        полного таймаута вместо немедленной ошибки.
        """
        for request_id in list(self._pending):
            waiter = self._pending.pop(request_id, None)
            if waiter is not None:
                waiter.put({"id": request_id, "error": {"message": reason}})

    def _read_loop(self) -> None:
        process = self.process
        assert process and process.stdout
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue

            self.last_inbound = time.time()
            if "id" in message and ("result" in message or "error" in message):
                waiter = self._pending.pop(message["id"], None)
                if waiter is not None:
                    waiter.put(message)
            elif "id" in message and message.get("method"):
                # Запрос от сервера: подтверждение, запрос ввода и подобное.
                # Молчание подвесило бы ход — отвечаем явным отказом, чтобы
                # агент увидел причину и продолжил сам.
                self._answer_request(message)
            else:
                self._track(message)
                self.events.put(message)
        self._fail_pending("app-server закрыл поток")

    def _answer_request(self, message: dict) -> None:
        """Ответить на запрос сервера. Молчание подвесило бы ход.

        Формы ответов взяты из схемы протокола: у запросов подтверждения это
        `{"decision": ...}`, у запроса ввода — `{"answers": {}}`.
        """
        method = message.get("method", "")
        journal.log_event("appserver_request", {"method": method})

        if method in APPROVAL_REQUESTS:
            payload = {"result": {"decision": "acceptForSession"}}
        elif method in INPUT_REQUESTS:
            # Отвечать некому: пустой ответ честнее выдуманного.
            payload = {"result": {"answers": {}}}
        else:
            payload = {
                "error": {
                    "code": -32601,
                    "message": (
                        f"метод {method} супервизором не поддерживается: "
                        f"агент работает автономно, человека в цикле нет"
                    ),
                }
            }

        with self._lock:
            if not self.alive():
                return
            self.process.stdin.write(
                json.dumps(
                    {"jsonrpc": "2.0", "id": message["id"], **payload}, ensure_ascii=False
                )
                + "\n"
            )
            self.process.stdin.flush()

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            line = line.strip()
            # Логи Codex шумные; в журнал кладём только ошибки.
            if line and ("ERROR" in line or "error" in line.lower()):
                journal.log_event("codex_stderr", {"line": line[:500]})

    def _track(self, message: dict) -> None:
        """Состояние треда обновляется из потока нотификаций.

        Субагенты работают в собственных тредах и присылают свои
        turn/started и turn/completed. Без сверки threadId первый
        завершившийся субагент сбрасывал состояние основного агента.
        """
        method = message.get("method")
        params = message.get("params") or {}
        thread_id = params.get("threadId") or (params.get("thread") or {}).get("id", "")

        if method == "thread/started" and not self.thread_id:
            self.thread_id = thread_id
            return
        if thread_id and self.thread_id and thread_id != self.thread_id:
            return  # событие субагента, состояние основного не трогаем

        if method == "turn/started":
            self.turn_id = _turn_id(params)
            self.busy = True
            self.turn_started = time.time()
        elif method == "turn/completed":
            finished = _turn_id(params)
            if finished:
                # Ход мог завершиться раньше, чем вернулся ответ на его
                # запуск: события приходят отдельным потоком. Помним, что он
                # закончился, иначе start_turn выставит busy на уже мёртвый
                # ход, и супервизор навсегда решит, что агент занят.
                self._completed.append(finished)
                del self._completed[:-50]
            self.busy = False
            self.turn_id = ""
            self.turn_started = 0.0

    def request(self, method: str, params: dict, timeout: int = 120) -> dict:
        if not self.alive():
            raise AppServerError("app-server не запущен")
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            waiter: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
            payload = {"method": method, "id": request_id, "params": params}
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        try:
            message = waiter.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(request_id, None)
            raise AppServerError(f"{method}: нет ответа за {timeout} с") from None
        if "error" in message:
            raise AppServerError(f"{method}: {message['error']}")
        return message.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        if not self.alive():
            return
        with self._lock:
            self.process.stdin.write(
                json.dumps({"method": method, "params": params}, ensure_ascii=False) + "\n"
            )
            self.process.stdin.flush()

    # --- работа с тредом ---------------------------------------------------

    def start_thread(self, cwd: str) -> str:
        result = self.request("thread/start", {"cwd": cwd}, timeout=120)
        thread = result.get("thread") or {}
        self.thread_id = thread.get("id") or result.get("threadId", "")
        if not self.thread_id:
            raise AppServerError(f"не получили threadId: {result}")
        return self.thread_id

    def resume_thread(self, thread_id: str) -> str:
        # excludeTurns требует experimentalApi, а без него запрос отклоняется
        # целиком и сессия теряется при каждом перезапуске.
        result = self.request("thread/resume", {"threadId": thread_id}, timeout=180)
        thread = result.get("thread") or {}
        self.thread_id = thread.get("id") or thread_id
        return self.thread_id

    def start_turn(self, text: str) -> str:
        result = self.request(
            "turn/start",
            {"threadId": self.thread_id, "input": [{"type": "text", "text": text}]},
            timeout=120,
        )
        turn_id = _turn_id(result)
        if turn_id and turn_id in self._completed:
            # Уже закончился. Занятым его объявлять нельзя.
            self.busy = False
            self.turn_id = ""
            return turn_id
        self.turn_id = turn_id
        self.busy = True
        return turn_id

    def steer(self, text: str) -> bool:
        """Доставить сообщение в идущий ход. False — если хода нет."""
        if not self.busy or not self.turn_id:
            return False
        try:
            self.request(
                "turn/steer",
                {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": text}],
                    "expectedTurnId": self.turn_id,
                },
                timeout=60,
            )
            return True
        except AppServerError as exc:
            # Ход мог завершиться между проверкой и вызовом — это не ошибка.
            journal.log_event("steer_failed", {"error": str(exc)[:300]})
            return False

    def deliver(self, text: str, urgent: bool = False) -> str:
        """Отдать текст агенту.

        Врезаться в идущий ход можно только со срочным: сообщение оператора,
        сработавший стоп, ошибка исполнения. Рутина ждёт своей очереди —
        иначе будильник вливается в чужой ход и раздувает его. В разобранном
        прогоне так и вышло: будильник 09:40 попал в ход по жалобе оператора,
        и тот вырос до 144 вызовов инструментов и 17 поисков.
        """
        if self.busy:
            if not urgent:
                raise Busy("агент занят, рутинное событие подождёт")
            if self.steer(text):
                return "steer"
        self.start_turn(text)
        return "turn"

    def interrupt(self) -> None:
        if self.busy and self.turn_id:
            try:
                self.request(
                    "turn/interrupt",
                    {"threadId": self.thread_id, "turnId": self.turn_id},
                    timeout=30,
                )
            except AppServerError:
                pass

    def drain_events(self, limit: int = 200) -> list[dict]:
        collected = []
        for _ in range(limit):
            try:
                collected.append(self.events.get_nowait())
            except queue.Empty:
                break
        return collected
