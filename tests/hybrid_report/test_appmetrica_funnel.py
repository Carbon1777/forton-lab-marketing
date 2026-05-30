from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.hybrid_report import appmetrica
from src.hybrid_report.models import (
    AppMetricaFunnel,
    AppMetricaScreens,
    FunnelStep,
    ScreenStat,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hybrid_report"
SCREENS = json.loads((FIXTURES / "am_screens.json").read_text(encoding="utf-8"))

W_START = dt.date(2026, 5, 23)
W_END = dt.date(2026, 5, 29)


def _mock_resp(payload: dict, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload
    m.raise_for_status.return_value = None
    return m


# ----------------------------- onboarding funnel -----------------------------

def test_fetch_onboarding_funnel_per_step():
    steps = [("app_open", "открыли"), ("email_submitted", "отправили email")]
    with patch.object(
        appmetrica,
        "fetch_with_retry",
        side_effect=[_mock_resp({"totals": [25.0]}), _mock_resp({"totals": [18.0]})],
    ) as f:
        result = appmetrica.fetch_onboarding_funnel(
            "6301660", steps, W_START, W_END, token="t"
        )
    assert result == AppMetricaFunnel(
        steps=[FunnelStep("открыли", 25), FunnelStep("отправили email", 18)],
        error=None,
    )
    # каждый шаг — отдельный запрос с фильтром по eventLabel и метрикой devices
    calls = f.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["params"]["metrics"] == "ym:ce:devices"
    assert calls[0].kwargs["params"]["filters"] == "ym:ce:eventLabel=='app_open'"
    assert calls[1].kwargs["params"]["filters"] == "ym:ce:eventLabel=='email_submitted'"


def test_fetch_onboarding_funnel_drops_zero_steps():
    # шаг с нулём ОСТАВЛЯЕМ (devices=0) — нужно показать падение
    steps = [("app_open", "открыли"), ("email_submitted", "отправили email")]
    with patch.object(
        appmetrica,
        "fetch_with_retry",
        side_effect=[_mock_resp({"totals": [25.0]}), _mock_resp({"totals": []})],
    ):
        result = appmetrica.fetch_onboarding_funnel(
            "6301660", steps, W_START, W_END, token="t"
        )
    assert result.steps == [
        FunnelStep("открыли", 25),
        FunnelStep("отправили email", 0),
    ]
    assert result.error is None


def test_fetch_onboarding_funnel_degrades_on_error():
    steps = [("app_open", "открыли")]
    with patch.object(
        appmetrica, "fetch_with_retry", side_effect=RuntimeError("boom")
    ):
        result = appmetrica.fetch_onboarding_funnel(
            "6301660", steps, W_START, W_END, token="t"
        )
    assert result.steps == []
    assert result.error is not None
    assert "RuntimeError" in result.error


# ------------------------------- top screens --------------------------------

def test_fetch_top_screens_parses():
    with patch.object(
        appmetrica, "fetch_with_retry", return_value=_mock_resp(SCREENS)
    ) as f:
        result = appmetrica.fetch_top_screens("6301660", W_START, W_END, token="t")
    assert result.error is None
    assert result.screens == [
        ScreenStat("activity_feed", 40, None),
        ScreenStat("plans", 22, None),
        ScreenStat("profile", 15, None),
    ]
    params = f.call_args.kwargs["params"]
    assert params["metrics"] == "ym:ce:devices"
    assert params["dimensions"] == "ym:ce:paramsLevel1"
    assert params["filters"] == "ym:ce:eventLabel=='screen_view'"


def test_fetch_top_screens_degrades_on_error():
    with patch.object(
        appmetrica, "fetch_with_retry", side_effect=ValueError("nope")
    ):
        result = appmetrica.fetch_top_screens("6301660", W_START, W_END, token="t")
    assert result.screens == []
    assert result.error is not None
    assert "ValueError" in result.error


def test_fetch_top_screens_limits_top_n():
    big = {"data": [
        {"dimensions": [{"name": f"s{i}"}], "metrics": [float(100 - i)]}
        for i in range(12)
    ]}
    with patch.object(
        appmetrica, "fetch_with_retry", return_value=_mock_resp(big)
    ):
        result = appmetrica.fetch_top_screens(
            "6301660", W_START, W_END, token="t", top_n=7
        )
    assert len(result.screens) == 7
    # отсортировано по views убыв.: s0 (100) первый
    assert result.screens[0] == ScreenStat("s0", 100, None)
    assert result.screens[-1] == ScreenStat("s6", 94, None)
