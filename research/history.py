"""Загрузка истории кусками. Один файл на бумагу и интервал.

API отдаёт ограниченный отрезок за запрос, поэтому длинная история берётся
окнами. Незавершённый бар в файл не пишется: он ещё изменится, и запись
превратила бы его в наблюдение, которого не было.

Про доступность честно: скачивая историю сегодня, мы не доказываем, что тот
же ответ существовал в прошлом в этом же виде. Файл помечается допущением
`available_at = конец бара`, и это допущение, а не измерение.
"""

from __future__ import annotations

import csv
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Сколько дней истории брокер отдаёт за один запрос. Границы измерены на
# песочнице 05.09.2026: за ними приходит «The maximum request period for the
# given candle interval has been exceeded». Берём с запасом — при отказе
# теряется всё окно, а лишний запрос стоит доли секунды.
CHUNK_DAYS = {
    "CANDLE_INTERVAL_1_MIN": 1,
    "CANDLE_INTERVAL_2_MIN": 1,
    "CANDLE_INTERVAL_3_MIN": 1,
    "CANDLE_INTERVAL_5_MIN": 1,
    "CANDLE_INTERVAL_10_MIN": 7,
    "CANDLE_INTERVAL_15_MIN": 10,
    "CANDLE_INTERVAL_30_MIN": 14,
    "CANDLE_INTERVAL_HOUR": 60,
    "CANDLE_INTERVAL_2_HOUR": 60,
    "CANDLE_INTERVAL_4_HOUR": 90,
    "CANDLE_INTERVAL_DAY": 1500,
    "CANDLE_INTERVAL_WEEK": 1500,
    "CANDLE_INTERVAL_MONTH": 1500,
}

HEADER = ["time", "open", "high", "low", "close", "volume", "complete"]


def fetch(
    client, instrument_id: str, interval: str, days: int, out: Path,
    max_age: float = 0.0,
) -> dict:
    """Скачать историю окнами и записать CSV. Возвращает сводку, не серию.

    `max_age` — сколько секунд файл считается свежим. Перекачивать полгода
    часовых свечей ради последнего бара незачем.
    """
    if max_age > 0 and out.exists():
        age = time.time() - out.stat().st_mtime
        if age < max_age:
            with out.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            return {
                "file": str(out),
                "bars": len(rows),
                "from": rows[0]["time"] if rows else None,
                "to": rows[-1]["time"] if rows else None,
                "cached": True,
                "age_sec": int(age),
                "assumption": "available_at = конец бара; историческая доступность не проверена",
            }
    chunk = timedelta(days=CHUNK_DAYS.get(interval, 7))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    collected: dict[str, dict] = {}

    cursor = start
    while cursor < end:
        stop = min(cursor + chunk, end)
        for bar in client.candles_between(instrument_id, interval, cursor, stop):
            if bar.get("complete", True):
                collected[str(bar["time"])] = bar
        cursor = stop

    rows = [collected[key] for key in sorted(collected)]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for bar in rows:
            writer.writerow([bar.get(key) for key in HEADER])
    return {
        "file": str(out),
        "bars": len(rows),
        "from": rows[0]["time"] if rows else None,
        "to": rows[-1]["time"] if rows else None,
        "cached": False,
        "assumption": "available_at = конец бара; историческая доступность не проверена",
    }
