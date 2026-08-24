"""Конфигурация шлюза. Все секреты читаются из файлов, не из аргументов."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Файлы с секретами. Codex не пробрасывает окружение в MCP-подпроцесс,
# поэтому читаем сами.
ENV_FILES = [
    Path("/home/agent/.tinvest.env"),
    Path("/home/agent/.omniroute.env"),
    Path("/home/agent/.telegram.env"),
    ROOT / ".env",
]

TINVEST_SANDBOX_URL = "https://sandbox-invest-public-api.tbank.ru/rest"
TINVEST_NS = "tinkoff.public.invest.api.contract.v1"

CA_BUNDLE = str(ROOT / "deploy" / "RussianTrustedRootCA.pem")
DB_PATH = os.environ.get("AGENT_DB", "/home/agent/state/agent.db")

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
