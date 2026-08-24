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
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gateway import config

WORKDIR = Path("/home/agent/work")
# Архив кладётся ВНЕ рабочей директории. Иначе агент его находит: в прошлый
# раз он так и сделал — восстановил заметки из архива и записал процедуру
# восстановления в lessons.md, то есть чистого листа не получилось.
ARCHIVE_DIR = Path("/home/agent/archive")
SERVICE = "tinvest-agent"

# Что стирается: всё, что описывает текущий разговор и наблюдения за ним.
WIPE = ["transcript", "tool_calls", "events", "wakeups", "messages", "snapshots"]
# Что остаётся: сделки — история счёта, её терять нельзя.
KEEP = ["orders"]
# Ключи состояния, которые надо сохранить: сдвиг Telegram, иначе бот
# заново обработает старые сообщения.
KEEP_KEYS = ["tg_offset"]


def stop_service() -> bool:
    """Сброс на живом супервизоре бессмыслен: он держит thread_id в памяти
    и продолжит старый разговор после очистки ключей."""
    result = subprocess.run(
        ["systemctl", "stop", SERVICE], capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        print(f"остановлен {SERVICE}")
        return True
    print(f"не удалось остановить {SERVICE}: {result.stderr.strip()[:200]}")
    return False


def start_service() -> None:
    result = subprocess.run(
        ["systemctl", "start", SERVICE], capture_output=True, text=True, check=False
    )
    print(
        f"{SERVICE} запущен" if result.returncode == 0
        else f"{SERVICE} НЕ запущен: {result.stderr.strip()[:200]}"
    )


def backup_database(path: Path, target: Path) -> None:
    """Копия через backup API, а не shutil.copy2.

    База в режиме WAL: обычное копирование файла берёт его без журнала
    записи, и в копии не хватает последних транзакций — вплоть до
    отсутствующих таблиц.
    """
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with sqlite3.connect(target) as destination:
            source.backup(destination)
    finally:
        source.close()
    # Проверяем, что копией можно пользоваться.
    check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"копия {target} повреждена")
        check.execute("SELECT COUNT(*) FROM orders").fetchone()
    finally:
        check.close()


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

    conn.close()
    was_running = stop_service()

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    backup = ARCHIVE_DIR / f"agent.{stamp}.db"
    backup_database(path, backup)
    print(f"\nкопия базы: {backup} (проверена)")

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
        archive = ARCHIVE_DIR / f"notes-{stamp}"
        shutil.make_archive(str(archive), "gztar", root_dir=WORKDIR, base_dir="notes")
        print(f"архив заметок: {archive}.tar.gz")
        for item in notes_files:
            item.unlink()
        (WORKDIR / "notes" / "reports").mkdir(parents=True, exist_ok=True)
        print(f"заметки очищены: {len(notes_files)} файлов")

    # Данные тоже относятся к прежней картине мира.
    data_dir = Path(config.DATA_DIR)
    if data_dir.exists():
        removed = 0
        for item in data_dir.iterdir():
            if item.is_file():
                item.unlink()
                removed += 1
        print(f"кеш котировок очищен: {removed} файлов")

    print("журнал очищен, thread_id сброшен — при запуске начнётся новая сессия")
    if was_running:
        start_service()
    else:
        print(f"запусти вручную: systemctl start {SERVICE}")


if __name__ == "__main__":
    main()
