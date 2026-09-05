"""Оценка: превращает «выглядит хорошо» в «да» или «нет».

Устройство простое и намеренно недоверчивое.

  * Кандидат выбирается только по обучающей части. Если на ней все кандидаты
    в минусе, выбирается отсутствие сделок — это допустимый ответ.
  * Между обучением и проверкой оставляется зазор не меньше максимального
    срока удержания, иначе последняя сделка обучения продолжается внутри
    проверки.
  * Проверочные отрезки не пересекаются, неполный последний не считается за
    полноценный.
  * Выбранное правило проверяется ещё и при удвоенных издержках и при входе
    на бар позже. Преимущество, которое исчезает от одного бара задержки, —
    это не преимущество, а точность попадания в цену.
  * Результат сравнивается с «ничего не делать» и «купил и держал». Обогнать
    ноль недостаточно: рынок мог просто расти.
  * Нижняя граница среднего дневного результата считается блочным
    бутстрапом с поправкой на число зарегистрированных попыток.

Чего здесь нет: это не Deflated Sharpe Ratio и не решение проблемы
переобучения при адаптивном переборе. Это консервативный фильтр, который
отсекает часть случайных победителей, а не доказательство преимущества.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import asdict, dataclass, field

from .bars import Bar, MSK, by_symbol, sessions
from .replay import Costs, Result, Rules, run
from .spec import Spec


@dataclass(frozen=True)
class Protocol:
    """Критерии, объявляемые до перебора.

    Числа — инженерные минимумы этого стенда, а не статистическая
    достаточность. Сорок сделок одного события в один день — это не сорок
    независимых наблюдений, сколько бы их ни было по счётчику.
    """

    folds: int = 3
    train_fraction: float = 0.6
    embargo_bars: int = 24
    min_oos_sessions: int = 60
    min_closed_trades: int = 40
    min_positive_fold_fraction: float = 0.6
    max_drawdown_fraction: float = 0.25
    bootstrap_block: int = 5
    bootstrap_samples: int = 4000
    alpha: float = 0.05
    must_beat_passive: bool = True


def block_bootstrap(daily: list[float], trials: int, protocol: Protocol,
                    seed: int = 7) -> dict:
    """Нижняя граница среднего дневного результата.

    Блочный, потому что дни не независимы: серия одной идеи растянута на
    несколько дней подряд, и обычный бутстрап разорвал бы эту связь и выдал
    более узкий интервал, чем есть на самом деле.

    Поправка на число попыток — деление уровня на количество
    зарегистрированных кандидатов. Грубая, зато не требует предположений о
    форме распределения.
    """
    if trials < 1:
        raise ValueError("число попыток должно включать все зарегистрированные")
    n = len(daily)
    if n < protocol.bootstrap_block * 3:
        return {
            "resolved": False,
            "why": f"дней всего {n} — на блочный бутстрап не хватает",
            "lower_mean": None,
            "trials": trials,
        }

    alpha = protocol.alpha / trials
    # Хвост должен опираться на выборки, а не на интерполяцию одной точки.
    resolved = protocol.bootstrap_samples * alpha >= 10
    block = min(protocol.bootstrap_block, n)
    blocks = n // block
    rng = random.Random(seed)
    means = []
    for _ in range(protocol.bootstrap_samples):
        total = 0.0
        for _ in range(blocks):
            start = rng.randrange(n)
            total += sum(daily[(start + k) % n] for k in range(block))
        means.append(total / (blocks * block))
    means.sort()
    index = max(0, min(len(means) - 1, math.floor(alpha * len(means))))
    return {
        "resolved": resolved,
        "why": "" if resolved else (
            f"уровень {alpha:.5f} при {protocol.bootstrap_samples} выборках "
            f"опирается меньше чем на десять наблюдений в хвосте"
        ),
        "lower_mean": round(means[index], 4),
        "sample_mean": round(statistics.fmean(daily), 4),
        "alpha_per_trial": alpha,
        "trials": trials,
        "days": n,
        "block": block,
        "method": "circular_block_bootstrap + Bonferroni, не DSR и не PBO",
    }


def passive(bars: list[Bar], rules: Rules, costs: Costs) -> Result:
    """Равновзвешенная покупка всего и удержание до конца, с издержками.

    Это ориентир, а не инвестиционная альтернатива: без дивидендов и без
    расходов на исследование. Но обогнать его обязательно — иначе вся
    работа сводится к дорогому способу купить рынок.
    """
    series = by_symbol(bars)
    result = Result()
    if not series:
        return result
    share = rules.capital / len(series)
    for symbol, items in series.items():
        first, last = items[0], items[-1]
        entry = first.open * (1 + costs.per_side)
        exit_price = last.close * (1 - costs.per_side)
        shares = int(share / entry)
        if shares < 1:
            continue
        day = last.start.astimezone(MSK).date().isoformat()
        result.daily[day] = result.daily.get(day, 0.0) + (exit_price - entry) * shares
    for day in sessions(bars):
        result.daily.setdefault(day, 0.0)
    return result


@dataclass
class Fold:
    index: int
    train: tuple[int, int]
    test: tuple[int, int]
    chosen: str | None = None
    net: float = 0.0
    closed: int = 0
    sessions: int = 0


def split(total: int, protocol: Protocol) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Непересекающиеся проверочные отрезки с зазором после обучения.

    Неполный последний отрезок не возвращается: короткий кусок, поданный
    как ещё одна полноценная проверка, завышает число независимых
    наблюдений.
    """
    out = []
    chunk = total // protocol.folds
    if chunk <= protocol.embargo_bars + 10:
        return out
    for i in range(protocol.folds):
        start = i * chunk
        train_end = start + int(chunk * protocol.train_fraction)
        test_start = train_end + protocol.embargo_bars
        test_end = start + chunk
        if test_end - test_start < 10:
            continue
        out.append(((start, train_end), (test_start, test_end)))
    return out


def evaluate(
    specs: list[Spec],
    bars: list[Bar],
    trials: int,
    protocol: Protocol = Protocol(),
    costs: Costs = Costs(),
    rules: Rules = Rules(),
    events: dict | None = None,
) -> dict:
    """Полный проход: выбор на обучении, проверка вне его, стресс, ворота."""
    ordered = sorted(bars, key=lambda b: b.start)
    moments = sorted({bar.start for bar in ordered})
    windows = split(len(moments), protocol)
    if not windows:
        return {
            "verdict": "недостаточно данных",
            "why": f"баров {len(moments)} — на {protocol.folds} отрезка не делится",
            "eligible": False,
        }

    folds: list[Fold] = []
    oos_daily: dict[str, float] = {}
    oos_trades = 0
    closed: list = []
    for i, ((a, b), (c, d)) in enumerate(windows):
        train = [bar for bar in ordered if moments[a] <= bar.start < moments[b]]
        test = [bar for bar in ordered if moments[c] <= bar.start < moments[min(d, len(moments) - 1)]]
        fold = Fold(i, (a, b), (c, d))

        best, best_net = None, 0.0
        for spec in specs:
            outcome = run([spec], train, costs, rules, events)
            if outcome.net > best_net:
                best, best_net = spec, outcome.net
        if best is None:
            # На обучении в плюсе не было никого. Отсутствие сделок — это
            # ответ, а не повод выбрать наименее убыточного.
            fold.chosen = None
            folds.append(fold)
            continue

        outcome = run([best], test, costs, rules, events)
        fold.chosen = best.name
        fold.net = round(outcome.net, 2)
        fold.closed = outcome.closed
        fold.sessions = outcome.sessions
        oos_trades += outcome.closed
        closed.extend(t for t in outcome.trades if not t.open_at_end)
        for day, value in outcome.daily.items():
            oos_daily[day] = oos_daily.get(day, 0.0) + value
        folds.append(fold)

    traded = [fold for fold in folds if fold.chosen]
    net = round(sum(fold.net for fold in folds), 2)
    daily = [oos_daily[day] for day in sorted(oos_daily)]

    stress = _stress(specs, ordered, moments, windows, protocol, costs, rules, events)
    reference = passive(ordered, rules, costs)
    interval = block_bootstrap(daily, trials, protocol) if daily else {
        "resolved": False, "why": "вне обучения не было ни одного дня", "lower_mean": None
    }

    positive = sum(1 for fold in traded if fold.net > 0)
    fraction = positive / len(traded) if traded else 0.0
    drawdown = _drawdown(daily) / rules.capital if daily else 0.0

    gates = {
        "хватает_сессий": len(daily) >= protocol.min_oos_sessions,
        "хватает_сделок": oos_trades >= protocol.min_closed_trades,
        "результат_положительный": net > 0,
        "держит_двойные_издержки": stress["double_costs"] > 0,
        "держит_задержку_входа": stress["entry_lag"] > 0,
        "большинство_отрезков_в_плюс": fraction >= protocol.min_positive_fold_fraction,
        "просадка_в_пределах": drawdown <= protocol.max_drawdown_fraction,
        "нижняя_граница_положительна": bool(
            interval.get("resolved") and (interval.get("lower_mean") or 0) > 0
        ),
        "обгоняет_пассивную_покупку": (
            net > reference.net if protocol.must_beat_passive else True
        ),
    }
    failed = [name for name, passed in gates.items() if not passed]

    if not traded:
        why = (
            "ни на одном обучающем отрезке кандидат не вышел в плюс — выбрано "
            "отсутствие сделок. Это ответ, а не сбой: правило не заработало "
            "даже там, где его подбирали"
        )
    elif not oos_trades:
        why = (
            "кандидат выбран, но вне обучения не сработал ни разу — условие "
            "входа слишком редкое для этого периода"
        )
    else:
        why = ""

    return {
        "verdict": "преимущество не подтверждено" if failed else "проверку прошло",
        "why": why,
        "eligible": not failed,
        # Даже при всех зелёных воротах это разрешение проверять дальше, а не
        # разрешение торговать и не установленная прибыльность.
        "trading_allowed": False,
        "net": net,
        "net_pct": round(net / rules.capital * 100, 2),
        "closed_trades": oos_trades,
        # Статистика только по проверочным отрезкам: на обучении кандидата
        # выбирали, и его результат там завышен по построению.
        "oos": {
            "trades": len(closed),
            "wins": sum(1 for t in closed if t.net > 0),
            "avg_r": round(
                sum(t.r_multiple for t in closed) / len(closed), 3
            ) if closed else 0.0,
        },
        "oos_sessions": len(daily),
        "positive_folds": f"{positive}/{len(traded)}",
        "max_drawdown_pct": round(drawdown * 100, 2),
        "stress": stress,
        "baselines": {
            "cash": 0.0,
            "passive": round(reference.net, 2),
            "passive_pct": round(reference.net / rules.capital * 100, 2),
        },
        "bootstrap": interval,
        "folds": [asdict(fold) for fold in folds],
        "candidates": len(specs),
        "registered_trials": trials,
        "protocol": asdict(protocol),
        "failed_gates": failed,
    }


def _stress(specs, ordered, moments, windows, protocol, costs, rules, events) -> dict:
    """Тот же выбор при удвоенных издержках и при входе на бар позже."""
    out = {}
    for label, kwargs in (
        ("double_costs", {"costs": costs.scaled(2.0)}),
        ("entry_lag", {"rules": Rules(**{**asdict(rules), "entry_lag_bars": rules.entry_lag_bars + 1})}),
    ):
        use_costs = kwargs.get("costs", costs)
        use_rules = kwargs.get("rules", rules)
        total = 0.0
        for (a, b), (c, d) in windows:
            train = [bar for bar in ordered if moments[a] <= bar.start < moments[b]]
            test = [bar for bar in ordered if moments[c] <= bar.start < moments[min(d, len(moments) - 1)]]
            best, best_net = None, 0.0
            for spec in specs:
                outcome = run([spec], train, use_costs, use_rules, events)
                if outcome.net > best_net:
                    best, best_net = spec, outcome.net
            if best is not None:
                total += run([best], test, use_costs, use_rules, events).net
        out[label] = round(total, 2)
    return out


def _drawdown(daily: list[float]) -> float:
    peak = equity = worst = 0.0
    for value in daily:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)
