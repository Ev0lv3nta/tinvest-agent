"""Веб-поиск через OmniRoute.

Штатный поиск Codex обращается к внутреннему маршруту /v1/alpha/search,
которого у шлюза нет. Здесь тот же самый поиск вызывается обычным запросом
к /v1/responses с tools: [{"type": "web_search"}] — движок один и тот же.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config


class SearchError(RuntimeError):
    pass


def web_search(query: str, model: str = "") -> dict:
    base = config.secret("OMNIROUTE_BASE_URL").rstrip("/")
    key = config.secret("OMNIROUTE_API_KEY")
    if not base or not key:
        raise SearchError("не заданы OMNIROUTE_BASE_URL / OMNIROUTE_API_KEY")

    body = json.dumps(
        {
            "model": model or config.SEARCH_MODEL,
            "tools": [{"type": "web_search"}],
            "input": query,
        }
    ).encode()
    request = urllib.request.Request(
        f"{base}/responses",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Стабильный идентификатор сессии помогает попадать в кеш
            # промптов на стороне провайдера.
            "X-Session-Id": "tinvest-agent-search",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=config.SEARCH_TIMEOUT) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SearchError(f"поиск вернул HTTP {exc.code}: {exc.read()[:300]!r}") from exc
    except urllib.error.URLError as exc:
        raise SearchError(f"поиск недоступен: {exc.reason}") from exc

    sources: list[str] = []
    for item in data.get("output", []):
        for block in item.get("content") or []:
            for annotation in block.get("annotations") or []:
                url = annotation.get("url")
                if url and url not in sources:
                    sources.append(url)

    return {
        "text": data.get("output_text", ""),
        "sources": sources,
        "tokens": (data.get("usage") or {}).get("input_tokens"),
    }
