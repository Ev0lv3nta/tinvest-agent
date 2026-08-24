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


class AppServerError(RuntimeError):
    pass


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

    # --- жизненный цикл ----------------------------------------------------

    def start(self) -> None:
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
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def alive(self) -> bool:
        return bool(self.process) and self.process.poll() is None

    # --- транспорт ---------------------------------------------------------

    def _read_loop(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue

            if "id" in message and ("result" in message or "error" in message):
                waiter = self._pending.pop(message["id"], None)
                if waiter is not None:
                    waiter.put(message)
            else:
                self._track(message)
                self.events.put(message)

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
            self.turn_id = params.get("turnId") or (params.get("turn") or {}).get("id", "")
            self.busy = True
        elif method == "turn/completed":
            self.busy = False
            self.turn_id = ""

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
        turn = result.get("turn") or {}
        self.turn_id = turn.get("id") or result.get("turnId", "")
        self.busy = True
        return self.turn_id

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

    def deliver(self, text: str) -> str:
        """Отдать текст агенту: в текущий ход или новым ходом."""
        if self.busy and self.steer(text):
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
