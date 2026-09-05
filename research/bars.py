"""Бары: единственный вид данных, по которому здесь считают.

Главное свойство бара — момент, начиная с которого он доступен. Сигнал,
посчитанный по бару, не может быть исполнен раньше, чем этот бар закрылся:
пока он формируется, его максимум, минимум и закрытие ещё изменятся.
Нарушение этого правила и есть самый дешёвый способ нарисовать прибыль в
бэктесте, поэтому доступность здесь не соглашение, а поле в структуре.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class Bar:
    symbol: str
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    complete: bool

    @property
    def available_at(self) -> datetime:
        """Момент, начиная с которого бар можно использовать в решении.

        Незакрытый бар недоступен никогда: у него ещё нет ни максимума, ни
        закрытия. Это не «мы пока не успели прочитать», а «величины ещё не
        существует».
        """
        return self.end

    def sane(self) -> bool:
        prices = (self.open, self.high, self.low, self.close)
        if not all(math.isfinite(p) and p > 0 for p in prices):
            return False
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            return False
        return self.volume >= 0 and self.end > self.start


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def load_csv(path: Path, symbol: str, minutes: int) -> list[Bar]:
    """Прочитать файл, записанный шлюзом: тот же формат, что видит агент.

    Незавершённые и битые бары отбрасываются здесь, а не в стратегии: у
    правила не должно быть возможности случайно посмотреть на то, чего в
    момент решения ещё не было.
    """
    step = timedelta(minutes=minutes)
    bars: list[Bar] = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            raw = row.get("complete")
            start = parse_time(row["time"])
            bar = Bar(
                symbol=symbol,
                start=start,
                end=start + step,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(float(row.get("volume") or 0)),
                complete=str(raw) not in ("False", "false", "0"),
            )
            if bar.complete and bar.sane():
                bars.append(bar)
    bars.sort(key=lambda b: b.start)
    return dedupe(bars)


def dedupe(bars: list[Bar]) -> list[Bar]:
    """Один бар на момент времени. Дубли приходят от повторных выгрузок."""
    seen: dict[datetime, Bar] = {}
    for bar in bars:
        seen[bar.start] = bar
    return [seen[key] for key in sorted(seen)]


def sessions(bars: list[Bar]) -> list[str]:
    """Торговые дни по московскому времени, в порядке следования."""
    days: list[str] = []
    for bar in bars:
        day = bar.start.astimezone(MSK).date().isoformat()
        if not days or days[-1] != day:
            days.append(day)
    return days


def by_symbol(bars: list[Bar]) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    for bar in bars:
        out.setdefault(bar.symbol, []).append(bar)
    for series in out.values():
        series.sort(key=lambda b: b.start)
    return out
