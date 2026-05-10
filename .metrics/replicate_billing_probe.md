# Replicate billing endpoint probe

**Date:** 2026-05-10
**Tested by:** Phase 7 Plan 01 (BOOT-05 baseline)
**Token source:** `~/.config/forton-lab/keys.env` → `REPLICATE_API_TOKEN` (length 40, valid — control endpoint вернул 200)

## Verdict

verdict: NO_FALLBACK_TO_CUMULATIVE

Все три предполагаемых billing-endpoint'а вернули `HTTP 404 Not Found` с одинаковым телом
`{"detail":"The requested resource could not be found.","status":404}`. Control endpoint
`GET /v1/account` отдал валидный профиль (`type=user`, `username=carbon1777`) — то есть
авторизация по токену работает; именно сами billing-ресурсы публично недоступны.

Это окончательно подтверждает RESEARCH RQ-1 (MEDIUM-HIGH → HIGH): публичного REST API для
чтения остатка кредитов на Replicate не существует. Pre-flight balance check сделать нельзя.

## Raw results

### GET /v1/billing
HTTP_STATUS: 404
Body:
```
{"detail":"The requested resource could not be found.","status":404}
```

### GET /v1/account/billing
HTTP_STATUS: 404
Body:
```
{"detail":"The requested resource could not be found.","status":404}
```

### GET /v1/account/credits
HTTP_STATUS: 404
Body:
```
{"detail":"The requested resource could not be found.","status":404}
```

### GET /v1/account (control)
HTTP_STATUS: 200
Body:
```
{"type":"user","username":"carbon1777","name":"","avatar_url":"https://github.com/Carbon1777.png","github_url":"https://github.com/Carbon1777"}
```

Контраст показателен: profile-поля есть (`type`, `username`, `name`, `avatar_url`,
`github_url`), но никаких `credit_balance`, `prepaid_credit_usd`, `usage_this_month` —
ровно то, что описано в RESEARCH RQ-1. Replicate Python SDK (`replicate.accounts.current()`)
маппится на тот же endpoint и balance не вернёт.

## Implementation impact (BOOT-05)

verdict == NO_FALLBACK_TO_CUMULATIVE → BOOT-05 переформулирован:

- **НЕ имплементируем `check_replicate_balance(min_required_usd)`** в Plan 02 — endpoint'а нет.
- Plan 02 имплементирует **только** `preflight_check(spend_file, "replicate", est_cost)`,
  который raises `ProviderMonthlyCapExceededError` если cumulative Replicate spend в текущем
  месяце + `est_cost` > $4.00 (proxy для остатка $4.93 кредита, поправленного на безопасный
  буфер).
- User получает alert через уже-существующий weekly digest pattern (Phase 5) —
  строка вида «Replicate spent this month: $X / $4.00. Last verified balance was $Y on
  YYYY-MM-DD — check replicate.com/account/billing».
- Manual balance refresh: раз в неделю юзер заглядывает на UI billing-страницу и обновляет
  `.metrics/replicate_balance_snapshot.json` руками (либо через `make replicate-snapshot`
  если решим автоматизировать через scrape — но это уже out of scope Phase 7).

## Probe reproducibility

Запустить заново можно одной командой:

```bash
source ~/.config/forton-lab/keys.env && \
for ep in /v1/billing /v1/account/billing /v1/account/credits /v1/account; do
  echo "=== GET ${ep} ==="
  curl -sS -w "\nHTTP_STATUS:%{http_code}\n" \
    -H "Authorization: Bearer $REPLICATE_API_TOKEN" \
    "https://api.replicate.com${ep}"
  echo ""
done
```

**Re-verify cadence:** проверять перед каждым новым deploy meter'а (Phase 7 → каждые ~30 дней),
т.к. Replicate ввели prepaid credits в июле 2025 и могут добавить billing API без громкого анонса.

## Decision audit trail

- RESEARCH.md RQ-1 (2026-05-10) — теоретический verdict MEDIUM-HIGH.
- This probe (2026-05-10) — empirical verdict HIGH.
- Plan 02 BOOT-05 branch: `NO_FALLBACK_TO_CUMULATIVE` → cumulative provider-monthly cap warning only.
