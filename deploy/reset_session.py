#!/usr/bin/env python3
"""Начать сессию агента с нуля.

Сбрасывает разговор, журнал наблюдений и заметки агента. Сохраняется только
история сделок: это состояние счёта, а не сессии. Перед очисткой делается
копия базы и архив заметок.

Заметки стираются намеренно. Они написаны под прежними инструкциями и якорят:
агент, прочитавший вчерашний план по трём бумагам, продолжит эту линию вместо
того, чтобы составить собственную картину заново. Для чистого прогона нужен
чистый лист.

    python3 deploy/reset_session.py            # показать, что будет сделано
    python3 deploy/reset_session.py --confirm   # выполнить
"""

import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gateway import config

WORKDIR = Path("/home/agent/work")

# Что стирается: всё, что описывает текущий разговор и наблюдения за ним.
WIPE = ["transcript", "tool_calls", "events", "wakeups", "messages", "snapshots"]
# Что остаётся: сделки — история счёта, её терять нельзя.
KEEP = ["orders"]
# Ключи состояния, которые надо сохранить: сдвиг Telegram, иначе бот
# заново обработает старые сообщения.
KEEP_KEYS = ["tg_offset"]


def main() -> None:
    confirm = "--confirm" in sys.argv
    path = Path(config.DB_PATH)
    if not path.exists():
        print(f"базы нет: {path}")
        return

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    counts = {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in WIPE + KEEP
    }

    print("будет стёрто:")
    for table in WIPE:
        print(f"   {table:12} {counts[table]:6} записей")
    print("останется:")
    for table in KEEP:
        print(f"   {table:12} {counts[table]:6} записей")
    notes_files = sorted(WORKDIR.glob("notes/**/*")) if WORKDIR.exists() else []
    notes_files = [f for f in notes_files if f.is_file()]
    print(f"   заметки       {len(notes_files):6} файлов в notes/")

    if not confirm:
        print("\nэто предпросмотр; для выполнения добавь --confirm")
        return

    backup = path.with_name(
        f"{path.stem}.{datetime.now():%Y%m%d-%H%M%S}.bak{path.suffix}"
    )
    conn.close()
    shutil.copy2(path, backup)
    print(f"\nкопия базы: {backup}")

    conn = sqlite3.connect(path)
    for table in WIPE:
        conn.execute(f"DELETE FROM {table}")
    placeholders = ",".join("?" * len(KEEP_KEYS))
    conn.execute(f"DELETE FROM kv WHERE key NOT IN ({placeholders})", KEEP_KEYS)
    conn.commit()
    # VACUUM не выполняется внутри транзакции, поэтому только после commit
    # и с отключённым автоматическим её открытием.
    conn.isolation_level = None
    conn.execute("VACUUM")
    conn.close()

    if notes_files:
        archive = WORKDIR / f"notes-archive-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.make_archive(str(archive), "gztar", root_dir=WORKDIR, base_dir="notes")
        print(f"архив заметок: {archive}.tar.gz")
        for item in notes_files:
            item.unlink()
        (WORKDIR / "notes" / "reports").mkdir(parents=True, exist_ok=True)
        print(f"заметки очищены: {len(notes_files)} файлов")

    print("журнал очищен, thread_id сброшен — при запуске начнётся новая сессия")


if __name__ == "__main__":
    main()
