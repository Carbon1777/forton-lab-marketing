"""Instagram-канал: подписчики @forton_lab — БЕЗ обязательных секретов.

У Instagram нет простого открытого API как у VK/YouTube. Официальный путь —
Instagram Graph API (нужен Meta-app + Business-аккаунт + long-lived token); он
надёжен, но требует настройки. Здесь используется публичный web-профильный
endpoint `web_profile_info` (тот же, что отдаёт число на странице профиля) —
ноль настройки. Минус: Instagram агрессивно лимитирует датацентр-IP, поэтому с
GitHub Actions запрос МОЖЕТ вернуть 401/429/challenge. В этом случае —
error-снапшот («optional»): дайджест рендерит Instagram как «нет данных» и не
падает. Никакого блокера.

Аккаунт @forton_lab — Business/Professional, значит при желании можно перейти на
официальный Graph API (followers_count) без смены типа аккаунта — тогда
подменить только тело fetch_weekly, контракт снапшота тот же.

Username берётся из env INSTAGRAM_USERNAME (по умолчанию forton_lab). Пустая
строка в INSTAGRAM_USERNAME отключает канал (error-снапшот «disabled»).
"""
from __future__ import annotations

import datetime as dt
import os

from ._http import fetch_with_retry
from .models import ChannelSnapshot

IG_PROFILE_API = "https://www.instagram.com/api/v1/users/web_profile_info/"
# Публичный web app id инстаграма (не секрет; нужен, иначе endpoint отдаёт 302).
_IG_APP_ID = "936619743392459"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_DEFAULT_USERNAME = "forton_lab"


def _ok(week_start: dt.date, subs) -> ChannelSnapshot:
    return ChannelSnapshot(
        platform="instagram", week_start=week_start,
        subscribers=int(subs) if subs is not None else None,
    )


def _err(week_start: dt.date, msg: str) -> ChannelSnapshot:
    return ChannelSnapshot(
        platform="instagram", week_start=week_start, subscribers=None, error=msg,
    )


def fetch_weekly(week_start: dt.date) -> ChannelSnapshot:
    # env задан и пуст → канал сознательно отключён.
    username = os.environ.get("INSTAGRAM_USERNAME", _DEFAULT_USERNAME)
    if not username:
        return _err(week_start, "IG disabled (INSTAGRAM_USERNAME empty)")

    try:
        resp = fetch_with_retry(
            IG_PROFILE_API,
            method="GET",
            headers={"x-ig-app-id": _IG_APP_ID, "User-Agent": _UA},
            params={"username": username},
        )
        if resp.status_code != 200:
            # 401/403/429/302 → IG заблокировал/челлендж (частый случай на CI-IP).
            return _err(week_start, f"IG optional: HTTP {resp.status_code}")
        user = (resp.json().get("data") or {}).get("user") or {}
        count = (user.get("edge_followed_by") or {}).get("count")
        if count is None:
            return _err(week_start, "IG: follower count missing")
        return _ok(week_start, count)
    except Exception as exc:  # noqa: BLE001 — optional-канал, мягкая деградация
        return _err(week_start, f"IG optional {type(exc).__name__}: {str(exc)[:60]}")
