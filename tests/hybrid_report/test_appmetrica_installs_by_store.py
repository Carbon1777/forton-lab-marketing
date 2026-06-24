from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

from src.hybrid_report import appmetrica
from src.hybrid_report.appmetrica import InstallsByStore

W_START = dt.date(2026, 6, 15)
W_END = dt.date(2026, 6, 21)

# Реальная форма ответа AppMetrica (Diktum 6301663, 15–21 июня) — id стабильны,
# name зависит от lang; маппим по id. Порядок строк в ответе НЕ отсортирован.
LIVE_PAYLOAD = {
    "data": [
        {"dimensions": [{"id": "ios", "name": "iOS"}], "metrics": [47.0]},
        {"dimensions": [{"id": "com.android.vending", "name": "Google Play"}],
         "metrics": [19.0]},
        {"dimensions": [{"id": "android", "name": "Unknown Android"}],
         "metrics": [8.0]},
        {"dimensions": [{"id": "com.sec.android.app.samsungapps",
                         "name": "Galaxy Apps"}], "metrics": [3.0]},
        {"dimensions": [{"id": "ru.vk.store", "name": "RuStore"}],
         "metrics": [3.0]},
    ],
    "totals": [80.0],
}


def _mock_resp(payload: dict, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload
    m.raise_for_status.return_value = None
    return m


def test_installs_by_store_maps_ids_and_orders():
    with patch.object(
        appmetrica, "fetch_with_retry", return_value=_mock_resp(LIVE_PAYLOAD)
    ) as f:
        res = appmetrica.fetch_installs_by_store(
            "6301663", W_START, W_END, token="t"
        )
    assert isinstance(res, InstallsByStore)
    # маппинг id → витринное имя + порядок (приоритет основных, прочее по убыв.)
    assert res.rows == [
        ("App Store", 47),
        ("Google Play", 19),
        ("RuStore", 3),
        ("Galaxy Store", 3),
        ("Android (источник неизвестен)", 8),
    ]
    assert res.total == 80
    # правильные параметры запроса (надёжный источник)
    _, kwargs = f.call_args
    params = kwargs["params"]
    assert params["metrics"] == "ym:ts:installDevices"
    assert params["dimensions"] == "ym:ts:appInstaller"
    assert params["accuracy"] == "full"
    assert params["id"] == "6301663"


def test_installs_by_store_unknown_installer_falls_back_to_name():
    payload = {
        "data": [
            {"dimensions": [{"id": "ios", "name": "iOS"}], "metrics": [10.0]},
            {"dimensions": [{"id": "org.fdroid.fdroid", "name": "F-Droid"}],
             "metrics": [2.0]},
        ],
        "totals": [12.0],
    }
    with patch.object(
        appmetrica, "fetch_with_retry", return_value=_mock_resp(payload)
    ):
        res = appmetrica.fetch_installs_by_store(
            "6301663", W_START, W_END, token="t"
        )
    # незнакомый installer показывается под своим name, не теряется
    assert ("App Store", 10) in res.rows
    assert ("F-Droid", 2) in res.rows
    assert res.total == 12


def test_installs_by_store_groups_sideload_into_unknown_android():
    # системный установщик + песочница + «android» → один бакет, суммируются
    payload = {
        "data": [
            {"dimensions": [{"id": "android", "name": "Unknown Android"}],
             "metrics": [21.0]},
            {"dimensions": [{"id": "com.google.android.packageinstaller",
                             "name": "GooglePackageInstaller"}], "metrics": [1.0]},
            {"dimensions": [{"id": "com.gbox.android", "name": "com.gbox.android"}],
             "metrics": [1.0]},
            {"dimensions": [{"id": "com.android.vending", "name": "Google Play"}],
             "metrics": [26.0]},
        ],
        "totals": [49.0],
    }
    with patch.object(
        appmetrica, "fetch_with_retry", return_value=_mock_resp(payload)
    ):
        res = appmetrica.fetch_installs_by_store(
            "6301663", W_START, W_END, token="t"
        )
    # три технических id слились в один «Android (источник неизвестен)» = 23
    assert ("Android (источник неизвестен)", 23) in res.rows
    assert ("Google Play", 26) in res.rows
    # один бакет неизвестного андроида, не три отдельные строки
    unknown_rows = [r for r in res.rows if r[0] == "Android (источник неизвестен)"]
    assert len(unknown_rows) == 1
    assert res.total == 49


def test_installs_by_store_empty_data():
    with patch.object(
        appmetrica, "fetch_with_retry",
        return_value=_mock_resp({"data": [], "totals": []}),
    ):
        res = appmetrica.fetch_installs_by_store(
            "6301663", W_START, W_END, token="t"
        )
    assert res.rows == []
    assert res.total == 0
