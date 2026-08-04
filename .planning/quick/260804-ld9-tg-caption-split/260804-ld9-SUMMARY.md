---
quick_id: 260804-ld9
slug: tg-caption-split
date: 2026-08-04
status: complete
---

# Quick Task 260804-ld9: Split long TG posts — SUMMARY

## What was wrong
Approved post `lapulya-aug4-stress` never reached the channels. The `publish`
workflow ([run 30894608324](https://github.com/Carbon1777/forton-lab-marketing/actions/runs/30894608324),
2026-08-04 12:03 МСК) crashed with `400 Bad Request` on `sendPhoto`: the
caption was **1044 chars** vs Telegram's **1024** limit.

Root cause was a preview↔production divergence: `preview_bot.py` splits a
>1024 body into media + text, but `tg_post.py` only logged a WARN and still
sent the oversized caption. So the preview looked fine while production could
not physically send it.

## What changed
- `src/tg_post.py` — `publish_one` now detects `len(body) > TG_CAPTION_LIMIT`
  and sends the photo/video with an **empty caption** + the full body as a
  follow-up `sendMessage`. Result is visually identical to a normal captioned
  post (image on top, text below). The ≤1024 path is unchanged.
- `tests/test_tg_post_caption_split.py` — new regression suite: long-caption
  photo/video split, unchanged short-caption single-send, and >4096 body still
  hard-fails.

## Verification
- `pytest tests/test_tg_post_caption_split.py tests/test_tg_post_resilience.py
  tests/test_publisher_filters.py` → **24 passed**.
- Full suite: 972 passed. The only failures are pre-existing and unrelated
  (`test_ai_talent_identity_anchor.py`, missing optional AI-talent dep —
  confirmed failing on `git stash` of this change too).
- Fix pushed to `main` (d233e39), then re-dispatched `publish.yml`
  slug=lapulya-aug4-stress → [run 30909606229](https://github.com/Carbon1777/forton-lab-marketing/actions/runs/30909606229)
  **success**: TG `msg_id=109 kind=photo` (no WARN, no FAIL) + VK `post_id=117`.

## Commits
- pre-dispatch plan
- `fix(tg_post): split posts with body >1024 into media + separate text`
- docs summary (this commit)

## Follow-up (not done here)
- The lint step already flags caption >1024 as a warning; consider promoting it
  to an informational note now that production splits automatically, so the
  preview badge reads "will split in prod" rather than implying a defect.
