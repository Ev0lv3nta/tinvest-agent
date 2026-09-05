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
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Сколько дней истории брокер отдаёт за один запрос по каждому интервалу.
CHUNK_DAYS = {
    "CANDLE_INTERVAL_1_MIN": 1,
    "CANDLE_INTERVAL_5_MIN": 1,
    "CANDLE_INTERVAL_15_MIN": 7,
    "CANDLE_INTERVAL_30_MIN": 14,
    "CANDLE_INTERVAL_HOUR": 30,
    "CANDLE_INTERVAL_DAY": 365,
}

HEADER = ["time", "open", "high", "low", "close", "volume", "complete"]


def fetch(client, instrument_id: str, interval: str, days: int, out: Path) -> dict:
    """Скачать историю окнами и записать CSV. Возвращает сводку, не серию."""
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
        "assumption": "available_at = конец бара; историческая доступность не проверена",
    }
