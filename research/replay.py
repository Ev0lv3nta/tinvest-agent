"""Прогон правила по истории. Грубая модель ликвидности, честная в оценках.

Чего этот прогон не знает: пути цены внутри бара, очереди лимитных заявок,
аукционов, влияния собственной заявки на рынок. Поэтому все спорные случаи
решаются в сторону худшего для нас исхода:

  * сигнал на закрытии бара исполняется по открытию следующего;
  * если в одном баре задеты и стоп, и цель, считается стоп;
  * разрыв через стоп исполняется по фактическому открытию, а не по
    удобной цене стопа;
  * объём бара ограничивает размер сделки, но не подсказывает сигнал.

Издержки вычитаются в ценах исполнения один раз. Считать их ещё и отдельной
строкой значило бы вычесть дважды.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .bars import Bar, MSK, by_symbol
from .signals import Signal, generate
from .spec import Spec


@dataclass(frozen=True)
class Costs:
    """Издержки в долях от оборота одной стороны."""

    commission: float = 0.0005   # 5 б.п. — как в песочнице
    half_spread: float = 0.0004
    slippage: float = 0.0003

    @property
    def per_side(self) -> float:
        return self.commission + self.half_spread + self.slippage

    def scaled(self, factor: float) -> "Costs":
        return Costs(
            self.commission * factor, self.half_spread * factor, self.slippage * factor
        )


@dataclass(frozen=True)
class Rules:
    """Ограничения прогона. Те же, что у шлюза: измеряем то, чем торгуем."""

    capital: float = 100_000.0
    max_positions: int = 2
    max_entries_per_day: int = 4
    risk_unit_fraction: float = 0.01
    max_portfolio_risk_units: float = 2.0
    entry_lag_bars: int = 1      # исполнение через N баров после сигнала
    volume_share: float = 0.02   # не больше доли объёма бара входа
    lot_size: int = 1


@dataclass
class Trade:
    """Сделка. `entry` и `exit` — цены исполнения, издержки уже в них.

    Отдельное поле `cost` существует только для отчёта. Вычитать его из
    результата ещё раз нельзя: издержки сидят в ценах исполнения, и второе
    вычитание было бы двойным счётом.
    """

    symbol: str
    version: str
    entered_at: datetime
    entry: float          # цена исполнения входа, с издержками
    stop: float
    target: float
    shares: int
    exited_at: datetime | None = None
    exit: float = 0.0     # цена исполнения выхода, с издержками
    reason: str = ""
    cost: float = 0.0     # сколько из результата съели издержки
    open_at_end: bool = False

    @property
    def net(self) -> float:
        return (self.exit - self.entry) * self.shares

    @property
    def gross(self) -> float:
        """Результат, каким он был бы без издержек. Для сравнения, не для учёта."""
        return self.net + self.cost

    @property
    def r_multiple(self) -> float:
        risk = (self.entry - self.stop) * self.shares
        return self.net / risk if risk > 0 else 0.0


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
    daily: dict[str, float] = field(default_factory=dict)
    rejected: int = 0

    @property
    def net(self) -> float:
        """Результат считается по дневной кривой, а не по списку сделок.

        Кривая — то, что видит счёт: по ней же считается просадка и по ней
        работает бутстрап. Ориентиры вроде «купил и держал» сделок не
        порождают вовсе, но результат у них есть.
        """
        return sum(self.daily.values())

    @property
    def closed(self) -> int:
        """Сделки, которые действительно закрылись по правилу.

        Позиция, оставшаяся открытой в конце периода, оценена по последней
        цене, но закрытой сделкой не считается: правило её не закрывало.
        """
        return sum(1 for trade in self.trades if not trade.open_at_end)

    @property
    def sessions(self) -> int:
        return len(self.daily)

    def max_drawdown(self) -> float:
        """Просадка по кривой дневных результатов, в долях капитала."""
        peak = equity = 0.0
        worst = 0.0
        for day in sorted(self.daily):
            equity += self.daily[day]
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return abs(worst)

    def summary(self, capital: float) -> dict:
        wins = [t for t in self.trades if t.net > 0]
        return {
            "net": round(self.net, 2),
            "return_pct": round(self.net / capital * 100, 2) if capital else 0.0,
            "trades": len(self.trades),
            "closed": self.closed,
            "sessions": self.sessions,
            "hit_rate": round(len(wins) / len(self.trades), 3) if self.trades else None,
            "avg_r": round(
                sum(t.r_multiple for t in self.trades) / len(self.trades), 3
            ) if self.trades else None,
            "max_drawdown": round(self.max_drawdown(), 2),
            "max_drawdown_pct": round(self.max_drawdown() / capital * 100, 2)
            if capital else 0.0,
            "rejected_by_rules": self.rejected,
        }


@dataclass
class _Open:
    trade: Trade
    stop: float
    target: float
    deadline: int   # индекс бара, на котором позиция закрывается по сроку
    entry_raw: float  # цена входа без издержек — для отчёта об издержках


def run(
    specs: list[Spec],
    bars: list[Bar],
    costs: Costs = Costs(),
    rules: Rules = Rules(),
    events: dict | None = None,
) -> Result:
    """Один проход по истории. Несколько спецификаций делят один счёт.

    Порядок внутри бара: сначала выходы, потом входы. Иначе освободившееся
    место занималось бы новой позицией раньше, чем закрылась старая, и
    ограничение на число позиций оказалось бы фиктивным.
    """
    series = by_symbol(bars)
    if not series:
        return Result()

    timeline = sorted({bar.start for bar in bars})
    index = {moment: i for i, moment in enumerate(timeline)}
    at: dict[str, dict[datetime, Bar]] = {
        symbol: {bar.start: bar for bar in items} for symbol, items in series.items()
    }
    # Позиция бара в своём ряду: срез истории берётся по индексу, а не
    # фильтром по всему ряду на каждом баре.
    offset: dict[str, dict[datetime, int]] = {
        symbol: {bar.start: i for i, bar in enumerate(items)}
        for symbol, items in series.items()
    }
    window = {
        spec.version: max(spec.trend_window, spec.lookback, 15) + 2 for spec in specs
    }

    result = Result()
    open_positions: dict[str, _Open] = {}
    pending: list[tuple[int, Signal]] = []
    seen_keys: set[str] = set()
    entries_today: dict[str, int] = {}
    risk_unit = rules.capital * rules.risk_unit_fraction
    risk_cap = risk_unit * rules.max_portfolio_risk_units

    for i, moment in enumerate(timeline):
        day = moment.astimezone(MSK).date().isoformat()
        result.daily.setdefault(day, 0.0)

        # --- выходы ---------------------------------------------------------
        for symbol in list(open_positions):
            bar = at[symbol].get(moment)
            if bar is None:
                continue
            position = open_positions[symbol]
            exit_price, reason = _exit_price(bar, position, i)
            if exit_price is None:
                continue
            _close(result, open_positions.pop(symbol), exit_price, reason, bar, costs, day)

        # --- отложенные входы ------------------------------------------------
        ready = [signal for due, signal in pending if due == i]
        pending = [(due, signal) for due, signal in pending if due > i]
        for signal in sorted(ready, key=lambda s: (-s.rank, s.key)):
            bar = at[signal.symbol].get(moment)
            if bar is None:
                result.rejected += 1
                continue
            if not _allowed(signal, open_positions, entries_today, day, rules,
                            risk_unit, risk_cap):
                result.rejected += 1
                continue
            position = _enter(signal, bar, costs, rules, risk_unit, i)
            if position is None:
                result.rejected += 1
                continue
            open_positions[signal.symbol] = position
            entries_today[day] = entries_today.get(day, 0) + 1
            result.trades.append(position.trade)

        # --- сигналы на закрытии бара ---------------------------------------
        for spec in specs:
            depth = window[spec.version]
            for symbol, items in series.items():
                position = offset[symbol].get(moment)
                if position is None:
                    continue
                history = items[max(0, position - depth):position + 1]
                signal = generate(spec, history, events)
                if signal is None or signal.key in seen_keys:
                    continue
                seen_keys.add(signal.key)
                due = i + max(rules.entry_lag_bars, 1)
                if due < len(timeline):
                    pending.append((due, signal))

    # Незакрытые позиции оцениваются по последнему закрытию: это оценка, а не
    # сделка, и она не превращается в удобный результат.
    for symbol, position in open_positions.items():
        last = series[symbol][-1]
        _close(
            result, position, last.close, "оценка на конец периода", last, costs,
            last.start.astimezone(MSK).date().isoformat(), open_at_end=True,
        )
    return result


def _allowed(signal, open_positions, entries_today, day, rules, risk_unit, risk_cap) -> bool:
    if signal.symbol in open_positions:
        return False  # доливать в открытую позицию нельзя
    if len(open_positions) >= rules.max_positions:
        return False
    if entries_today.get(day, 0) >= rules.max_entries_per_day:
        return False
    planned = sum(
        (p.trade.entry - p.stop) * p.trade.shares for p in open_positions.values()
    )
    return planned + risk_unit <= risk_cap + 1e-9


def _enter(signal, bar, costs, rules, risk_unit, i) -> _Open | None:
    """Вход по открытию бара, размер — от расстояния до стопа."""
    entry = bar.open * (1 + costs.per_side)
    stop = signal.stop
    if entry <= stop:
        return None  # разрыв уже ниже стопа: идея умерла, не начавшись
    per_share = entry - stop
    shares = int(risk_unit / per_share)
    shares -= shares % max(rules.lot_size, 1)
    volume_cap = int(bar.volume * rules.volume_share)
    shares = min(shares, volume_cap - volume_cap % max(rules.lot_size, 1))
    if shares < rules.lot_size:
        return None
    trade = Trade(
        symbol=signal.symbol,
        version=signal.version,
        entered_at=bar.start,
        entry=entry,
        stop=stop,
        target=signal.target,
        shares=shares,
    )
    trade.cost = bar.open * shares * costs.per_side
    return _Open(trade, stop, signal.target, i + signal.max_hold_bars, bar.open)


def _exit_price(bar: Bar, position: _Open, i: int) -> tuple[float | None, str]:
    """Цена выхода на этом баре, если он состоялся.

    Разрыв вниз исполняется по открытию: цена стопа в такой ситуации не
    существовала. Пересечение стопа и цели в одном баре считается стопом —
    пути внутри бара мы не знаем, и предполагать удачный порядок нельзя.
    """
    if bar.open <= position.stop:
        return bar.open, "разрыв через стоп"
    if bar.low <= position.stop:
        return position.stop, "стоп"
    if bar.open >= position.target:
        return bar.open, "разрыв через цель"
    if bar.high >= position.target:
        return position.target, "цель"
    if i >= position.deadline:
        return bar.close, "истёк срок идеи"
    return None, ""


def _close(result, position, price, reason, bar, costs, day, open_at_end=False) -> None:
    trade = position.trade
    trade.exited_at = None if open_at_end else bar.start
    trade.exit = price * (1 - costs.per_side)
    trade.reason = reason
    trade.open_at_end = open_at_end
    trade.cost += price * trade.shares * costs.per_side
    result.daily[day] = result.daily.get(day, 0.0) + trade.net
