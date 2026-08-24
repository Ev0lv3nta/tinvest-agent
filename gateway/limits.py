"""Квоты подключённых аккаунтов OmniRoute.

Роутер опрашивает Codex quota API авторизациями каждого Plus-аккаунта и
отдаёт остаток по каждому. Знать его важно: упёршись в лимит, агент начнёт
получать отказы, а внешне это выглядит просто как «всё тормозит».

Запрос с forceRefresh ходит к провайдеру, поэтому обычные вызовы читают
кеш, а принудительное обновление делается только по явной просьбе и не
чаще раза в минуту.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import config, journal

ENDPOINT = "/api/usage/provider-limits"
CACHE_TTL = 300
FORCE_MIN_INTERVAL = 60


class LimitsError(RuntimeError):
    pass


def _call(force: bool) -> dict:
    base = config.secret("OMNIROUTE_BASE_URL").rstrip("/")
    key = config.secret("OMNIROUTE_MGMT_KEY")
    if not key:
        raise LimitsError("не задан OMNIROUTE_MGMT_KEY")
    # Управляющий эндпоинт живёт рядом с /v1, но не внутри него.
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    request = urllib.request.Request(
        root + ENDPOINT,
        data=json.dumps({"forceRefresh": force}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise LimitsError(f"HTTP {exc.code}: {exc.read()[:200]!r}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LimitsError(str(exc)[:200]) from exc


def fetch(force: bool = False) -> dict:
    """Квоты аккаунтов. Возвращает готовый к показу вид."""
    cached = journal.kv_get("limits_cache", "")
    cached_ts = float(journal.kv_get("limits_cache_ts", "0") or 0)
    age = time.time() - cached_ts

    if force and age < FORCE_MIN_INTERVAL:
        force = False  # только что обновляли, к провайдеру не идём
    if cached and not force and age < CACHE_TTL:
        payload = json.loads(cached)
        payload["age"] = int(age)
        return payload

    raw = _call(force)
    accounts = []
    for account_id, item in (raw.get("caches") or {}).items():
        session = (item.get("quotas") or {}).get("session") or {}
        total = int(session.get("total") or 0)
        used = int(session.get("used") or 0)
        accounts.append(
            {
                "id": account_id[:8],
                "plan": item.get("plan") or "?",
                "period": session.get("displayName") or "",
                "used": used,
                "total": total,
                "remaining": int(session.get("remaining") or 0),
                "percent": round(used / total * 100, 1) if total else 0.0,
                "unlimited": bool(session.get("unlimited")),
                "reset_at": session.get("resetAt") or "",
                "banked": item.get("bankedResetCredits") or 0,
            }
        )
    for account in accounts:
        account["reset_human"] = reset_in(account["reset_at"])
    accounts.sort(key=lambda a: a["id"])

    payload = {
        "accounts": accounts,
        "failed": int(raw.get("failed") or 0),
        "fetched_at": time.time(),
        "age": 0,
    }
    journal.kv_set("limits_cache", json.dumps(payload, ensure_ascii=False))
    journal.kv_set("limits_cache_ts", str(time.time()))
    return payload


def reset_in(reset_at: str) -> str:
    """Сколько осталось до сброса квоты, по-человечески."""
    if not reset_at:
        return ""
    try:
        moment = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = (moment - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "вот-вот"
    days, hours = int(delta // 86400), int(delta % 86400 // 3600)
    if days:
        return f"через {days} дн {hours} ч"
    minutes = int(delta % 3600 // 60)
    return f"через {hours} ч {minutes} мин" if hours else f"через {minutes} мин"
