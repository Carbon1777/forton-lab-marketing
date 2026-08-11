---
quick_id: 260811-w4d
slug: watchdog-auth-diagnosis
date: 2026-08-11
status: complete
---

# Quick Task 260811-w4d: Watchdog diagnoses a dead PAT — SUMMARY

## What was wrong
Approved post `diktum-aug11-nedoslushivayut` never published. The approve
button's `publish.yml` dispatch and the watchdog's `preview_bot.yml` dispatch
both returned `HTTP 401 Bad credentials` — the shared `BOT_DISPATCH_PAT`
secret (fine-grained PAT, 90-day life) had expired / been revoked.

Worse, `preview_watchdog._alert` appended the reassuring footer *"GH throttling
— known behavior, not a bug, watchdog recovers"* on **every** failure, so the
alarm told the operator everything was fine while the credential was dead. The
real, action-required cause (rotate the secret) was hidden.

## What changed
- `src/preview_watchdog.py` — new `_is_auth_failure(err)` detects `HTTP 401` /
  `HTTP 403` / `bad credentials` in a dispatch error. `_alert` now branches: on
  an auth failure it sends `🔴 Watchdog: BOT_DISPATCH_PAT мёртв` stating the
  token expired/was revoked, that it is NOT throttling, that the approve button
  + auto-preview are down until rotation, and the exact fix command
  `gh secret set BOT_DISPATCH_PAT -R <owner>/<repo>`. Real throttles
  (dispatch OK) and non-auth failures (500/timeout) keep the old wording.
- `tests/test_preview_watchdog.py` — added `_is_auth_failure` parametrized test
  + a `main()` auth-failure alert test; repointed the generic-failure test to
  `HTTP 500` so the two paths are covered distinctly.

## Verification
- `pytest tests/test_preview_watchdog.py` → **16 passed**.
- Post published: workflow run 31485048780 succeeded; commit
  `9ca7e66 auto: publish diktum-aug11-nedoslushivayut`.
- Fix on main: `9b7f72a`.

## NOT fixed here (operator action — credential handling)
The **root cause** — the expired `BOT_DISPATCH_PAT` — must be rotated by the
operator (Claude may not generate or enter tokens). Create a new fine-grained
PAT for `Carbon1777/forton-lab-marketing` (Actions: RW, Contents: RW) and set
it: `gh secret set BOT_DISPATCH_PAT -R Carbon1777/forton-lab-marketing`. Until
then the TG approve button stays down; sending posts must go through a manual
`gh workflow run publish.yml -f slug=<slug>`.

## Follow-up (not done here)
- CLAUDE.md Q6 planned a proactive PAT-expiry nudge (`.github/pat_expires.txt`
  + reminder 14 days before expiry) — never implemented. Worth a separate quick
  task so the token never dies silently again.
