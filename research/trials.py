"""Реестр попыток. Каждая гипотеза записывается ДО того, как посчитан её результат.

Смысл реестра — не бухгалтерия, а честный знаменатель. Если перебрать сорок
вариантов и показать лучший, его результат нельзя оценивать так, будто
проверяли один. Реестр помнит все сорок, включая брошенные на середине, и
поправка на множественность считается по этому числу, а не по числу
понравившихся.

База отдельная от торгового журнала: исследование не должно иметь причин
открывать файл, в котором лежат заявки.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.environ.get("RESEARCH_DB", Path.home() / ".tinvest-research.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    name       TEXT PRIMARY KEY,
    created_ts REAL NOT NULL,
    question   TEXT NOT NULL,
    universe   TEXT NOT NULL,
    period     TEXT NOT NULL,
    protocol   TEXT NOT NULL,
    closed_ts  REAL,
    verdict    TEXT
);

CREATE TABLE IF NOT EXISTS trials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign    TEXT NOT NULL,
    version     TEXT NOT NULL,
    spec        TEXT NOT NULL,
    started_ts  REAL NOT NULL,
    finished_ts REAL,
    result      TEXT,
    UNIQUE (campaign, version)
);
CREATE INDEX IF NOT EXISTS idx_trials_campaign ON trials(campaign);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def open_campaign(name: str, question: str, universe: list[str], period: str,
                  protocol: dict) -> None:
    """Кампания объявляется до перебора: вопрос, бумаги, период, критерии.

    Объявить их после просмотра результатов означало бы выбрать те, при
    которых результат выглядит лучше.
    """
    conn = connect()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO campaigns (name, created_ts, question, universe,"
            " period, protocol) VALUES (?, ?, ?, ?, ?, ?)",
            (
                name, time.time(), question,
                json.dumps(sorted(universe), ensure_ascii=False),
                period, json.dumps(protocol, ensure_ascii=False, sort_keys=True),
            ),
        )
    conn.close()


def register(campaign: str, version: str, spec: dict) -> None:
    """Записать попытку. Вызывается до расчёта, результат дописывается потом."""
    conn = connect()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO trials (campaign, version, spec, started_ts)"
            " VALUES (?, ?, ?, ?)",
            (campaign, version, json.dumps(spec, ensure_ascii=False, sort_keys=True),
             time.time()),
        )
    conn.close()


def finish(campaign: str, version: str, result: dict) -> None:
    conn = connect()
    with conn:
        conn.execute(
            "UPDATE trials SET finished_ts = ?, result = ? WHERE campaign = ?"
            " AND version = ?",
            (time.time(), json.dumps(result, ensure_ascii=False, default=str),
             campaign, version),
        )
    conn.close()


def count(campaign: str) -> int:
    """Сколько попыток зарегистрировано, включая незавершённые.

    Незавершённые считаются намеренно: попытка, брошенная потому, что
    промежуточный результат не понравился, — это тоже попытка.
    """
    conn = connect()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM trials WHERE campaign = ?", (campaign,)
    ).fetchone()
    conn.close()
    return int(row["n"])


def trials(campaign: str) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM trials WHERE campaign = ? ORDER BY id", (campaign,)
    ).fetchall()
    conn.close()
    return [
        {
            "version": row["version"],
            "spec": json.loads(row["spec"]),
            "started_ts": row["started_ts"],
            "finished": row["finished_ts"] is not None,
            "result": json.loads(row["result"]) if row["result"] else None,
        }
        for row in rows
    ]


def campaigns() -> list[dict]:
    conn = connect()
    rows = conn.execute("SELECT * FROM campaigns ORDER BY created_ts").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def close_campaign(name: str, verdict: str) -> None:
    """Кампания завершается одним из трёх решений, а не тем, что надоело."""
    conn = connect()
    with conn:
        conn.execute(
            "UPDATE campaigns SET closed_ts = ?, verdict = ? WHERE name = ?",
            (time.time(), verdict, name),
        )
    conn.close()


def campaign(name: str) -> Optional[dict]:
    conn = connect()
    row = conn.execute("SELECT * FROM campaigns WHERE name = ?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None
