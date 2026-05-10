"""One-off probe: verify VK_COMMUNITY_TOKEN_PHOTOS scopes.

Calls 5 VK API methods that require different scopes and reports which ones
work. Helps decide whether Phase 3 (vk_post media upload) can proceed or if
the token needs to be regenerated with extra permissions.
"""
from __future__ import annotations

import os
import sys

import requests

TOKEN = os.environ.get("VK_COMMUNITY_TOKEN_PHOTOS", "")
GROUP_ID = os.environ.get("VK_GROUP_ID", "")

if not TOKEN:
    sys.stderr.write("ERROR: VK_COMMUNITY_TOKEN_PHOTOS env not set\n")
    sys.exit(0)
if not GROUP_ID:
    sys.stderr.write("ERROR: VK_GROUP_ID env not set\n")
    sys.exit(0)

GROUP_ID_INT = int(GROUP_ID)
API_VERSION = "5.199"
API_BASE = "https://api.vk.com/method"


def call(method, **params):
    params.setdefault("v", API_VERSION)
    try:
        r = requests.get(
            f"{API_BASE}/{method}",
            params={**params, "access_token": TOKEN},
            timeout=15,
        )
        data = r.json()
    except Exception as exc:
        return False, {"error": {"error_code": -1, "error_msg": repr(exc)}}
    if "error" in data:
        return False, data
    return True, data


def fmt(label, ok, info):
    return f"  {'OK' if ok else 'FAIL'} | {label:<40} | {info}"


print("=" * 70)
print(f"VK TOKEN PROBE  group_id={GROUP_ID_INT}  token_len={len(TOKEN)}")
print("=" * 70)

# 1. groups.getById — sanity (any token)
ok, data = call("groups.getById", group_id=GROUP_ID_INT)
if ok:
    name = data.get("response", {}).get("groups", [{}])[0].get("name", "?")
    print(fmt("groups.getById [sanity]", True, f"group: {name!r}"))
else:
    err = data.get("error", {})
    print(fmt("groups.getById [sanity]", False,
              f"code={err.get('error_code')} {err.get('error_msg', '')[:50]}"))
    print("\n!! sanity failed - token invalid")
    sys.exit(0)

# 2. wall.get
ok, data = call("wall.get", owner_id=-GROUP_ID_INT, count=1)
if ok:
    print(fmt("wall.get [scope: wall]", True,
              f"count={data['response']['count']}"))
else:
    err = data.get("error", {})
    print(fmt("wall.get [scope: wall]", False,
              f"code={err.get('error_code')} {err.get('error_msg', '')[:50]}"))

# 3. photos.getWallUploadServer (THE big one)
ok, data = call("photos.getWallUploadServer", group_id=GROUP_ID_INT)
if ok:
    url_len = len(data.get("response", {}).get("upload_url", ""))
    print(fmt("photos.getWallUploadServer [PHOTOS]", True,
              f"got upload URL ({url_len} chars)"))
else:
    err = data.get("error", {})
    print(fmt("photos.getWallUploadServer [PHOTOS]", False,
              f"code={err.get('error_code')} {err.get('error_msg', '')[:50]}"))

# 4. docs.getUploadServer
ok, data = call("docs.getUploadServer", group_id=GROUP_ID_INT)
if ok:
    print(fmt("docs.getUploadServer [scope: docs]", True, "got upload URL"))
else:
    err = data.get("error", {})
    print(fmt("docs.getUploadServer [scope: docs]", False,
              f"code={err.get('error_code')} {err.get('error_msg', '')[:50]}"))

# 5. video.save
ok, data = call("video.save", group_id=GROUP_ID_INT, name="probe", description="probe")
if ok:
    print(fmt("video.save [scope: video]", True, "got upload metadata"))
else:
    err = data.get("error", {})
    print(fmt("video.save [scope: video]", False,
              f"code={err.get('error_code')} {err.get('error_msg', '')[:50]}"))

print("=" * 70)
print("VK error codes context:")
print("  15 'Access denied' / 7 'Permission denied' -> missing scope")
print("  5  'User authorization failed'             -> token invalid")
print("  100 'Invalid params'                       -> method ok, param issue")
print("=" * 70)
