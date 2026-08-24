"""Конфигурация шлюза. Все секреты читаются из файлов, не из аргументов."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Файлы с секретами. Codex не пробрасывает окружение в MCP-подпроцесс,
# поэтому читаем сами.
ENV_FILES = [
    Path("/home/agent/.agent.env"),
    ROOT / ".env",
]

TINVEST_SANDBOX_URL = "https://sandbox-invest-public-api.tbank.ru/rest"
TINVEST_NS = "tinkoff.public.invest.api.contract.v1"

CA_BUNDLE = str(ROOT / "deploy" / "RussianTrustedRootCA.pem")
DB_PATH = os.environ.get("AGENT_DB", "/home/agent/state/agent.db")

# Куда складываются объёмные данные. В контекст агента уходит путь и сводка,
# сама таблица живёт файлом: агент читает её своим кодом, а не глазами.
DATA_DIR = Path(os.environ.get("AGENT_DATA_DIR", "/home/agent/data"))

# Рабочая директория агента. Отчёты отправляются только отсюда.
WORKDIR = Path(os.environ.get("AGENT_WORKDIR", "/home/agent/work"))
REPORTS_DIR = WORKDIR / "notes" / "reports"
MAX_REPORT_BYTES = 2_000_000

# Потолок на размер ответа инструмента. Всё, что больше, выгружается в файл.
# Это страховка на уровне транспорта: даже если кто-то добавит инструмент,
# возвращающий таблицу на тысячу строк, она не окажется в сессии текстом.
MAX_TOOL_RESULT_BYTES = 8000

# Жёсткие ограничители. Проверяются в коде, а не в промпте:
# инструкции агент может переписать, этот файл — тоже, но тогда
# расхождение будет видно в git-диффе.
MAX_ORDERS_PER_HOUR = 30
ALLOW_LEVERAGE = False
ALLOW_SHORT = False

# Порог остановки. Падение стоимости портфеля ниже — конец прогона:
# торговля блокируется, супервизор перестаёт будить агента, дальше
# нужно вмешательство человека. Снимается только вручную.
CAPITAL_FLOOR = 30_000.0
STARTING_CAPITAL = 100_000.0

# --- риск ---------------------------------------------------------------
# Стартовые значения, а не истина рынка: их калибруют по накопленной
# выборке. Но пока они здесь, обойти их агент не может.

# Единица риска: сколько денег теряется при срабатывании стопа.
RISK_UNIT = 0.01 * STARTING_CAPITAL           # 1000 ₽
MAX_RISK_PER_TRADE = 1.0 * RISK_UNIT          # не больше 1R на идею
DAILY_LOSS_LIMIT = 3.0 * RISK_UNIT            # -3R — входы закрыты до утра

MIN_REWARD_RISK = 2.0        # (цель-вход)/(вход-стоп) до издержек
ROUND_TRIP_COST = 0.001      # 0.05% на сторону по факту песочницы
MIN_TARGET_OVER_COST = 10.0  # цель должна быть хотя бы в 10 раз больше издержек
MIN_STOP_ATR = 1.5           # стоп не ближе 1.5 ATR(15м) — иначе выбьет шумом

MAX_POSITIONS = 2            # одновременно открытых позиций
MAX_ENTRIES_PER_DAY = 4      # число решений, а не скорость заявок
LOSS_COOLDOWN = 45 * 60      # после убыточного выхода — пауза перед входом

SEARCH_MODEL = "gpt-5.6-luna"
SEARCH_TIMEOUT = 180
HTTP_TIMEOUT = 60


def _load_env() -> dict:
    values = {}
    for path in ENV_FILES:
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            continue
    return values


_ENV = _load_env()


def secret(name: str, default: str = "") -> str:
    """Приоритет у окружения, затем файлы, затем значение по умолчанию."""
    return os.environ.get(name) or _ENV.get(name) or default
