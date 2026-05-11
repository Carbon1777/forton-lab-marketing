"""LTX Video API client — duplicate of ~/Developer/video-toolkit/tools/ltx_api.py.

PIPE-05: eliminates fragile cross-repo import; ~80 LOC proportionate
(RESEARCH §Pattern 3). Differences from upstream:
    * Key from LTX_API_KEY env var; .env.ltx file fallback.
    * Library function (not CLI); typed exceptions.
    * BOOT-01 wrapped at CALLER side (assemble.py), NOT inside this module —
      mirror of character_designer.py / voice_selector.py shape.
    * Optional image_path → image_base64 conditioning (Q-LTX-IMG resolved
      YES 2026-05-11; see Brain/projects/forton-lab/decisions.md).

Caller pattern (MANDATORY — threat T-11-03-02 mitigation):
    from src.ai_talent._ltx_api import generate, estimate_cost
    from src.spend_tracker_v2 import preflight_check, record_provider_spend
    est = estimate_cost("ltx-2-3-pro", 5, "1080x1920")
    preflight_check(spend_file, "ltx", est)
    data = generate(prompt=..., duration_sec=5, image_path=ref_png)
    out_path.write_bytes(data)
    record_provider_spend(spend_file, "ltx", usd=est, units=5,
                          unit_field="seconds")
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Final

import requests

API_URL: Final[str] = "https://api.ltx.video/v1/text-to-video"
VALID_MODELS: Final[frozenset[str]] = frozenset({
    "ltx-2-fast", "ltx-2-pro", "ltx-2-3-fast", "ltx-2-3-pro",
})
DEFAULT_MODEL: Final[str] = "ltx-2-3-pro"
COST_PER_SEC: Final[dict[str, float]] = {
    "ltx-2-fast": 0.04, "ltx-2-pro": 0.06,
    "ltx-2-3-fast": 0.06, "ltx-2-3-pro": 0.08,
}
DEFAULT_RESOLUTION: Final[str] = "1080x1920"  # 9:16 — TG/VK/Дзен mandate
DEFAULT_FPS: Final[int] = 24

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
DEFAULT_ENV_LTX: Final[Path] = _REPO_ROOT.parent / ".env.ltx"  # video-toolkit


class LtxError(RuntimeError):
    """Generic LTX failure (non-2xx not covered by Auth/Quota subclasses)."""


class LtxAuthError(LtxError):
    """HTTP 401, or LTX_API_KEY missing / lacks `ltxv_` prefix."""


class LtxQuotaError(LtxError):
    """HTTP 402 (payment required) or 429 (rate/quota exceeded)."""


def _read_key() -> str:
    env_key = os.environ.get("LTX_API_KEY", "").strip()
    if env_key:
        if not env_key.startswith("ltxv_"):
            raise LtxAuthError("LTX_API_KEY missing 'ltxv_' prefix")
        return env_key
    if DEFAULT_ENV_LTX.exists():
        file_key = DEFAULT_ENV_LTX.read_text(encoding="utf-8").strip()
        if not file_key.startswith("ltxv_"):
            raise LtxAuthError(f"Key in {DEFAULT_ENV_LTX} missing 'ltxv_' prefix")
        return file_key
    raise LtxAuthError(f"LTX_API_KEY env not set and {DEFAULT_ENV_LTX} missing")


def estimate_cost(model: str, duration_sec: int,
                  resolution: str = DEFAULT_RESOLUTION) -> float:
    """Deterministic USD estimate. Caller uses for preflight_check()."""
    if model not in COST_PER_SEC:
        raise LtxError(f"unknown model {model!r}; valid: {sorted(COST_PER_SEC)}")
    rate = COST_PER_SEC[model]
    mult = 2 if "1440" in resolution else 4 if "2160" in resolution else 1
    return rate * mult * duration_sec


def generate(
    *,
    prompt: str,
    duration_sec: int = 5,
    model: str = DEFAULT_MODEL,
    resolution: str = DEFAULT_RESOLUTION,
    fps: int = DEFAULT_FPS,
    camera_motion: str | None = None,
    image_path: str | Path | None = None,
    generate_audio: bool = False,
    timeout: int = 600,
) -> bytes:
    """Sync text-to-video → mp4 bytes. BOOT-01 wrap is CALLER responsibility."""
    if model not in VALID_MODELS:
        raise LtxError(f"invalid model {model!r}; valid: {sorted(VALID_MODELS)}")
    key = _read_key()
    body: dict = {
        "prompt": prompt, "model": model, "duration": duration_sec,
        "resolution": resolution, "fps": fps, "generate_audio": generate_audio,
    }
    if camera_motion:
        body["camera_motion"] = camera_motion
    if image_path:
        # Q-LTX-IMG resolved YES 2026-05-11: API uses image_base64 conditioning.
        body["image_base64"] = base64.b64encode(
            Path(image_path).read_bytes()
        ).decode("ascii")
    try:
        r = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            data=json.dumps(body), timeout=timeout,
        )
    except requests.RequestException as e:
        raise LtxError(f"network error: {e}") from e
    if r.status_code == 401:
        raise LtxAuthError(f"401 unauthorized: {r.text[:200]}")
    if r.status_code in (402, 429):
        raise LtxQuotaError(f"{r.status_code}: {r.text[:200]}")
    if r.status_code != 200:
        raise LtxError(f"HTTP {r.status_code}: {r.text[:500]}")
    return r.content


__all__ = [
    "generate", "estimate_cost",
    "LtxError", "LtxAuthError", "LtxQuotaError",
    "API_URL", "DEFAULT_MODEL", "DEFAULT_RESOLUTION",
    "VALID_MODELS", "COST_PER_SEC",
]
