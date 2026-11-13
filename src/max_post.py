"""Forton Lab — публикатор в канал студии в MAX (мессенджер VK).

Берёт файлы из published/ (уже ушедшие в Telegram через tg_post.py) и публикует
их в канал MAX от имени бота студии (бот — администратор канала с правом write).

Какие посты идут в MAX:
    - в `channels:` есть `max` или `tg` (MAX повторяет Telegram), и
    - дата в имени файла (published/YYYY-MM-DD-…) не раньше MAX_START_DATE —
      чтобы при подключении не опубликовать весь архив, и
    - во frontmatter ещё нет `max_post_id`.
Пост без `max` в channels, но с `max_skip: true`, пропускается.

Медиа: `video:` → загрузка видео (type=video, токен до загрузки, повтор при
attachment.not.ready); `image:` → загрузка картинки (токен после загрузки).
Текст — HTML Telegram (b, i, u, s, a, code) с format=html, лимит 4000.
Необязательно: `max_button: {text: …, url: https://…}` — кнопка-ссылка под постом.

После успеха во frontmatter пишутся `max_post_id` (message.body.mid) и
`max_posted_at`.

API: https://platform-api2.max.ru, токен в Authorization без «Bearer».
Сертификат API — УЦ Минцифры (Russian Trusted Sub CA): добавляем его к certifi.

Окружение:
    MAX_BOT_TOKEN    — токен бота студии
    MAX_CHANNEL_ID   — chat_id канала (int64)
    MAX_START_DATE   — YYYY-MM-DD, по умолчанию DEFAULT_START_DATE
Без MAX_BOT_TOKEN / MAX_CHANNEL_ID шаг тихо пропускается (exit 0).
"""

from __future__ import annotations

import datetime as dt
import mimetypes
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import certifi
import frontmatter
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISHED_DIR = REPO_ROOT / "published"

MAX_API = "https://platform-api2.max.ru"
MAX_TEXT_LIMIT = 4000
DEFAULT_START_DATE = "2026-11-14"
VIDEO_READY_DELAYS = (3, 5, 10, 20, 30, 45)
TIMEOUT = 60

FILE_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")

# Russian Trusted Sub CA (Минцифры) — из цепочки platform-api2.max.ru, до 19.07.2029.
MAX_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIG6DCCBNCgAwIBAgICEAUwDQYJKoZIhvcNAQELBQAwcDELMAkGA1UEBhMCUlUx
PzA9BgNVBAoMNlRoZSBNaW5pc3RyeSBvZiBEaWdpdGFsIERldmVsb3BtZW50IGFu
ZCBDb21tdW5pY2F0aW9uczEgMB4GA1UEAwwXUnVzc2lhbiBUcnVzdGVkIFJvb3Qg
Q0EwHhcNMjQwNzE1MTI1MDQxWhcNMjkwNzE5MTI1MDQxWjBvMQswCQYDVQQGEwJS
VTE/MD0GA1UECgw2VGhlIE1pbmlzdHJ5IG9mIERpZ2l0YWwgRGV2ZWxvcG1lbnQg
YW5kIENvbW11bmljYXRpb25zMR8wHQYDVQQDDBZSdXNzaWFuIFRydXN0ZWQgU3Vi
IENBMIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA1j0rkZECOt1S8o7I
JY+4YKAxuEa5xaHKHXT2EpkuC/0krqMOjUy2oPIRNgR5g8X0Jl6jamxeGLc4Q1tf
ju6or9oSRYThIUhRsFDQNBiBBEXoBgWxTfiKB2eyT97+pz5TBtBiRCPaLGRHYLRb
9Jz2HkJlxbtNPjtDrF5DPHym+mZ1M1z3hIQYAqJwLpsEBnsw/VxWMlxqHoeewd0h
uJMd71KQ5vOKlz7KrIZ6EobNNa6wItuvsfj3kYCK7O78uLHGXXFxdr8Hae9lMUmC
8F7AFwa+bO1LRlTlqW7rE3rLf+jj70N01N8T3o22v14YBaFBWQWncAVYD2JuL3tH
252+kdNOERf1fLbLRigJAbd+hOhWYlNf963TFDgnNPliHNIW72SygVBnI2V3JwO1
dp1hVKpK/zt8ziGdHW4gmOLTsH50YKdR4jNqUgQv4wASlKn9OpN6zHYc5G8h86fY
BM+zxE5ikGI+I/vIqBuI0eaDU92AWN/YjFLpu8tMu9kLRSCf1vug6FIfDPWVo7iP
ac/SI2v8jnnpaW7ph/Pz3WkzaG7ZZJsfFs+8dploWc6LOoDtbFBhMdGMxu024msC
0PSjZb5ODXPIaO2NsA7fMiAtZcoK6anTUJh4zOP/stA9qsJGNxdrEmiPXSmBZY/N
Y0wkZgZ6JTDhw7038bPvctkblJkCAwEAAaOCAYswggGHMB0GA1UdDgQWBBR3Pdk5
r0K93FvKduru/c4+YSkwXzAfBgNVHSMEGDAWgBTh0YHlzlpfBKrS6badZrHF+qws
hzAOBgNVHQ8BAf8EBAMCAYYwEgYDVR0TAQH/BAgwBgEB/wIBADCBmAYIKwYBBQUH
AQEEgYswgYgwQAYIKwYBBQUHMAKGNGh0dHA6Ly9udWMtY2RwLnZvc2tob2QucnUv
Y2RwL3Jvb3RjYV9zc2xfcnNhMjAyMi5jcnQwRAYIKwYBBQUHMAKGOGh0dHA6Ly9u
dWMtY2RwLmRpZ2l0YWwuZ292LnJ1L2NkcC9yb290Y2Ffc3NsX3JzYTIwMjIuY3J0
MIGFBgNVHR8EfjB8MDqgOKA2hjRodHRwOi8vbnVjLWNkcC52b3NraG9kLnJ1L2Nk
cC9yb290Y2Ffc3NsX3JzYTIwMjIuY3JsMD6gPKA6hjhodHRwOi8vbnVjLWNkcC5k
aWdpdGFsLmdvdi5ydS9jZHAvcm9vdGNhX3NzbF9yc2EyMDIyLmNybDANBgkqhkiG
9w0BAQsFAAOCAgEAmsINXtQ7wwUWvIeOr80MdJS/5G4xhyZOVEmeUorThquT672y
cCg3XCxc4fwbiZqSSbBqntQ7RtiTAKMYMvBageKoVHbzz+R4jX01tKcTx8cDePrz
dJ73bLNUorE7RU9QsW4KyiUeRmjMDV23AUlEvuQFTwgkHXvbac1BBdPn9CrssQuF
5EGohZKcQPFiAAc4SHbRNhlr7uAwgpc/erzI9EAcvA6BVAXcVKoeGpV01uexUgZ6
St5RP9UmDWNA7T4yVXWJ233N0Q8bl+6AswINQ3PosPu6yQQHQjr65YS06epK+AeI
6j+oGR4xI7EhTQhQvaobnGmX/8QQ7XDRYCP2HXYxiffnn/CfZ/BVyKLYeY1ZipjE
nzqdQIC2+Q3WtY8jsVRQMP38WFRmtsIt5snehnPTs5bKGVIcYzj3o3Ex/K7agEz0
zAJ0JR5ivXZOvNkT0g9x1v+S1IkU3e/nX1a+tpRquMtnHX0L2lXArNHUbaOO9EJt
d57WaIpofV5cVhhwShOgAuBc9UMJF3/n4t4RKiPxtsK8P67gcmphMhslj7AMYrYM
ej2NvQZY4m3ub3CPC/PrTjDONvb+8g5xrKtxBjYqC74HSB4dg9G3WimSDUuP2Su6
G2y2TUeyJuCvCLz289VoO0vg7cNdMobE3KCqAiiNhN2VBFxHAUKmUoRcRdw=
-----END CERTIFICATE-----
"""

_ca_bundle: str | None = None


def ca_bundle() -> str:
    """certifi + сертификат Минцифры одним файлом (кэш на процесс)."""
    global _ca_bundle
    if _ca_bundle is None:
        fd, path = tempfile.mkstemp(prefix="max-ca-", suffix=".pem")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(Path(certifi.where()).read_text(encoding="utf-8"))
            f.write("\n" + MAX_CA_PEM)
        _ca_bundle = path
    return _ca_bundle


class MaxError(RuntimeError):
    pass


def max_call(method: str, path: str, token: str, *, params=None, json=None, files=None,
             url: str | None = None) -> dict:
    headers = {"Authorization": token}
    r = requests.request(method, url or f"{MAX_API}{path}", params=params, json=json,
                         files=files, headers=headers, timeout=TIMEOUT, verify=ca_bundle())
    try:
        data = r.json()
    except ValueError:
        data = {"_raw": r.text[:300]}
    api_error = isinstance(data, dict) and data.get("code") and isinstance(data.get("message"), str)
    if r.status_code >= 400 or api_error:
        raise MaxError(f"{method} {path or url}: HTTP {r.status_code} {str(data)[:300]}")
    return data


# ---------- выбор файлов ----------

def file_date(p: Path) -> str | None:
    m = FILE_DATE_RE.match(p.name)
    return m.group(1) if m else None


def should_publish(post: frontmatter.Post, p: Path, start_date: str) -> bool:
    if post.metadata.get("max_post_id"):
        return False
    channels = post.metadata.get("channels") or []
    if "max" in channels:
        pass
    elif "tg" in channels or not channels:
        if post.metadata.get("max_skip"):
            return False
    else:
        return False
    d = file_date(p)
    return d is not None and d >= start_date


def find_pending(start_date: str) -> list[Path]:
    out = []
    for p in sorted(PUBLISHED_DIR.glob("*.md")):
        try:
            post = frontmatter.load(p)
        except Exception:
            continue
        if should_publish(post, p, start_date):
            out.append(p)
    return out


# ---------- медиа ----------

def _mime(path: Path, fallback: str) -> str:
    """MIME по расширению: без него видеосервер MAX отвечает 400 (проверено 13.11.2026)."""
    return mimetypes.guess_type(path.name)[0] or fallback


def upload_image(token: str, path: Path) -> str:
    slot = max_call("POST", "/uploads", token, params={"type": "image"})
    with path.open("rb") as fh:
        res = max_call("POST", "", token, url=slot["url"],
                       files={"data": (path.name, fh, _mime(path, "image/png"))})
    photos = res.get("photos") or {}
    for v in photos.values():
        if isinstance(v, dict) and v.get("token"):
            return v["token"]
    if res.get("token"):
        return res["token"]
    raise MaxError(f"upload image: no token in response {str(res)[:200]}")


def upload_video(token: str, path: Path) -> str:
    slot = max_call("POST", "/uploads", token, params={"type": "video"})
    vtoken = slot.get("token")
    if not vtoken:
        raise MaxError("upload video: no token in /uploads response")
    with path.open("rb") as fh:
        r = requests.post(slot["url"], files={"data": (path.name, fh, _mime(path, "video/mp4"))},
                          headers={"Authorization": token}, timeout=600, verify=ca_bundle())
    if r.status_code >= 400:
        raise MaxError(f"upload video: HTTP {r.status_code}")
    return vtoken


def build_body(post: frontmatter.Post, text: str, attachment: dict | None) -> dict:
    body: dict = {"text": text, "format": "html", "notify": True}
    atts = []
    if attachment:
        atts.append(attachment)
    btn = post.metadata.get("max_button")
    if isinstance(btn, dict) and str(btn.get("url", "")).startswith("https://") and btn.get("text"):
        atts.append({"type": "inline_keyboard", "payload": {"buttons": [[
            {"type": "link", "text": str(btn["text"])[:128], "url": str(btn["url"])}]]}})
    if atts:
        body["attachments"] = atts
    return body


def send_message(token: str, channel_id: str, body: dict, *, sleep=time.sleep) -> str:
    delays = list(VIDEO_READY_DELAYS)
    while True:
        try:
            res = max_call("POST", "/messages", token, params={"chat_id": channel_id}, json=body)
        except MaxError as e:
            if "attachment.not.ready" in str(e) and delays:
                sleep(delays.pop(0))
                continue
            raise
        mid = ((res.get("message") or {}).get("body") or {}).get("mid")
        if not mid:
            raise MaxError(f"POST /messages: no message.body.mid in {str(res)[:200]}")
        return mid


def publish_one(p: Path, token: str, channel_id: str, *, sleep=time.sleep) -> str:
    post = frontmatter.load(p)
    text = post.content.strip()
    if not text:
        raise ValueError(f"{p.name}: empty body")
    if len(text) > MAX_TEXT_LIMIT:
        raise ValueError(f"{p.name}: body too long for MAX ({len(text)} > {MAX_TEXT_LIMIT})")

    attachment = None
    video_rel, image_rel = post.metadata.get("video"), post.metadata.get("image")
    if video_rel:
        attachment = {"type": "video", "payload": {"token": upload_video(token, REPO_ROOT / video_rel)}}
    elif image_rel:
        attachment = {"type": "image", "payload": {"token": upload_image(token, REPO_ROOT / image_rel)}}

    mid = send_message(token, channel_id, build_body(post, text, attachment), sleep=sleep)
    post.metadata["max_post_id"] = mid
    post.metadata["max_posted_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    with p.open("w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    print(f"✓ {p.name} → MAX channel mid={mid}")
    return mid


def main() -> int:
    token = os.environ.get("MAX_BOT_TOKEN")
    channel_id = os.environ.get("MAX_CHANNEL_ID")
    if not token or not channel_id:
        print("MAX: MAX_BOT_TOKEN / MAX_CHANNEL_ID не заданы — шаг пропущен.")
        return 0
    start = os.environ.get("MAX_START_DATE") or DEFAULT_START_DATE

    pending = find_pending(start)
    if not pending:
        print("MAX: nothing to post.")
        return 0
    print(f"MAX: {len(pending)} file(s) to post:")
    failures = 0
    for p in pending:
        try:
            publish_one(p, token, channel_id)
        except Exception as e:  # noqa: BLE001 — один пост не валит остальные
            sys.stderr.write(f"FAIL {p.name}: {e}\n")
            failures += 1
    if failures:
        sys.stderr.write(f"\n{failures} file(s) failed.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
