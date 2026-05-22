from __future__ import annotations

import datetime as dt

from src.diktum_funnel.digest import render_digest
from src.diktum_funnel.models import FunnelWeek


def _fw(**over) -> FunnelWeek:
    base = dict(
        week_start=dt.date(2026, 5, 12), week_end=dt.date(2026, 5, 18),
        installs_total=18, installs_organic=16, installs_ads=2,
        registrations=4, activated=2,
    )
    base.update(over)
    return FunnelWeek(**base)


def test_digest_contains_funnel_numbers():
    text = render_digest(_fw(), prev=None)
    assert "Diktum" in text
    assert "18" in text          # установки
    assert "4" in text           # регистрации
    assert "2" in text           # активация
    assert "12" in text and "18" in text   # даты периода


def test_digest_renders_conversions():
    text = render_digest(_fw(), prev=None)
    assert "22%" in text         # 4/18
    assert "50%" in text         # 2/4


def test_digest_handles_none_installs():
    text = render_digest(_fw(installs_total=None, installs_organic=None,
                             installs_ads=None, appmetrica_error="401"),
                         prev=None)
    assert "—" in text
    # регистрации всё равно показаны
    assert "4" in text


def test_digest_shows_wow_delta_when_prev_present():
    prev = {"installs_total": 15, "registrations": 5, "activated": 2}
    text = render_digest(_fw(), prev=prev)
    # установки выросли 15→18 → стрелка вверх
    assert "📈" in text
