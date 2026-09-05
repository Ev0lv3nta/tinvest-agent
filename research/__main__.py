"""Командная строка исследовательского контура.

Команды `trade` здесь нет и не будет: этот контур не владеет счётом.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import trials
from .bars import load_csv
from .evaluate import Protocol, evaluate, passive
from .history import fetch
from .replay import Costs, Rules, run
from .spec import Spec, grid
from .synthetic import random_walk

INTERVAL_MINUTES = {
    "CANDLE_INTERVAL_1_MIN": 1,
    "CANDLE_INTERVAL_5_MIN": 5,
    "CANDLE_INTERVAL_15_MIN": 15,
    "CANDLE_INTERVAL_30_MIN": 30,
    "CANDLE_INTERVAL_HOUR": 60,
    "CANDLE_INTERVAL_DAY": 1440,
}


def show(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def load(paths: list[str], minutes: int):
    bars = []
    for item in paths:
        path = Path(item)
        symbol = path.stem.split("_")[0].upper()
        bars.extend(load_csv(path, symbol, minutes))
    if not bars:
        raise SystemExit("не загружено ни одного бара — проверь пути к файлам")
    return bars


def specs_from(args) -> list[Spec]:
    if args.family == "all":
        out = []
        for family in ("breakout", "pullback"):
            out.extend(grid(
                family,
                lookback=args.lookback,
                stop_atr=args.stop_atr,
                target_atr=args.target_atr,
            ))
        return out
    return grid(
        args.family,
        lookback=args.lookback,
        stop_atr=args.stop_atr,
        target_atr=args.target_atr,
    )


def add_grid(parser) -> None:
    parser.add_argument("--family", default="all",
                        choices=["all", "breakout", "pullback", "event_drift"])
    parser.add_argument("--lookback", type=int, nargs="+", default=[20])
    parser.add_argument("--stop-atr", type=float, nargs="+", default=[1.5])
    parser.add_argument("--target-atr", type=float, nargs="+", default=[3.0])
    parser.add_argument("--interval", default="CANDLE_INTERVAL_HOUR",
                        choices=sorted(INTERVAL_MINUTES))
    parser.add_argument("--capital", type=float, default=100_000.0)


def cmd_backtest(args) -> None:
    """Один проход по истории. Это наблюдение, а не вывод о прибыльности."""
    bars = load(args.files, INTERVAL_MINUTES[args.interval])
    rules = Rules(capital=args.capital)
    outcome = run(specs_from(args), bars, Costs(), rules)
    show({
        "внимание": "один проход на всей истории — это подгонка по построению; "
                    "вывод делает evaluate",
        **outcome.summary(args.capital),
        "пассивная_покупка": round(passive(bars, rules, Costs()).net, 2),
    })


def cmd_evaluate(args) -> None:
    """Кампания: перебор объявляется целиком, попытки пишутся до результата."""
    bars = load(args.files, INTERVAL_MINUTES[args.interval])
    candidates = specs_from(args)
    protocol = Protocol(folds=args.folds, min_oos_sessions=args.min_sessions,
                        min_closed_trades=args.min_trades)
    symbols = sorted({bar.symbol for bar in bars})
    period = f"{bars[0].start.date()}..{bars[-1].start.date()}"

    trials.open_campaign(args.campaign, args.question, symbols, period, asdict(protocol))
    for spec in candidates:
        trials.register(args.campaign, spec.version, spec.to_dict())

    registered = trials.count(args.campaign)
    report = evaluate(candidates, bars, registered, protocol, Costs(),
                      Rules(capital=args.capital))
    for spec in candidates:
        trials.finish(args.campaign, spec.version, {"campaign_verdict": report["verdict"]})
    report["campaign"] = args.campaign
    report["universe"] = symbols
    report["period"] = period
    show(report)


def cmd_demo(args) -> None:
    """Отрицательный контроль: те же правила на случайном блуждании.

    Если машинка рисует прибыль на шуме, она сломана. Ожидаемый ответ —
    «преимущество не подтверждено».
    """
    bars = random_walk(symbols=args.symbols, bars=args.bars, seed=args.seed)
    candidates = grid("breakout", lookback=[20], stop_atr=[1.5, 2.0], target_atr=[3.0])
    candidates += grid("pullback", lookback=[20], stop_atr=[1.5], target_atr=[3.0])
    report = evaluate(candidates, bars, len(candidates), Protocol(min_oos_sessions=10,
                                                                 min_closed_trades=5))
    show({
        "источник": "синтетическое случайное блуждание, не рынок",
        "verdict": report["verdict"],
        "net": report["net"],
        "closed_trades": report["closed_trades"],
        "failed_gates": report["failed_gates"],
        "bootstrap": report["bootstrap"],
    })


def cmd_fetch(args) -> None:
    from gateway.config import secret
    from gateway.tinvest import SandboxClient

    client = SandboxClient(secret("TINVEST_TOKEN"), secret("TINVEST_ACCOUNT_ID"))
    out = Path(args.out) / f"{args.ticker.upper()}_{args.interval[16:].lower()}.csv"
    show(fetch(client, args.instrument_id, args.interval, args.days, out))


def cmd_trials(args) -> None:
    show({"campaign": trials.campaign(args.campaign), "trials": trials.trials(args.campaign)})


def cmd_campaigns(args) -> None:
    show(trials.campaigns())


def cmd_close(args) -> None:
    trials.close_campaign(args.campaign, args.verdict)
    show(trials.campaign(args.campaign))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="research",
        description="Проверка гипотез на истории. Торговых команд здесь нет.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    back = sub.add_parser("backtest", help="один проход по всей истории")
    back.add_argument("files", nargs="+")
    add_grid(back)
    back.set_defaults(func=cmd_backtest)

    ev = sub.add_parser("evaluate", help="кампания с walk-forward и воротами")
    ev.add_argument("files", nargs="+")
    ev.add_argument("--campaign", required=True)
    ev.add_argument("--question", required=True,
                    help="что именно проверяем и что опровергнет гипотезу")
    ev.add_argument("--folds", type=int, default=3)
    ev.add_argument("--min-sessions", type=int, default=60)
    ev.add_argument("--min-trades", type=int, default=40)
    add_grid(ev)
    ev.set_defaults(func=cmd_evaluate)

    demo = sub.add_parser("demo", help="отрицательный контроль на случайном блуждании")
    demo.add_argument("--symbols", type=int, default=6)
    demo.add_argument("--bars", type=int, default=6000)
    demo.add_argument("--seed", type=int, default=29)
    demo.set_defaults(func=cmd_demo)

    get = sub.add_parser("fetch", help="скачать историю в CSV")
    get.add_argument("instrument_id")
    get.add_argument("ticker")
    get.add_argument("--interval", default="CANDLE_INTERVAL_HOUR",
                     choices=sorted(INTERVAL_MINUTES))
    get.add_argument("--days", type=int, default=365)
    get.add_argument("--out", default="history")
    get.set_defaults(func=cmd_fetch)

    lst = sub.add_parser("trials", help="попытки кампании")
    lst.add_argument("campaign")
    lst.set_defaults(func=cmd_trials)

    sub.add_parser("campaigns", help="список кампаний").set_defaults(func=cmd_campaigns)

    end = sub.add_parser("close", help="закрыть кампанию решением")
    end.add_argument("campaign")
    end.add_argument("verdict", choices=["продолжить", "следующая-фаза", "гипотеза-закрыта"])
    end.set_defaults(func=cmd_close)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
