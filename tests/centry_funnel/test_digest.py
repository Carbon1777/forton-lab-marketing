from __future__ import annotations

import datetime as dt

from src.centry_funnel.digest import render_digest
from src.centry_funnel.models import FunnelWeek


def _fw(**over) -> FunnelWeek:
    base = dict(
        week_start=dt.date(2026, 5, 12), week_end=dt.date(2026, 5, 18),
        installs_total=18, installs_organic=16, installs_ads=2,
        new_profiles=7, guests=5, users=2, activations=1,
    )
    base.update(over)
    return FunnelWeek(**base)


def test_digest_contains_funnel_numbers():
    text = render_digest(_fw(), prev=None)
    assert "Centry" in text
    assert "18" in text          # установки
    assert "7" in text           # новые профили
    assert "гости 5" in text
    assert "регистрации 2" in text
    assert "12" in text and "18" in text   # даты периода


def test_digest_renders_conversion():
    text = render_digest(_fw(), prev=None)
    assert "39%" in text         # 7/18


def test_digest_handles_none_installs():
    text = render_digest(_fw(installs_total=None, installs_organic=None,
                             installs_ads=None, appmetrica_error="401"),
                         prev=None)
    assert "—" in text
    assert "7" in text           # профили всё равно показаны


def test_digest_shows_wow_delta_when_prev_present():
    prev = {"installs_total": 15, "new_profiles": 5, "activations": 0}
    text = render_digest(_fw(), prev=prev)
    assert "📈" in text          # установки выросли 15→18
