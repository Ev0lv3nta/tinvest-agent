"""Три детерминированных семейства правил.

Это не найденные преимущества, а контроль. Их задача — ответить на вопрос,
который иначе задать нечему: приносит ли понимание текста событий что-нибудь
сверх правил, для которых модель вообще не нужна. Если не приносит, платить
за рассуждение незачем.

Сигнал считается только по закрытым барам и только по тем, что были
доступны на момент решения. Срез истории отрезается здесь, а не в правиле:
у правила не должно быть технической возможности заглянуть вперёд.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import fmean, pstdev

from .bars import Bar
from .spec import Spec


@dataclass(frozen=True)
class Signal:
    key: str          # идентичность идеи: одна идея — один вход
    version: str
    symbol: str
    at: datetime      # момент решения (закрытие сигнального бара)
    reference: float  # цена, от которой считались уровни
    stop: float
    target: float
    max_hold_bars: int
    risk_fraction: float
    rank: float       # сила сигнала: чем больше, тем раньше в очереди


def true_range(bar: Bar, previous: Bar | None) -> float:
    if previous is None:
        return bar.high - bar.low
    return max(
        bar.high - bar.low,
        abs(bar.high - previous.close),
        abs(bar.low - previous.close),
    )


def atr(bars: list[Bar], window: int = 14) -> float:
    if len(bars) < window + 1:
        return 0.0
    tail = bars[-(window + 1):]
    return fmean(true_range(bar, prev) for prev, bar in zip(tail, tail[1:]))


def generate(spec: Spec, history: list[Bar], events: dict | None = None) -> Signal | None:
    """Сигнал по последнему закрытому бару истории, либо ничего.

    `history` — уже отрезанный срез: последний элемент это бар, на закрытии
    которого принимается решение. Вход возможен не раньше следующего бара.
    """
    need = max(spec.trend_window, spec.lookback) + 1
    if len(history) < need:
        return None

    bar = history[-1]
    window = history[-spec.lookback - 1:-1]
    noise = atr(history)
    if noise <= 0 or bar.volume <= 0:
        return None

    trend = fmean(x.close for x in history[-spec.trend_window:])
    average_volume = fmean(x.volume for x in window)
    volume_x = bar.volume / average_volume if average_volume > 0 else 0.0

    identity = bar.end.isoformat()
    if spec.family == "breakout":
        level = max(x.high for x in window)
        fires = bar.close > level and bar.close > trend and volume_x >= spec.volume_multiple
        rank = (bar.close - level) / noise
    elif spec.family == "pullback":
        center = fmean(x.close for x in window)
        spread = pstdev(x.close for x in window)
        z = (bar.close - center) / spread if spread > 0 else 0.0
        # Откат внутри растущего режима, а не покупка всего, что падает.
        fires = z <= -spec.z_entry and bar.close > trend
        rank = -z
    else:
        event = _known_event(events, bar)
        if event is None:
            return None
        fires = (
            bar.close > history[-2].close
            and bar.close > bar.open
            and volume_x >= spec.volume_multiple
        )
        rank = (bar.close - history[-2].close) / noise
        identity = event

    stop = bar.close - spec.stop_atr * noise
    target = bar.close + spec.target_atr * noise
    if not fires or stop <= 0:
        return None
    return Signal(
        key=f"{spec.version}:{bar.symbol}:{identity}",
        version=spec.version,
        symbol=bar.symbol,
        at=bar.end,
        reference=bar.close,
        stop=stop,
        target=target,
        max_hold_bars=spec.max_hold_bars,
        risk_fraction=spec.risk_fraction,
        rank=rank,
    )


def _known_event(events: dict | None, bar: Bar) -> str | None:
    """Событие, которое было известно ДО начала сигнального бара.

    Не «опубликовано в этот день», а «получено нашим сборщиком до момента,
    с которого начал считаться бар». Опубликованный задним числом факт не
    становится известным в момент, указанный внутри текста.
    """
    if not events:
        return None
    known = [
        item
        for item in events.get(bar.symbol, [])
        if item["observed_at"] <= bar.start and item["valid_until"] > bar.start
        and item.get("polarity") == 1 and not item.get("ambiguous")
    ]
    if not known:
        return None
    return max(known, key=lambda item: (item["observed_at"], item["id"]))["id"]
