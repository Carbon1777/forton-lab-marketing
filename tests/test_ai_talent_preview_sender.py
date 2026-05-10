"""Tests for ai_talent.preview_sender — Phase 8 Plan 02.

Mocks `requests.post` via monkeypatch on the symbol imported into preview_sender.
Asserts HTTP call counts, payload shape, callback_data format, sha8 determinism.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.ai_talent import preview_sender as ps


# --- helpers --------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {"ok": True}

    def raise_for_status(self):
        return None

    def json(self) -> dict:
        return self._payload


class Recorder:
    """Captures every requests.post call: (url, kwargs)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, **kwargs):
        # Drain file handles so caller's `with`/finally doesn't error,
        # but keep their names/positions for assertions.
        captured = {"data": kwargs.get("data"), "json": kwargs.get("json")}
        files = kwargs.get("files")
        if files:
            captured_files: dict = {}
            for k, v in files.items():
                # v is (name, fh, mime) — read & close
                name, fh, mime = v
                try:
                    content = fh.read()
                except Exception:
                    content = b""
                captured_files[k] = {"name": name, "mime": mime, "size": len(content)}
            captured["files"] = captured_files
        captured["timeout"] = kwargs.get("timeout")
        self.calls.append((url, captured))
        return FakeResponse()


def _make_pngs(tmp_path: Path, count: int, prefix: str = "v") -> list[Path]:
    paths = []
    for i in range(count):
        p = tmp_path / f"{prefix}_{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]) * 32)
        paths.append(p)
    return paths


# --- Test 1: sha8 deterministic + sensitive to content --------------------- #


def test_compute_batch_sha8_deterministic(tmp_path: Path):
    paths = _make_pngs(tmp_path, 3)
    sha_a = ps.compute_batch_sha8(paths)
    sha_b = ps.compute_batch_sha8(list(reversed(paths)))  # order-independent (sorted)

    assert sha_a == sha_b
    assert len(sha_a) == 8
    assert all(c in "0123456789abcdef" for c in sha_a)

    # Recompute manually to lock the formula
    expected = hashlib.sha256()
    for p in sorted(paths, key=str):
        expected.update(p.read_bytes())
    assert sha_a == expected.hexdigest()[:8]

    # Change 1 byte in 1 file → sha changes
    paths[0].write_bytes(paths[0].read_bytes() + b"\x00")
    sha_c = ps.compute_batch_sha8(paths)
    assert sha_c != sha_a


# --- Test 2: callback_data format + 64-byte limit -------------------------- #


def test_build_callback_data_format():
    assert ps.build_callback_data("select", "variant_2", "abcd1234") == "char_select:variant_2:abcd1234"
    assert ps.build_callback_data("regen", None, "abcd1234") == "char_regen:abcd1234"
    assert ps.build_callback_data("cancel", None, "abcd1234") == "char_cancel:abcd1234"

    # 64-byte TG limit
    for action, vid in (("select", "variant_3"), ("regen", None), ("cancel", None)):
        cb = ps.build_callback_data(action, vid, "abcd1234")
        assert len(cb.encode("utf-8")) <= 64, f"{cb!r} exceeds 64 bytes"

    with pytest.raises(ValueError):
        ps.build_callback_data("select", None, "abcd1234")
    with pytest.raises(ValueError):
        ps.build_callback_data("bogus", None, "abcd1234")


# --- Test 3: send_preview_batch issues correct number of POSTs ------------- #


def test_send_preview_batch_calls_count(tmp_path: Path, monkeypatch):
    variants = {
        "variant_1": _make_pngs(tmp_path / "v1", 4, "a") if (tmp_path / "v1").mkdir() or True else [],
        "variant_2": _make_pngs(tmp_path / "v2", 4, "b") if (tmp_path / "v2").mkdir() or True else [],
        "variant_3": _make_pngs(tmp_path / "v3", 4, "c") if (tmp_path / "v3").mkdir() or True else [],
    }
    recorder = Recorder()
    monkeypatch.setattr(ps.requests, "post", recorder)

    result = ps.send_preview_batch("TOKEN", "12345", variants, batch_sha8="abcd1234")

    assert result == {"sent_messages": 7, "batch_sha8": "abcd1234"}
    assert len(recorder.calls) == 7

    # Sequence: label, mediaGroup, label, mediaGroup, label, mediaGroup, keyboard
    urls = [u for u, _ in recorder.calls]
    expected_suffixes = [
        "/sendMessage",       # label v1
        "/sendMediaGroup",    # v1
        "/sendMessage",       # label v2
        "/sendMediaGroup",    # v2
        "/sendMessage",       # label v3
        "/sendMediaGroup",    # v3
        "/sendMessage",       # keyboard
    ]
    for url, suffix in zip(urls, expected_suffixes):
        assert url.endswith(suffix), f"{url} should end with {suffix}"
    # All URLs share `/botTOKEN/`
    assert all("/botTOKEN/" in u for u in urls)


# --- Test 4: sendMediaGroup payload shape ---------------------------------- #


def test_send_media_group_payload_shape(tmp_path: Path, monkeypatch):
    variants = {
        "variant_1": _make_pngs(tmp_path / "v1", 4, "a") if (tmp_path / "v1").mkdir() or True else [],
        "variant_2": _make_pngs(tmp_path / "v2", 4, "b") if (tmp_path / "v2").mkdir() or True else [],
        "variant_3": _make_pngs(tmp_path / "v3", 4, "c") if (tmp_path / "v3").mkdir() or True else [],
    }
    recorder = Recorder()
    monkeypatch.setattr(ps.requests, "post", recorder)

    ps.send_preview_batch("TOKEN", "12345", variants, batch_sha8="abcd1234")

    # First sendMediaGroup is call index 1
    url, captured = recorder.calls[1]
    assert url.endswith("/sendMediaGroup")
    data = captured["data"]
    assert data is not None
    assert data["chat_id"] == "12345"
    media = json.loads(data["media"])
    assert isinstance(media, list) and len(media) == 4
    # First item has caption + parse_mode
    assert media[0]["type"] == "photo"
    assert media[0]["media"] == "attach://file0"
    assert "caption" in media[0] and media[0]["caption"].startswith("Вариант 1")
    assert media[0]["parse_mode"] == "HTML"
    # Items 1-3 have no caption
    for i in (1, 2, 3):
        assert media[i]["type"] == "photo"
        assert media[i]["media"] == f"attach://file{i}"
        assert "caption" not in media[i]

    # files dict has file0..file3
    files = captured["files"]
    assert set(files.keys()) == {"file0", "file1", "file2", "file3"}
    for k, v in files.items():
        assert v["mime"] == "image/png"
        assert v["size"] > 0


# --- Test 5: keyboard payload — 5 buttons, all carry sha8 ------------------ #


def test_keyboard_payload_has_5_buttons_with_sha8(tmp_path: Path, monkeypatch):
    variants = {
        "variant_1": _make_pngs(tmp_path / "v1", 4, "a") if (tmp_path / "v1").mkdir() or True else [],
        "variant_2": _make_pngs(tmp_path / "v2", 4, "b") if (tmp_path / "v2").mkdir() or True else [],
        "variant_3": _make_pngs(tmp_path / "v3", 4, "c") if (tmp_path / "v3").mkdir() or True else [],
    }
    recorder = Recorder()
    monkeypatch.setattr(ps.requests, "post", recorder)

    ps.send_preview_batch("TOKEN", "12345", variants, batch_sha8="deadbeef")

    url, captured = recorder.calls[-1]  # final POST = keyboard
    assert url.endswith("/sendMessage")
    payload = captured["json"]
    assert payload is not None
    assert payload["chat_id"] == "12345"
    assert payload["text"] == "Выбери типаж:"
    keyboard = json.loads(payload["reply_markup"])
    rows = keyboard["inline_keyboard"]
    assert len(rows) == 2
    assert len(rows[0]) == 3  # 3 select
    assert len(rows[1]) == 2  # regen + cancel

    # Row 1: select_1..3
    for i, btn in enumerate(rows[0], start=1):
        assert btn["callback_data"] == f"char_select:variant_{i}:deadbeef"
    # Row 2: regen + cancel
    assert rows[1][0]["callback_data"] == "char_regen:deadbeef"
    assert rows[1][1]["callback_data"] == "char_cancel:deadbeef"

    # All 5 callback_data end with sha8
    all_btns = rows[0] + rows[1]
    assert len(all_btns) == 5
    for btn in all_btns:
        assert btn["callback_data"].endswith(":deadbeef")


# --- Test 6 (bonus): HTTP failure propagates ------------------------------- #


def test_http_failure_propagates(tmp_path: Path, monkeypatch):
    variants = {
        "variant_1": _make_pngs(tmp_path / "v1", 4, "a") if (tmp_path / "v1").mkdir() or True else [],
        "variant_2": _make_pngs(tmp_path / "v2", 4, "b") if (tmp_path / "v2").mkdir() or True else [],
        "variant_3": _make_pngs(tmp_path / "v3", 4, "c") if (tmp_path / "v3").mkdir() or True else [],
    }

    class FailingResp(FakeResponse):
        def raise_for_status(self):
            import requests as _r
            raise _r.HTTPError("simulated 500")

    def failing_post(url, **kwargs):
        # Drain files to avoid resource leaks on the call we abort
        files = kwargs.get("files") or {}
        for _, v in files.items():
            try:
                v[1].read()
                v[1].close()
            except Exception:
                pass
        return FailingResp()

    monkeypatch.setattr(ps.requests, "post", failing_post)

    import requests
    with pytest.raises(requests.HTTPError):
        ps.send_preview_batch("TOKEN", "12345", variants, batch_sha8="abcd1234")
