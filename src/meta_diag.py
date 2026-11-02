"""Диагностика доступа Meta (без вывода токена): кто мы, какие права, видна ли страница.

Запуск: воркфлоу meta_diag.yml (workflow_dispatch). Окружение: META_PAGE_TOKEN, META_PAGE_ID.
"""
from __future__ import annotations

import json
import os
import sys

import requests

GRAPH = "https://graph.facebook.com"


def call(path: str, token: str, **params) -> dict:
    r = requests.get(f"{GRAPH}/{os.environ.get('META_GRAPH_VERSION', 'v25.0')}/{path}",
                     params={**params, "access_token": token}, timeout=60)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    return {"http": r.status_code, **({"error": data["error"]} if "error" in data else {"data": data})}


SECRET_KEYS = {"access_token", "token", "client_secret", "refresh_token"}


def scrub(o):
    """Рекурсивно прячет значения секретных ключей (репозиторий публичный — логи видны всем)."""
    if isinstance(o, dict):
        return {k: ("<скрыто>" if k in SECRET_KEYS else scrub(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [scrub(x) for x in o]
    return o


def mask(d):
    s = json.dumps(scrub(d), ensure_ascii=False, default=str)
    for k in ("META_PAGE_TOKEN",):
        v = os.environ.get(k)
        if v:
            s = s.replace(v, "***")
    return s


def main() -> int:
    token = os.environ.get("META_PAGE_TOKEN")
    page = os.environ.get("META_PAGE_ID")
    if not token:
        print("META_PAGE_TOKEN не задан")
        return 1
    steps = [
        ("debug_token (кто мы, срок, права)", call("debug_token", token, input_token=token)),
        ("me", call("me", token, fields="id,name")),
        ("страница", call(page, token, fields="id,name")),
        ("страница → токен и Instagram", call(page, token, fields="access_token,instagram_business_account{id,username}")),
        ("me/accounts", call("me/accounts", token, fields="id,name")),
    ]
    for title, res in steps:
        print(f"== {title}\n{mask(res)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
