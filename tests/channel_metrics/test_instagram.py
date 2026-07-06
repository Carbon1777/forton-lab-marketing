"""Instagram fetcher: успех, IG-блок (graceful), отключение, строка дайджеста."""
import datetime as dt

from src.channel_metrics import instagram
from src.channel_metrics.digest import _render_row
from src.channel_metrics.models import PLATFORM_ORDER, PLATFORM_LABEL

_WEEK = dt.date(2026, 7, 6)


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_instagram_in_platform_order_and_label():
    assert "instagram" in PLATFORM_ORDER
    assert PLATFORM_LABEL["instagram"] == "🟣 Instagram"


def test_success_parses_follower_count(monkeypatch):
    payload = {"data": {"user": {"edge_followed_by": {"count": 42}}}}
    monkeypatch.setattr(instagram, "fetch_with_retry",
                        lambda *a, **k: _FakeResp(200, payload))
    snap = instagram.fetch_weekly(_WEEK)
    assert snap.platform == "instagram"
    assert snap.subscribers == 42
    assert snap.error is None
    assert _render_row(snap, None) == "🟣 Instagram   42 подписчиков  —"


def test_ig_block_degrades_softly(monkeypatch):
    """429/401 от IG (частый случай на CI-IP) → error-снапшот, не падает."""
    monkeypatch.setattr(instagram, "fetch_with_retry",
                        lambda *a, **k: _FakeResp(429))
    snap = instagram.fetch_weekly(_WEEK)
    assert snap.subscribers is None
    assert "429" in snap.error
    # дайджест рендерит как «нет данных», строка не ломается
    assert "🟣 Instagram" in _render_row(snap, None)


def test_exception_degrades_softly(monkeypatch):
    def _boom(*a, **k):
        raise ConnectionError("dns")
    monkeypatch.setattr(instagram, "fetch_with_retry", _boom)
    snap = instagram.fetch_weekly(_WEEK)
    assert snap.subscribers is None
    assert "IG optional" in snap.error


def test_empty_username_disables(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_USERNAME", "")
    snap = instagram.fetch_weekly(_WEEK)
    assert snap.subscribers is None
    assert "disabled" in snap.error


def test_delta_renders_when_prev_exists(monkeypatch):
    payload = {"data": {"user": {"edge_followed_by": {"count": 10}}}}
    monkeypatch.setattr(instagram, "fetch_with_retry",
                        lambda *a, **k: _FakeResp(200, payload))
    cur = instagram.fetch_weekly(_WEEK)
    from src.channel_metrics.models import ChannelSnapshot
    prev = ChannelSnapshot(platform="instagram", week_start=_WEEK, subscribers=5)
    assert _render_row(cur, prev) == "🟣 Instagram   10 подписчиков  (+5 📈)"
