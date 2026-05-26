# forton-cron-trigger — Cloudflare Worker

Внешний cron-триггер для GH Actions workflow'ов. Решает проблему **GH Actions
cron throttling** (scheduled-запуски иногда пропускаются GitHub'ом — incidents
2026-05-11..20 для preview_bot, 2026-05-26 для funnel_metrics/centry_funnel).

## Как работает

Cloudflare cron срабатывает с **секундной точностью** (в отличие от GH cron,
который может drift'ить на 5-30 минут). По расписанию вызывает `scheduled`
handler, который POST'ит на GitHub API `/actions/workflows/{wf}/dispatches`
для нужного workflow.

## Cron → workflow map

| Cron (UTC) | МСК | Workflow |
|---|---|---|
| `0 9 * * *` | Daily 12:00 | `preview_bot.yml` |
| `7 12 * * 2` | Tue 15:07 | `funnel_metrics.yml` (Diktum funnel) |
| `12 12 * * 2` | Tue 15:12 | `centry_funnel.yml` (Centry funnel) |

**Source of truth — `src/index.js` константа `CRON_TO_WORKFLOW`.** При
добавлении нового триггера нужно править И мапу, И секцию `triggers.crons`
в `wrangler.jsonc` — иначе scheduled handler залогирует "UNKNOWN cron" и
workflow не запустится.

## Ручной триггер (для дебага)

```bash
# Backward-compat: default = preview_bot.yml
curl https://forton-cron-trigger.carbon-arma3.workers.dev/trigger

# Конкретный workflow (whitelist enforced)
curl 'https://forton-cron-trigger.carbon-arma3.workers.dev/trigger?wf=funnel_metrics.yml'
curl 'https://forton-cron-trigger.carbon-arma3.workers.dev/trigger?wf=centry_funnel.yml'
```

Workflow должен быть в `CRON_TO_WORKFLOW` — иначе 400 `{"error":"workflow not in allowlist"}`.

## Secrets

- **`GH_PAT`** — fine-grained PAT с правами `actions:write` + `contents:read`
  для `Carbon1777/forton-lab-marketing`. Тот же что `BOT_DISPATCH_PAT` в
  GH Secrets, но хранится отдельно в Cloudflare. **Срок: 90 дней** —
  rotate напоминалка через `weekly_planner.py` (см. PROJECT.md Q6).

## Деплой

```bash
cd marketing-v3/cf-worker/forton-cron-trigger
wrangler login                    # один раз (OAuth в браузере)
wrangler deploy                   # деплой кода + cron triggers

# Если нужно rotate secret:
wrangler secret put GH_PAT        # interactive prompt
```

## Проверка

```bash
# Cron triggers зарегистрированы
curl -H "Authorization: Bearer $(awk -F'"' '/oauth_token/{print $2}' ~/Library/Preferences/.wrangler/config/default.toml)" \
  https://api.cloudflare.com/client/v4/accounts/560430ff1f8732906207f30cd4625ab3/workers/scripts/forton-cron-trigger/schedules

# Live logs (tail)
wrangler tail forton-cron-trigger
```

## Что НЕ делать

- **Не добавлять никакой бизнес-логики** в Worker — это "тонкий мост"
  между cron и GH API. Все решения о контенте/postах живут в `marketing-v3`
  Python-коде.
- **Не использовать `GITHUB_TOKEN` из workflow** — GH блокирует self-trigger,
  нужен именно personal PAT.
- **Не плодить второй Worker** — все cron'ы для GH Actions workflow'ов
  студии живут здесь.
