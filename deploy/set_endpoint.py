#!/usr/bin/env python3
"""Сменить адрес OmniRoute во всех местах разом.

Quick Tunnel на trycloudflare выдаёт новый адрес при каждом перезапуске,
а прописан он в трёх местах: файл секретов, конфигурация Codex и окружение
MCP-сервера. Ручная правка гарантированно что-нибудь пропустит.

    python3 deploy/set_endpoint.py https://новый-адрес.trycloudflare.com/v1
"""

import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path("/home/agent/.agent.env")
CODEX_CONFIG = Path("/home/agent/.codex/config.toml")


def secret(name: str) -> str:
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return ""


def check(url: str, key: str) -> tuple[bool, str]:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status == 200, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:100]


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(1)
    url = sys.argv[1].rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"

    key = secret("OMNIROUTE_API_KEY")
    ok, detail = check(url, key)
    print(f"проверка адреса: {detail}")
    if not ok:
        print("новый адрес не отвечает — ничего не меняю")
        raise SystemExit(2)

    text = ENV_FILE.read_text()
    ENV_FILE.write_text(
        re.sub(r"(?m)^OMNIROUTE_BASE_URL=.*$", f"OMNIROUTE_BASE_URL={url}", text)
    )
    print(f"обновлён {ENV_FILE}")

    config_text = CODEX_CONFIG.read_text()
    # Якорим на секцию провайдера: голый ^base_url бил бы по любой секции,
    # если в конфиге появится второй провайдер.
    updated, count = re.subn(
        r'(?ms)(^\[model_providers\.omniroute\][^\[]*?^base_url = )"[^"]*"',
        lambda m: f'{m.group(1)}"{url}"',
        config_text,
    )
    if count != 1:
        raise SystemExit(
            f"в {CODEX_CONFIG} не найдена ровно одна base_url в секции "
            f"[model_providers.omniroute] (найдено {count}) — правь вручную"
        )
    CODEX_CONFIG.write_text(updated)
    print(f"обновлён {CODEX_CONFIG}")

    result = subprocess.run(["systemctl", "restart", "tinvest-agent"], check=False)
    if result.returncode == 0:
        print("супервизор перезапущен — агент подхватит новый адрес")
    else:
        print(
            f"перезапуск не удался (код {result.returncode}); "
            f"выполни вручную: systemctl restart tinvest-agent"
        )


if __name__ == "__main__":
    main()
