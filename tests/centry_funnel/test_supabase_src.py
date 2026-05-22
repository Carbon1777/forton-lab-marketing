from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.centry_funnel import supabase_src

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "centry_funnel"
RPC = json.loads((FIXTURES / "supabase_rpc.json").read_text(encoding="utf-8"))


def _mock_resp(payload, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload
    m.raise_for_status.return_value = None
    return m


def test_fetch_funnel_parses_single_row():
    with patch.object(supabase_src, "fetch_with_retry", return_value=_mock_resp(RPC)) as f:
        result = supabase_src.fetch_funnel(
            dt.date(2026, 5, 11), dt.date(2026, 5, 17),
            url="https://x.supabase.co", key="k",
        )
    assert result.new_profiles == 5
    assert result.guests == 0
    assert result.users == 5
    assert result.activations == 1
    _, kwargs = f.call_args
    assert kwargs["method"] == "POST"
    assert kwargs["json_body"] == {"p_from": "2026-05-11", "p_to": "2026-05-17"}
    assert kwargs["headers"]["apikey"] == "k"


def test_fetch_funnel_empty_is_zero():
    with patch.object(supabase_src, "fetch_with_retry", return_value=_mock_resp([])):
        result = supabase_src.fetch_funnel(
            dt.date(2026, 5, 11), dt.date(2026, 5, 17),
            url="https://x.supabase.co", key="k",
        )
    assert result.new_profiles == 0
    assert result.guests == 0
    assert result.users == 0
    assert result.activations == 0
