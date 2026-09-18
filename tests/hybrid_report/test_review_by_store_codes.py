"""Разбивка запроса оценки по сторам: строковый store (Diktum/Lucea) и int-код
Листвии (152-ФЗ — параметры событий только num/bool) дают одинаковый результат."""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

from src.hybrid_report import appmetrica

W_START = dt.date(2026, 9, 14)
W_END = dt.date(2026, 9, 20)


def _resp(rows: list[tuple[str, float]]) -> MagicMock:
    m = MagicMock()
    m.status_code = 200
    m.raise_for_status.return_value = None
    m.json.return_value = {
        "data": [
            {"dimensions": [{"name": name}], "metrics": [n]} for name, n in rows
        ]
    }
    return m


def _by_store(rows):
    with patch.object(appmetrica, "fetch_with_retry", return_value=_resp(rows)):
        return appmetrica._fetch_review_by_store(
            "6316003", "review_prompt_triggered", W_START, W_END, "t"
        )


def test_listvia_int_codes_map_to_labels_in_priority_order():
    # AppMetrica отдаёт paramsLevel2 строкой, в произвольном порядке
    res = _by_store([("3", 2.0), ("1", 5.0), ("2", 4.0)])
    assert res == [("App Store", 5), ("Google Play", 4), ("RuStore", 2)]


def test_listvia_tail_codes_labelled_not_raw_digits():
    res = _by_store([("4", 1.0), ("7", 3.0), ("0", 2.0)])
    labels = [label for label, _ in res]
    assert "Huawei AppGallery" in labels
    assert "Загрузка вручную" in labels
    assert "Источник неизвестен" in labels
    assert not any(label.isdigit() for label in labels)


def test_string_store_products_unaffected():
    res = _by_store([("rustore", 2.0), ("app_store", 5.0), ("google_play", 4.0)])
    assert res == [("App Store", 5), ("Google Play", 4), ("RuStore", 2)]


def test_int_and_string_same_store_are_summed():
    res = _by_store([("1", 2.0), ("app_store", 3.0)])
    assert res == [("App Store", 5)]


def test_unmapped_code_falls_back_to_raw_value():
    res = _by_store([("99", 1.0)])
    assert res == [("99", 1)]
