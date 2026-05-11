"""Phase 5 — Weekly store metrics digest.

Каждый понедельник в 09:37 МСК (06:37 UTC, защита от cron drift MAJ-1)
собираем installs/uninstalls/rating per-product × per-store за прошедшую
неделю + динамику WoW + 4-недельный тренд + алерты при падении >20%.

Architecture:
    asc.py       — Apple Analytics Reports API (JWT ES256)
    play.py      — Google Play Developer Reporting API (service account)
    rustore.py   — RuStore Public API (JWE RSA) с fallback на manual
    snapshot.py  — load/save исторические снимки → vs prev для Δ WoW
    digest.py    — markdown render с emoji, тренды, алерты
    cli.py       — main() entrypoint для store_metrics.yml workflow

Public API:
    collect_all, render_digest, send_to_planner.
"""
from __future__ import annotations
