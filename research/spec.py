"""Спецификация гипотезы: то, что проверяется и что нельзя поправить потом.

Спецификация неизменяема и имеет версию — хеш от собственных полей. Это
единственный способ отличить «правило работало» от «правило подкрутили,
пока не заработало»: изменение параметра даёт другую версию и другую
попытку, а не улучшение результата прежней.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field

FAMILIES = ("breakout", "pullback", "event_drift")


@dataclass(frozen=True)
class Spec:
    family: str
    lookback: int = 20          # окно уровня/статистики, в барах
    trend_window: int = 50      # фильтр режима: закрытие выше средней
    volume_multiple: float = 1.5  # объём бара против среднего за lookback
    z_entry: float = 1.5        # для отката: насколько ниже среднего
    stop_atr: float = 1.5       # стоп в единицах ATR
    target_atr: float = 3.0     # цель в единицах ATR
    max_hold_bars: int = 24     # срок жизни идеи
    risk_fraction: float = 0.01  # доля капитала на сделку
    note: str = ""

    def __post_init__(self) -> None:
        if self.family not in FAMILIES:
            raise ValueError(f"неизвестное семейство {self.family!r}; есть {FAMILIES}")
        numbers = (
            self.volume_multiple, self.z_entry, self.stop_atr,
            self.target_atr, self.risk_fraction,
        )
        if not all(math.isfinite(x) for x in numbers):
            raise ValueError("параметры должны быть конечными числами")
        if self.lookback < 5 or self.trend_window < 5:
            raise ValueError("окна короче пяти баров ничего не описывают")
        if self.stop_atr <= 0 or self.target_atr <= 0:
            raise ValueError("стоп и цель задаются положительным числом ATR")
        if not 0 < self.risk_fraction <= 0.05:
            raise ValueError("доля риска вне разумных границ (0, 0.05]")
        if self.max_hold_bars < 1:
            raise ValueError("срок жизни идеи меньше бара — это не идея")

    @property
    def version(self) -> str:
        """Хеш от полей. Одинаковые параметры дают одну версию, разные — разные."""
        payload = {k: v for k, v in asdict(self).items() if k != "note"}
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    @property
    def name(self) -> str:
        return f"{self.family}:{self.version}"

    def to_dict(self) -> dict:
        return {**asdict(self), "version": self.version}


def grid(family: str, **options) -> list[Spec]:
    """Все сочетания перечисленных параметров.

    Перебор объявляется заранее и целиком: число попыток входит в поправку
    на множественность, поэтому «попробуем ещё вот это» после просмотра
    результатов — не бесплатное действие.
    """
    keys = sorted(options)
    specs: list[Spec] = []

    def walk(index: int, chosen: dict) -> None:
        if index == len(keys):
            specs.append(Spec(family=family, **chosen))
            return
        key = keys[index]
        for value in options[key]:
            walk(index + 1, {**chosen, key: value})

    walk(0, {})
    return specs
