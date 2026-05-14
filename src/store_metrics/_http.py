"""Shared HTTP retry helper for Phase 5 store_metrics fetchers.

Per D-5-05: 3 retries with exp backoff (5/15/45s) for transient errors.
"""
from __future__ import annotations

import sys
import time
from typing import Final

import requests

REQUEST_TIMEOUT_S: Final[int] = 30


def fetch_with_retry(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    params: dict | None = None,
    json_body: dict | None = None,
    data: str | dict | None = None,
    timeout: int = REQUEST_TIMEOUT_S,
    max_retries: int = 3,
    base_delay: float = 5.0,
) -> requests.Response:
    """3-retry exp backoff (5/15/45s).

    Retries on: 5xx, 429, ConnectionError, Timeout. Does NOT retry on 4xx (except 429).

    Raises:
        requests.HTTPError on final non-2xx after retries.
        requests.RequestException on connection failure after retries.
    """
    attempt = 0
    last_exc: Exception | None = None
    while attempt < max_retries:
        attempt += 1
        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_body,
                data=data,
                timeout=timeout,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt >= max_retries:
                raise
            sys.stderr.write(f"WARN: retry {attempt}/{max_retries}: {exc!r}\n")
            time.sleep(base_delay * (3 ** (attempt - 1)))
            continue

        # Retryable status codes: 5xx + 429
        if 500 <= resp.status_code < 600 or resp.status_code == 429:
            if attempt >= max_retries:
                resp.raise_for_status()
                return resp  # unreachable, raise_for_status raises
            sys.stderr.write(
                f"WARN: retry {attempt}/{max_retries}: status {resp.status_code}\n"
            )
            time.sleep(base_delay * (3 ** (attempt - 1)))
            continue

        # 4xx (≠ 429) — hard error, but DO NOT raise — caller (_asc/_play/_rustore)
        # decides fetch_status="down" vs partial vs "report_not_ready" (404 case).
        if 400 <= resp.status_code < 500:
            return resp

        # 2xx success
        return resp

    # Если вышли по max_retries без response (всё было ConnectionError/Timeout)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"fetch_with_retry: unexpected fall-through ({url})")
