"""Синтетические ряды: отрицательный контроль.

На случайном блуждании преимущества нет по построению. Если оценка находит
его здесь, сломана оценка, а не рынок. Поэтому демонстрация обязана
заканчиваться отказом.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .bars import Bar


def random_walk(symbols: int = 4, bars: int = 1500, seed: int = 29,
                start_price: float = 100.0, step_minutes: int = 60) -> list[Bar]:
    rng = random.Random(seed)
    begin = datetime(2024, 1, 3, 7, 0, tzinfo=timezone.utc)
    step = timedelta(minutes=step_minutes)
    out: list[Bar] = []
    for index in range(symbols):
        symbol = f"SYN{index + 1}"
        price = start_price * (1 + index / 10)
        for i in range(bars):
            drift = rng.gauss(0, 0.004)
            open_price = price
            close = max(price * (1 + drift), 0.01)
            high = max(open_price, close) * (1 + abs(rng.gauss(0, 0.002)))
            low = min(open_price, close) * (1 - abs(rng.gauss(0, 0.002)))
            moment = begin + step * i
            out.append(
                Bar(symbol, moment, moment + step, open_price, high, low, close,
                    int(abs(rng.gauss(10_000, 3_000))) + 1, True)
            )
            price = close
    return out
