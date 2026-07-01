"""VK-сообщество: подписчики (всегда) + недельный охват (best-effort).

Ноль новых секретов: переиспользует VK_GROUP_TOKEN + VK_GROUP_ID (те же, что
vk_post.py). members_count через groups.getById работает с любым токеном;
охват через stats.get требует токен админа сообщества со stats-доступом —
если недоступно, reach=None (тихая деградация, дайджест не падает).
"""
from __future__ import annotations

import datetime as dt
import os

from ._http import fetch_with_retry
from .models import ChannelSnapshot

VK_API = "https://api.vk.com/method"
VK_VERSION = "5.199"


def _vk_call(method: str, token: str, **params) -> object:
    params["access_token"] = token
    params["v"] = VK_VERSION
    resp = fetch_with_retry(f"{VK_API}/{method}", method="POST", data=params)
    data = resp.json()
    if "error" in data:
        err = data["error"]
        msg = err.get("error_msg") if isinstance(err, dict) else str(err)
        raise RuntimeError(f"VK {method}: {str(msg)[:80]}")
    return data["response"]


def _fetch_members(token: str, group_id: str) -> int:
    resp = _vk_call("groups.getById", token, group_id=group_id, fields="members_count")
    # v5.199 может вернуть {"groups":[{...}]} (новый) или голый список (старый).
    if isinstance(resp, dict):
        groups = resp.get("groups") or []
    else:
        groups = resp
    if not groups:
        raise RuntimeError("groups.getById: empty response")
    return int(groups[0]["members_count"])


def _fetch_reach(token: str, group_id: str, week_start: dt.date) -> int | None:
    ts_from = int(
        dt.datetime(
            week_start.year, week_start.month, week_start.day,
            tzinfo=dt.timezone.utc,
        ).timestamp()
    )
    ts_to = ts_from + 7 * 86400
    resp = _vk_call(
        "stats.get", token,
        group_id=group_id,
        timestamp_from=ts_from,
        timestamp_to=ts_to,
        intervals="week",
    )
    total = 0
    found = False
    for period in resp or []:
        reach = period.get("reach") or {} if isinstance(period, dict) else {}
        val = reach.get("reach")
        if val is not None:
            total += int(val)
            found = True
    return total if found else None


def fetch_weekly(week_start: dt.date) -> ChannelSnapshot:
    token = os.environ.get("VK_GROUP_TOKEN")
    group_id = os.environ.get("VK_GROUP_ID")
    if not (token and group_id):
        return ChannelSnapshot(
            platform="vk", week_start=week_start, subscribers=None,
            error="VK_GROUP_TOKEN/VK_GROUP_ID not set",
        )
    try:
        members = _fetch_members(token, group_id)
    except Exception as exc:  # noqa: BLE001
        return ChannelSnapshot(
            platform="vk", week_start=week_start, subscribers=None,
            error=f"{type(exc).__name__}: {str(exc)[:80]}",
        )
    # Охват — best-effort: community-токен часто без stats-доступа → reach=None.
    reach: int | None = None
    try:
        reach = _fetch_reach(token, group_id, week_start)
    except Exception:  # noqa: BLE001 — deliberately swallow: reach optional
        reach = None
    return ChannelSnapshot(
        platform="vk", week_start=week_start, subscribers=members, reach=reach,
    )
