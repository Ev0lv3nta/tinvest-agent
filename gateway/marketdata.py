"""Рыночные данные: таблицы на диск, в контекст — сводка и путь.

Причина существования модуля видна в журнале первого прогона. За день агент
255 раз вызвал `candles`; один вызов минутных свечей за день — это около 980
строк OHLCV, то есть примерно 38 тысяч токенов сырого JSON в сессии. Свечи
съели 94% всего объёма инструментальных ответов и дважды за день вызвали
сжатие контекста.

При этом под агентом полноценная машина с Python. Считать по таблице должен
его код, а не он сам глазами. Поэтому серия пишется в CSV, а возвращается
сводка с посчитанными признаками и путь к файлу.

Второе следствие того же журнала: из 255 вызовов уникальных сочетаний
(бумага, интервал, глубина) было 76 — то есть 70% запросов повторяли уже
полученное. Поэтому файл переиспользуется, пока не устарел; свеча не может
измениться чаще, чем раз в свой интервал.
"""

from __future__ import annotations

import csv
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import config

MSK = timezone(timedelta(hours=3))

# Сколько минут в одной свече. Отсюда же берётся время жизни файла:
# перезапрашивать чаще, чем закрывается свеча, бессмысленно.
INTERVAL_MINUTES = {
    "CANDLE_INTERVAL_1_MIN": 1,
    "CANDLE_INTERVAL_2_MIN": 2,
    "CANDLE_INTERVAL_3_MIN": 3,
    "CANDLE_INTERVAL_5_MIN": 5,
    "CANDLE_INTERVAL_10_MIN": 10,
    "CANDLE_INTERVAL_15_MIN": 15,
    "CANDLE_INTERVAL_30_MIN": 30,
    "CANDLE_INTERVAL_HOUR": 60,
    "CANDLE_INTERVAL_2_HOUR": 120,
    "CANDLE_INTERVAL_4_HOUR": 240,
    "CANDLE_INTERVAL_DAY": 1440,
    "CANDLE_INTERVAL_WEEK": 7 * 1440,
    "CANDLE_INTERVAL_MONTH": 30 * 1440,
}

CSV_HEADER = ["time", "open", "high", "low", "close", "volume"]

# Открытие основной сессии. Диапазон первых тридцати минут — опорный уровень
# для внутридневной работы, поэтому считается всегда.
OPENING_RANGE_MINUTES = 30
MAIN_SESSION_START = (10, 0)


def data_dir() -> Path:
    path = config.DATA_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:40] or "x"


def _short_interval(interval: str) -> str:
    return interval.replace("CANDLE_INTERVAL_", "").lower()


def cache_ttl(interval: str) -> float:
    """Файл живёт до закрытия текущей свечи, но не дольше часа.

    Верхняя граница нужна для дневных и недельных свечей: они меняются редко,
    но не настолько, чтобы держать сутки устаревший файл.
    """
    minutes = INTERVAL_MINUTES.get(interval, 5)
    return min(minutes, 60) * 60.0


# --- индикаторы -----------------------------------------------------------


def _num(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def true_range(bar: dict, previous: Optional[dict]) -> float:
    high, low = _num(bar.get("high")), _num(bar.get("low"))
    if previous is None:
        return high - low
    close = _num(previous.get("close"))
    return max(high - low, abs(high - close), abs(low - close))


def atr(bars: list[dict], period: int = 14) -> float:
    """Средний истинный диапазон — мера шума в единицах цены.

    Нужен, чтобы отличить стоп «за уровнем» от стопа «внутри шума»: во втором
    случае позицию выбьет случайным движением независимо от правоты тезиса.
    """
    if len(bars) < 2:
        return 0.0
    window = bars[-(period + 1):]
    ranges = [true_range(bar, window[i - 1] if i else None) for i, bar in enumerate(window)]
    ranges = ranges[1:] or ranges
    return sum(ranges) / len(ranges) if ranges else 0.0


def vwap(bars: list[dict]) -> float:
    volume = sum(_num(b.get("volume")) for b in bars)
    if volume <= 0:
        return 0.0
    weighted = sum(
        (_num(b.get("high")) + _num(b.get("low")) + _num(b.get("close"))) / 3
        * _num(b.get("volume"))
        for b in bars
    )
    return weighted / volume


def _msk(iso: str) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MSK)
    except ValueError:
        return None


def opening_range(bars: list[dict]) -> Optional[dict]:
    """Диапазон первых 30 минут основной сессии последнего торгового дня."""
    stamped = [(b, _msk(b.get("time", ""))) for b in bars]
    stamped = [(b, t) for b, t in stamped if t is not None]
    if not stamped:
        return None
    day = stamped[-1][1].date()
    start = datetime(day.year, day.month, day.day, *MAIN_SESSION_START, tzinfo=MSK)
    end = start + timedelta(minutes=OPENING_RANGE_MINUTES)
    window = [b for b, t in stamped if start <= t < end]
    if not window:
        return None
    return {
        "hi": round(max(_num(b.get("high")) for b in window), 4),
        "lo": round(min(_num(b.get("low")) for b in window), 4),
        "window": f"{start:%H:%M}-{end:%H:%M}",
        "bars": len(window),
    }


# --- файл и сводка --------------------------------------------------------


def csv_path(name: str, interval: str, days: int) -> Path:
    return data_dir() / f"{_slug(name)}_{_short_interval(interval)}_{days}d.csv"


def write_csv(bars: list[dict], path: Path) -> None:
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for bar in bars:
            writer.writerow([bar.get(key) for key in CSV_HEADER])
    tmp.replace(path)


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    out = []
    for row in rows:
        out.append(
            {
                "time": row.get("time", ""),
                "open": _num(row.get("open")),
                "high": _num(row.get("high")),
                "low": _num(row.get("low")),
                "close": _num(row.get("close")),
                "volume": int(_num(row.get("volume"))),
            }
        )
    return out


def summarize(bars: list[dict], path: Path, interval: str, tail: int = 0) -> dict:
    """Компактный портрет серии: несколько сотен байт вместо сотни килобайт."""
    if not bars:
        return {
            "file": str(path),
            "bars": 0,
            "note": "свечей нет — вероятно, вне торгов или бумага не торговалась",
        }

    highs = [_num(b.get("high")) for b in bars]
    lows = [_num(b.get("low")) for b in bars]
    volumes = [_num(b.get("volume")) for b in bars]
    first_open = _num(bars[0].get("open"))
    last_close = _num(bars[-1].get("close"))
    high, low = max(highs), min(lows)
    span = high - low
    average_volume = sum(volumes) / len(volumes) if volumes else 0.0
    noise = atr(bars, 14)

    summary: dict[str, Any] = {
        "file": str(path),
        "columns": ",".join(CSV_HEADER),
        "bars": len(bars),
        "from": bars[0].get("time"),
        "to": bars[-1].get("time"),
        "open": round(first_open, 4),
        "high": round(high, 4),
        "low": round(low, 4),
        "last": round(last_close, 4),
        "change_pct": round((last_close / first_open - 1) * 100, 2) if first_open else 0.0,
        "range_pct": round(span / last_close * 100, 2) if last_close else 0.0,
        "vwap": round(vwap(bars), 4),
        "atr14": round(noise, 4),
        "atr14_pct": round(noise / last_close * 100, 2) if last_close else 0.0,
        "volume_total": int(sum(volumes)),
        "last_bar_volume_x_avg": round(volumes[-1] / average_volume, 2)
        if average_volume
        else 0.0,
        # 0 — у минимума серии, 1 — у максимума. Дешёвый ответ на вопрос
        # «мы у поддержки или у сопротивления».
        "position_in_range": round((last_close - low) / span, 2) if span else 0.5,
    }

    window = opening_range(bars)
    if window:
        summary["opening_range"] = window

    if tail > 0:
        summary["tail"] = [
            f"{b.get('time')};{b.get('open')};{b.get('high')};"
            f"{b.get('low')};{b.get('close')};{b.get('volume')}"
            for b in bars[-min(tail, 50):]
        ]

    return summary


def candles(
    client,
    instrument_id: str,
    interval: str,
    days: int,
    name: str = "",
    tail: int = 0,
    refresh: bool = False,
) -> dict:
    """Серия — в файл, наружу — сводка. Повторный запрос берёт файл с диска."""
    if interval not in INTERVAL_MINUTES:
        known = ", ".join(sorted(INTERVAL_MINUTES))
        raise ValueError(f"неизвестный интервал {interval!r}. Допустимые: {known}")

    path = csv_path(name or instrument_id, interval, days)
    fresh = False
    if not refresh and path.exists():
        age = time.time() - path.stat().st_mtime
        if age < cache_ttl(interval):
            fresh = True

    if fresh:
        bars = read_csv(path)
        summary = summarize(bars, path, interval, tail)
        summary["cached"] = True
        summary["age_sec"] = int(time.time() - path.stat().st_mtime)
        return summary

    bars = client.candles(instrument_id, interval, days)
    write_csv(bars, path)
    summary = summarize(bars, path, interval, tail)
    summary["cached"] = False
    return summary


# --- справочник инструментов ---------------------------------------------

UNIVERSE_FILE = "universe.json"


def _universe_path() -> Path:
    return data_dir() / UNIVERSE_FILE


def load_universe() -> dict:
    try:
        return json.loads(_universe_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_universe(universe: dict) -> None:
    path = _universe_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(universe, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def remember_instrument(item: dict) -> None:
    """Тикер → uid/figi/лот. За первый прогон find_instrument звался 90 раз,
    почти всегда ради уже известного UID."""
    ticker = (item.get("ticker") or "").upper()
    uid = item.get("instrument_id") or item.get("uid") or ""
    if not ticker or not uid:
        return
    universe = load_universe()
    universe[ticker] = {
        "uid": uid,
        "figi": item.get("figi", ""),
        "lot": int(item.get("lot") or 1),
        "name": item.get("name", ""),
        "ts": time.time(),
    }
    save_universe(universe)


def resolve(ticker: str) -> Optional[dict]:
    entry = load_universe().get((ticker or "").upper())
    if not entry:
        return None
    # Справочник обновляется раз в сутки: лот и листинг меняются редко,
    # но не никогда.
    if time.time() - float(entry.get("ts") or 0) > 24 * 3600:
        return None
    return entry


def reference(client, ticker: str, uid: str) -> dict:
    """Опорные величины по дневным свечам: вчерашнее закрытие, средний объём,
    дневной ATR. Считаются раз в сутки и кладутся в справочник.

    Дальше срез рынка по любому числу бумаг стоит один батч-запрос цен и
    немного арифметики — вместо трёх вызовов на каждую бумагу.
    """
    universe = load_universe()
    entry = universe.get(ticker.upper()) or {}
    ref = entry.get("ref") or {}
    if ref and time.time() - float(ref.get("ts") or 0) < 12 * 3600:
        return ref

    summary = candles(client, uid, "CANDLE_INTERVAL_DAY", 40, name=ticker)
    bars = read_csv(Path(summary["file"])) if summary.get("bars") else []
    # Последний бар — сегодняшний и ещё формируется, опора берётся до него.
    history = bars[:-1] if len(bars) > 1 else bars
    if not history:
        return {}
    volumes = [_num(b.get("volume")) for b in history[-20:]]
    ref = {
        "prev_close": round(_num(history[-1].get("close")), 4),
        "avg_volume": round(sum(volumes) / len(volumes), 1) if volumes else 0.0,
        "atr_day": round(atr(history, 14), 4),
        "ts": time.time(),
    }
    entry.setdefault("uid", uid)
    entry["ref"] = ref
    universe[ticker.upper()] = entry
    save_universe(universe)
    return ref


def ticker_for(uid: str) -> str:
    for ticker, entry in load_universe().items():
        if entry.get("uid") == uid:
            return ticker
    return ""


# --- страховка транспорта -------------------------------------------------


def _uniform_rows(payload: Any) -> Optional[list[dict]]:
    """Список одинаковых словарей — это таблица, её пишем как CSV."""
    if not isinstance(payload, list) or len(payload) < 2:
        return None
    if not all(isinstance(item, dict) for item in payload):
        return None
    keys = list(payload[0].keys())
    if not keys or any(list(item.keys()) != keys for item in payload):
        return None
    return payload


def spill(tool: str, payload: Any, text: str) -> dict:
    """Ответ великоват для контекста — выгружаем на диск, отдаём описание.

    Сюда попадает всё, что превысило потолок: и таблицы, для которых не
    сделали отдельной сводки, и разросшиеся ответы существующих инструментов.
    Смысл в том, что объёмные данные физически не могут попасть в сессию
    текстом — даже по недосмотру.
    """
    stamp = datetime.now(MSK).strftime("%Y%m%d-%H%M%S")
    rows = _uniform_rows(payload)
    suffix = "csv" if rows else "json"
    path = data_dir() / f"{_slug(tool)}_{stamp}.{suffix}"

    if rows:
        keys = list(rows[0].keys())
        tmp = path.with_suffix(f".{suffix}.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
        tmp.replace(path)
        shape: dict[str, Any] = {"rows": len(rows), "columns": ",".join(keys)}
    else:
        path.write_text(text, encoding="utf-8")
        shape = {"bytes": len(text.encode())}

    return {
        "spilled": True,
        "file": str(path),
        "tool": tool,
        **shape,
        "note": (
            "Ответ слишком объёмный для контекста и выгружен в файл. "
            "Читай его кодом (pandas, csv, json), а не целиком в сессию."
        ),
        "preview": text[:400],
    }
