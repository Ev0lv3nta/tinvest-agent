#!/usr/bin/env python3
"""Снимок журнала в каталог, недоступный агенту.

Шлюз, Codex и база работают от пользователя `agent`, поэтому утверждение
«агент не может переписать журнал» верно на уровне процесса, но не на уровне
операционной системы. Настоящая граница появится при разделении
пользователей; до тех пор снимок делает подделку обнаружимой — сравнением с
копией, которой агент не владеет.

Запускается таймером от root раз в час.
"""

import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gateway import config

BACKUP_DIR = Path("/var/backups/tinvest-agent")
KEEP = 72  # трое суток почасовых снимков


def main() -> None:
    source_path = Path(config.DB_PATH)
    if not source_path.exists():
        print(f"базы нет: {source_path}")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.chmod(0o700)
    target = BACKUP_DIR / f"agent.{datetime.now():%Y%m%d-%H}.db"

    # Backup API, а не копирование файла: база в режиме WAL, и обычная копия
    # приходит без последних транзакций — вплоть до отсутствующих таблиц.
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        with sqlite3.connect(target) as destination:
            source.backup(destination)
    finally:
        source.close()
    target.chmod(0o600)

    check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit(f"снимок {target} повреждён")
        orders = check.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        check.close()

    snapshots = sorted(BACKUP_DIR.glob("agent.*.db"))
    for stale in snapshots[:-KEEP]:
        stale.unlink()

    print(f"{target}: заявок {orders}, снимков {len(snapshots[-KEEP:])}")


if __name__ == "__main__":
    main()
