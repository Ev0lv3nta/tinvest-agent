#!/usr/bin/env python3
"""Состояние стенда одной командой: python3 deploy/status.py"""

import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gateway import config

MSK = ZoneInfo("Europe/Moscow")


def main() -> None:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row

    def one(query, *args):
        return conn.execute(query, args).fetchone()

    print(f"состояние агента : {one('SELECT value FROM kv WHERE key=?', 'agent_state')[0]}")
    halted = one("SELECT value FROM kv WHERE key=?", "halted")
    print(f"остановлен       : {halted[0] if halted and halted[0] else 'нет'}")

    snapshot = one("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1")
    if snapshot:
        delta = snapshot["total"] - config.STARTING_CAPITAL
        print(f"портфель         : {snapshot['total']:,.0f} ₽ ({delta:+,.0f})".replace(",", " "))

    wake = one("SELECT * FROM wakeups WHERE fired_ts IS NULL AND cancelled=0 ORDER BY due_ts LIMIT 1")
    if wake:
        when = datetime.fromtimestamp(wake["due_ts"], MSK).strftime("%d.%m %H:%M")
        print(f"следующий подъём : {when} — {wake['reason'][:70]}")
    else:
        print("следующий подъём : не назначен")

    last = one("SELECT tool, ts FROM tool_calls ORDER BY id DESC LIMIT 1")
    if last:
        mins = (time.time() - last["ts"]) / 60
        print(f"последний вызов  : {last['tool']} ({mins:.0f} мин назад)")

    for label, query in (
        ("вызовов за сутки ", "SELECT COUNT(*) FROM tool_calls WHERE ts > ?"),
        ("сделок           ", "SELECT COUNT(*) FROM orders WHERE ts > ?"),
        ("отчётов субагентов", "SELECT COUNT(*) FROM events WHERE kind='subagent_message' AND ts > ?"),
    ):
        print(f"{label}: {conn.execute(query, (time.time() - 86400,)).fetchone()[0]}")

    print("\nпоследние события:")
    for row in conn.execute("SELECT kind, ts FROM events ORDER BY id DESC LIMIT 6"):
        when = datetime.fromtimestamp(row["ts"], MSK).strftime("%H:%M:%S")
        print(f"  {when}  {row['kind']}")


if __name__ == "__main__":
    main()
