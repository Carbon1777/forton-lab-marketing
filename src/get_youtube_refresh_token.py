"""One-time helper: get a YouTube refresh_token for the publisher pipeline.

Run this LOCALLY on your machine (not in CI), once. It opens a browser to
the Google OAuth consent screen, asks for `youtube.upload` scope, and prints
the resulting refresh_token to stdout. Copy that value into the GitHub Secret
`YT_REFRESH_TOKEN`. Also copy `YT_CLIENT_ID` and `YT_CLIENT_SECRET` from your
Google Cloud OAuth Client (type: Desktop app) into the matching secrets.

Usage:
    pip install google-auth-oauthlib
    YT_CLIENT_ID=... YT_CLIENT_SECRET=... python src/get_youtube_refresh_token.py

The script uses a loopback redirect (127.0.0.1:<random_port>); Google's OAuth
flow for Desktop app type supports loopback. The browser will open
automatically; after you grant access, the local server captures the auth
code and exchanges it for tokens.

Output (stdout):
    YT_REFRESH_TOKEN=<value>
    (also prints access_token and expiry for debugging)

Prerequisites in Google Cloud Console (one-off):
  1. Create / pick a project.
  2. APIs & Services → Library → enable "YouTube Data API v3".
  3. APIs & Services → OAuth consent screen → External, fill required fields,
     add yourself as a test user (or publish the app to skip the unverified
     warning, but it's not required for personal use with refresh tokens
     valid for ~6 months in test mode).
  4. APIs & Services → Credentials → Create Credentials → OAuth client ID:
     application type "Desktop app", any name. Copy client_id and client_secret.
"""

from __future__ import annotations

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    client_id = os.environ.get("YT_CLIENT_ID")
    client_secret = os.environ.get("YT_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.stderr.write(
            "ERROR: set YT_CLIENT_ID and YT_CLIENT_SECRET as env vars before running.\n"
            "Example:\n"
            "  YT_CLIENT_ID=xxx.apps.googleusercontent.com \\\n"
            "  YT_CLIENT_SECRET=GOCSPX-xxx \\\n"
            "  python src/get_youtube_refresh_token.py\n"
        )
        return 1

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, YT_SCOPES)
    # access_type=offline + prompt=consent ensures we always get a refresh_token,
    # even if the user has already granted consent before.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="Открываю браузер для входа в Google...",
        success_message="Готово. Можно закрыть вкладку и вернуться в терминал.",
    )

    if not creds.refresh_token:
        sys.stderr.write(
            "ERROR: refresh_token не получен. Возможно, scope уже был выдан "
            "и Google отдал только access_token. Удали приложение из "
            "https://myaccount.google.com/permissions и запусти скрипт снова.\n"
        )
        return 1

    print()
    print("=" * 60)
    print("Скопируй это значение в GitHub Secret YT_REFRESH_TOKEN:")
    print()
    print(creds.refresh_token)
    print()
    print("=" * 60)
    print(f"(access_token: {creds.token[:24]}…, expires: {creds.expiry})")
    print()
    print("Также убедись, что в GitHub Secrets лежат:")
    print(f"  YT_CLIENT_ID     = {client_id}")
    print(f"  YT_CLIENT_SECRET = {client_secret[:8]}… (не публикую целиком)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
