"""Phase 8 TG preview delivery — sends 3 media groups + 1 keyboard message.

Raw `requests` HTTP (mirrors tg_post.py). NOT python-telegram-bot — this is a
one-shot emitter run from the local Cowork session, no event loop needed.

Anti-replay: callback_data carries batch_sha8 (CAT-2 pattern from Phase 1.5/2).
The callback handler (skill in Plan 03 + character_selector in Plan 04) is
responsible for recomputing sha8 on receipt and rejecting on mismatch.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import requests

TG_API_BASE = "https://api.telegram.org"


def compute_batch_sha8(paths: list[Path]) -> str:
    """First 8 hex chars of sha256 over concatenated bytes of sorted file list."""
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: str(x)):
        h.update(Path(p).read_bytes())
    return h.hexdigest()[:8]


def build_callback_data(action: str, variant_id: str | None, batch_sha8: str) -> str:
    """Build TG inline-button callback_data.

    Formats:
      - select: `char_select:variant_N:{sha8}`
      - regen:  `char_regen:{sha8}`
      - cancel: `char_cancel:{sha8}`
    """
    if action == "select":
        if variant_id is None:
            raise ValueError("variant_id required for select action")
        return f"char_select:{variant_id}:{batch_sha8}"
    if action in ("regen", "cancel"):
        return f"char_{action}:{batch_sha8}"
    raise ValueError(f"unknown action: {action}")


def _post(url: str, **kwargs) -> dict:
    r = requests.post(url, **kwargs)
    r.raise_for_status()
    return r.json()


def _send_label(token: str, chat_id: str, label: str) -> dict:
    return _post(
        f"{TG_API_BASE}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": f"<b>{label}</b>", "parse_mode": "HTML"},
        timeout=30,
    )


def _send_media_group(token: str, chat_id: str, paths: list[Path], caption: str) -> dict:
    if not 2 <= len(paths) <= 10:
        raise ValueError(f"sendMediaGroup requires 2-10 photos; got {len(paths)}")
    media: list[dict] = []
    files: dict = {}
    opened = []
    try:
        for i, p in enumerate(paths):
            key = f"file{i}"
            item: dict = {"type": "photo", "media": f"attach://{key}"}
            if i == 0:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)
            fh = Path(p).open("rb")
            opened.append(fh)
            files[key] = (Path(p).name, fh, "image/png")
        data = {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)}
        return _post(
            f"{TG_API_BASE}/bot{token}/sendMediaGroup",
            data=data,
            files=files,
            timeout=120,
        )
    finally:
        for fh in opened:
            try:
                fh.close()
            except Exception:
                pass


def _send_keyboard(token: str, chat_id: str, batch_sha8: str) -> dict:
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Вариант 1", "callback_data": build_callback_data("select", "variant_1", batch_sha8)},
                {"text": "✅ Вариант 2", "callback_data": build_callback_data("select", "variant_2", batch_sha8)},
                {"text": "✅ Вариант 3", "callback_data": build_callback_data("select", "variant_3", batch_sha8)},
            ],
            [
                {"text": "🔄 Перегенерить все", "callback_data": build_callback_data("regen", None, batch_sha8)},
                {"text": "❌ Отмена", "callback_data": build_callback_data("cancel", None, batch_sha8)},
            ],
        ]
    }
    return _post(
        f"{TG_API_BASE}/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "Выбери типаж:",
            "parse_mode": "HTML",
            "reply_markup": json.dumps(keyboard, ensure_ascii=False),
        },
        timeout=30,
    )


# Default variant labels — overridable by caller for custom vibes
DEFAULT_VARIANT_LABELS = {
    "variant_1": "Вариант 1 — Lifestyle красотка-брюнетка",
    "variant_2": "Вариант 2 — Sophisticated business-casual",
    "variant_3": "Вариант 3 — Tomboy creative urban",
}


def send_preview_batch(
    token: str,
    chat_id: str,
    variants: dict[str, list[Path]],
    *,
    batch_sha8: str,
    labels: dict[str, str] | None = None,
) -> dict:
    """Emit full preview: header label + media group per variant, then keyboard.

    Args:
        token: TG_PREVIEW_BOT_TOKEN.
        chat_id: TG_OWNER_CHAT_ID (string — Bot API tolerates int or str).
        variants: {"variant_1": [4 paths], "variant_2": [4 paths], "variant_3": [4 paths]}
        batch_sha8: precomputed sha8 of all 12 paths (use compute_batch_sha8).
        labels: optional per-variant label override.

    Returns:
        {"sent_messages": int, "batch_sha8": str}
    """
    labels = labels or DEFAULT_VARIANT_LABELS
    sent = 0
    for variant_id in ("variant_1", "variant_2", "variant_3"):
        paths = variants[variant_id]
        label = labels[variant_id]
        _send_label(token, chat_id, label)
        sent += 1
        _send_media_group(token, chat_id, paths, caption=label)
        sent += 1
    _send_keyboard(token, chat_id, batch_sha8)
    sent += 1
    return {"sent_messages": sent, "batch_sha8": batch_sha8}
