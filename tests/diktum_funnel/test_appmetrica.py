from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.diktum_funnel import appmetrica

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "diktum_funnel"
INSTALLS = json.loads((FIXTURES / "appmetrica_installs.json").read_text(encoding="utf-8"))


def _mock_resp(payload: dict, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload
    m.raise_for_status.return_value = None
    return m


def test_fetch_installs_splits_organic_and_ads():
    with patch.object(appmetrica, "fetch_with_retry", return_value=_mock_resp(INSTALLS)) as f:
        result = appmetrica.fetch_installs(
            dt.date(2026, 5, 12), dt.date(2026, 5, 18), token="t"
        )
    assert result.total == 18
    assert result.organic == 16
    assert result.ads == 2
    assert result.by_publisher["VK Ads (ex. myTarget)"] == 2
    # проверяем, что запрос ушёл с правильными метрикой и app id
    _, kwargs = f.call_args
    assert kwargs["params"]["id"] == "6301663"
    assert kwargs["params"]["metrics"] == "ym:ts:installDevices"
    assert kwargs["params"]["dimensions"] == "ym:ts:publisher"


def test_fetch_installs_empty_data_is_zero():
    with patch.object(appmetrica, "fetch_with_retry", return_value=_mock_resp({"data": []})):
        result = appmetrica.fetch_installs(dt.date(2026, 5, 12), dt.date(2026, 5, 18), token="t")
    assert result.total == 0
    assert result.organic == 0
    assert result.ads == 0
