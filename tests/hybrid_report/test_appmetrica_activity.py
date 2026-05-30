from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.hybrid_report import appmetrica
from src.hybrid_report.models import AppMetricaActivity

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hybrid_report"
ACTIVITY = json.loads((FIXTURES / "am_activity.json").read_text(encoding="utf-8"))

W_START = dt.date(2026, 5, 23)
W_END = dt.date(2026, 5, 29)


def _mock_resp(payload: dict, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload
    m.raise_for_status.return_value = None
    return m


def test_fetch_activity_parses_totals():
    with patch.object(
        appmetrica, "fetch_with_retry", return_value=_mock_resp(ACTIVITY)
    ) as f:
        result = appmetrica.fetch_activity("6301660", W_START, W_END, token="t")
    assert result == AppMetricaActivity(
        sessions=50, active_users=25, avg_session_sec=54.0
    )
    _, kwargs = f.call_args
    params = kwargs["params"]
    assert params["id"] == "6301660"
    assert params["metrics"] == "ym:s:sessions,ym:s:users,ym:s:avgSessionDuration"
    assert params["accuracy"] == "full"
    assert kwargs["headers"]["Authorization"].startswith("OAuth ")


def test_fetch_activity_empty_totals_zero():
    with patch.object(
        appmetrica, "fetch_with_retry",
        return_value=_mock_resp({"totals": [], "data": []}),
    ):
        result = appmetrica.fetch_activity("6301660", W_START, W_END, token="t")
    assert result == AppMetricaActivity(
        sessions=0, active_users=0, avg_session_sec=0.0
    )


def test_fetch_activity_missing_totals_key_zero():
    with patch.object(
        appmetrica, "fetch_with_retry", return_value=_mock_resp({"data": []})
    ):
        result = appmetrica.fetch_activity("6301660", W_START, W_END, token="t")
    assert result == AppMetricaActivity(
        sessions=0, active_users=0, avg_session_sec=0.0
    )


def test_fetch_activity_passes_token():
    with patch.object(
        appmetrica, "fetch_with_retry", return_value=_mock_resp(ACTIVITY)
    ) as f:
        appmetrica.fetch_activity("6301663", W_START, W_END, token="t")
    _, kwargs = f.call_args
    assert kwargs["headers"]["Authorization"] == "OAuth t"
