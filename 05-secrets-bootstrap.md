---
phase: 05-store-metrics
artifact: runbook
audience: Forton (manual setup)
when_to_run: ОДИН раз ДО первого запуска `store_metrics.yml` (Пн 2026-05-19 09:37 МСК)
estimated_time: 20-25 минут
---

# Runbook — Bootstrap GitHub Secrets для Phase 5 (store_metrics)

> Этот документ читает **юзер**, не агент. Выполни шаги 1→3 один раз. После Шага 3 можно запускать `store_metrics.yml` руками для smoke и/или ждать первого cron Пн 09:37 МСК.
>
> Источник всех значений: `~/.config/forton-lab/keys.env` (права 0600) + два файла кредов рядом. **НЕ commit-ить значения в repo.**

## Что добавляем

14 новых GH Secrets в `Carbon1777/forton-lab-marketing`. Plus уже существующие (`TG_PLANNER_BOT_TOKEN`, `TG_OWNER_CHAT_ID`, `BOT_DISPATCH_PAT`, `ANTHROPIC_API_KEY`) — их **не** трогаем.

URL панели секретов: `https://github.com/Carbon1777/forton-lab-marketing/settings/secrets/actions`.

Каждый секрет добавляется через `New repository secret` (зелёная кнопка) → ввести имя и значение → `Add secret`.

## Шаг 1 — Apple App Store (4 secrets)

Reporter API token + RSS app IDs. TTL 180 дней — ставь календарное напоминание за 14 дней до expiry.

| Secret name | Значение | Источник в `keys.env` | Notes |
|---|---|---|---|
| `ASC_REPORTER_ACCESS_TOKEN` | UUID-формат, начинается с `f...`, длина 36 | `ASC_REPORTER_ACCESS_TOKEN=...` | **TTL 180d, expires 2026-11-10.** Регенерируется в Apple Sales and Trends Reports → About Reports → Generate Reporter Token → Regenerate. Старый токен инвалидируется немедленно. |
| `ASC_VENDOR_NUMBER` | `94183271` | `ASC_VENDOR_NUMBER=94183271` | 8-значный numeric. Не меняется. |
| `ASC_APP_ID_CENTRY` | `6761648930` | (не в keys.env — resolved researcher 2026-05-14) | Numeric Apple App ID для Centry. |
| `ASC_APP_ID_DIKTUM` | `6763641709` | (не в keys.env — resolved researcher 2026-05-14) | Numeric Apple App ID для Diktum. |

Проверка: после добавления в списке секретов должны появиться `ASC_REPORTER_ACCESS_TOKEN`, `ASC_VENDOR_NUMBER`, `ASC_APP_ID_CENTRY`, `ASC_APP_ID_DIKTUM`.

## Шаг 2 — Google Play (4 secrets)

Service Account JSON + developer ID + 2 package names. Service Account без TTL — ротация только при компрометации.

| Secret name | Значение | Источник | Notes |
|---|---|---|---|
| `GOOGLE_PLAY_SA_JSON` | Весь raw JSON содержимым (multi-line, ~2.5KB) | Файл `~/.config/forton-lab/google_play_sa.json` (cat файла → paste весь output в Secret value) | Service Account `play-metrics-reader@forton-lab-publisher.iam.gserviceaccount.com`, key_id `15f776028c2e`. В Play Console приглашён с двумя read-only permissions. |
| `GPLAY_DEVELOPER_ID` | `6224792403622982347` | `GPLAY_DEVELOPER_ID=6224792403622982347` | Numeric Play Console developer ID (Kulitskiy Aleksey). |
| `GPLAY_PACKAGE_CENTRY` | `website.centry.app` | `GPLAY_PACKAGE_CENTRY=website.centry.app` | Package name (рабочая версия). |
| `GPLAY_PACKAGE_DIKTUM` | `ru.diktumweb.diktum` | `GPLAY_PACKAGE_DIKTUM=ru.diktumweb.diktum` | Package name (закрытое тестирование). |

**Как скопировать JSON:** `cat ~/.config/forton-lab/google_play_sa.json | pbcopy` → вставить в Secret value целиком, GH сам сохранит multi-line. Перед `Add secret` визуально проверь что JSON начинается с `{` и заканчивается `}`.

## Шаг 3 — RuStore (5 secrets)

JWS RSA-SHA512 auth + 2 package names. Manual rotation only — RuStore не декларирует TTL.

| Secret name | Значение | Источник | Notes |
|---|---|---|---|
| `RUSTORE_PRIVATE_KEY` | RSA 2048 PKCS#8 PEM (multi-line, начинается с `-----BEGIN PRIVATE KEY-----`) | Файл `~/.config/forton-lab/rustore_private.pem` (cat → paste весь output) | Public-пара зарегистрирована в RuStore Console под `RUSTORE_KEY_ID` (см. ниже). |
| `RUSTORE_KEY_ID` | `2351028465` | `RUSTORE_KEY_ID=2351028465` | Numeric ID ключа в Console. |
| `RUSTORE_COMPANY_ID` | `2351526569` | `RUSTORE_COMPANY_ID=2351526569` | Numeric ID компании в Console. |
| `RUSTORE_PACKAGE_CENTRY` | `website.centry.app` | (тот же что `GPLAY_PACKAGE_CENTRY`) | Same package name as Google Play. |
| `RUSTORE_PACKAGE_DIKTUM` | `ru.diktumweb.diktum` | (тот же что `GPLAY_PACKAGE_DIKTUM`) | Same package name as Google Play. |

**Как скопировать PEM:** `cat ~/.config/forton-lab/rustore_private.pem | pbcopy` → вставить в Secret value целиком (включая BEGIN/END маркеры).

## Шаг 4 — Anthropic (1 secret, может уже существовать)

| Secret name | Значение | Источник | Notes |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-api03-...` | `keys.env` (`ANTHROPIC_API_KEY=...`) | Уже должен быть с Phase 1/2 (для `daily_post_generator` и `monthly_plan`). Если отсутствует — добавь. Без ключа digest идёт **без** секции «💡 Гипотезы недели» (graceful fallback в `hypothesis.py`). |

## Шаг 5 — TG (already exist from earlier phases)

`TG_PLANNER_BOT_TOKEN` и `TG_OWNER_CHAT_ID` уже добавлены в Phase 0 / Phase 1.5. Никакого action не требуется. Если в Settings → Secrets и variables → Actions их нет — добавь по аналогии с другими секретами (значения из `keys.env`).

## Verification — smoke run

После того как все секреты добавлены и Phase 5 PR смерджен в `main`:

1. Открой `https://github.com/Carbon1777/forton-lab-marketing/actions/workflows/store_metrics.yml`
2. Нажми `Run workflow` → ветка `main` → `Run workflow`.
3. Подожди ~3-5 минут.
4. Проверь:
   - Run завершился зелёным (нет красных steps).
   - В TG-канал «Планировщик» (бот `@fortonlab_planner_bot`) пришёл digest вида `📊 Forton Lab — неделя <NN> <DD–DD месяц>`.
   - В repo `forton-lab-marketing` появился новый коммит автора `forton-metrics-bot`: `auto: weekly store metrics snapshot [skip ci]`, обновляющий `.metrics/store_snapshots.json`.

Если **все три пункта подтвердились** — Phase 5 готов к проду. Cron Пн 09:37 МСК подхватит дальше автоматически.

## Manual RuStore CSV workflow (еженедельная процедура)

Каждое воскресенье 18:00 МСК `rustore_csv_reminder.yml` шлёт нудж в TG. Процедура — 3 минуты:

1. Открой `https://console.rustore.ru` → Аналитика → Скачать CSV.
2. Период: текущая ISO-неделя (понедельник-воскресенье). Например, для недели 12–18 мая 2026 это W20.
3. Переименуй файл в `<YYYY-Www>.csv` (например `2026-W20.csv`).
4. Положи в `marketing-v3/.metrics/rustore_weekly/2026-W20.csv` и закоммить в `main` (через GitHub UI или локальный git).
5. Понедельник 09:37 МСК `store_metrics.yml` подхватит файл и включит installs в digest.

Если файл отсутствует на момент запуска — digest идёт с пометкой `RuStore CSV не положен за <week>` в секции 🚨 Алерты. Это не блокирует digest; RuStore-installs просто прочерком.

## Token rotation calendar

`secrets_metadata.json` (`.github/secrets_metadata.json`) отслеживает TTL и порядок ротации:

| Secret | Expires | Action date | Шаги ротации |
|---|---|---|---|
| `ASC_REPORTER_ACCESS_TOKEN` | **2026-11-10** (180d) | **2026-10-27** (за 14 дней) | App Store Connect → Sales and Trends Reports → About Reports → Generate Reporter Token → **Regenerate**. ⚠️ Старый токен инвалидируется немедленно — обнови `ASC_REPORTER_ACCESS_TOKEN` в GH Secrets **в тот же момент**, иначе следующий пн digest упадёт. Обнови `expires` поле в `secrets_metadata.json`. |
| `BOT_DISPATCH_PAT` | **2026-08-08** (90d) | **2026-07-25** (за 14 дней) | GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens → создать новый PAT с тем же name + scopes (Contents:write, Actions:write, Metadata:read), repo binding `forton-lab-marketing` only. Обнови `BOT_DISPATCH_PAT` в GH Secrets, потом revoke старый. |
| `RUSTORE_KEY_ID` | без TTL | manual rotation only if compromised | RuStore Console → Разработчик → API RuStore → Создание ключа → создать новый ключ, скачать private key, обновить `RUSTORE_KEY_ID` и `RUSTORE_PRIVATE_KEY` в GH Secrets, удалить старый ключ в Console. |

Рекомендация: поставь календарные напоминания на `2026-07-25` и `2026-10-27` сейчас, пока не забыто. Automated reminder через `weekly_planner.py` extension — задача Phase 5.x.

## Шаг 6 — Что проверить если красное

| Симптом в логах workflow | Причина | Как чинить |
|---|---|---|
| `KeyError: 'ASC_REPORTER_ACCESS_TOKEN'` / `RUSTORE_KEY_ID` / etc. | Секрет не добавлен или имя другое | Шаг 1-3, имена case-sensitive |
| `401 Unauthorized` от Apple Reporter | Token expired или опечатка copy/paste | Регенерируй Reporter Token в App Store Connect, обнови `ASC_REPORTER_ACCESS_TOKEN` |
| `403 Forbidden` от androidpublisher | Service Account не приглашён в Play Console или JSON битый | Проверь Play Console → Пользователи и разрешения, что `play-metrics-reader@forton-lab-publisher.iam.gserviceaccount.com` активен с двумя read-only permissions |
| RuStore `401 Invalid signature` | `RUSTORE_PRIVATE_KEY` не соответствует `RUSTORE_KEY_ID` | Убедись что PEM скопирован целиком (включая BEGIN/END), что `RUSTORE_KEY_ID` соответствует именно этому ключу в Console |
| Anthropic timeout / `429 rate limit` | API down или превышен budget cap | Это **не** блокирует digest — `hypothesis.py` делает 3 retry → fallback (секция «💡 Гипотезы недели» дропается, в 🚨 Алерты добавляется строка «LLM insights недоступны»). |
| Snapshot commit step падает с `403` | `BOT_DISPATCH_PAT` не имеет Contents:write или expired | Шаг 2 из Phase 1.5 runbook (`015-secrets-bootstrap.md`) — пересоздай PAT |

## Источники

- `~/.config/forton-lab/keys.env` — все credentials (права 0600, не commit-ить).
- `~/.config/forton-lab/google_play_sa.json` — Service Account JSON.
- `~/.config/forton-lab/rustore_private.pem` — RuStore RSA private key.
- `.planning/phases/05-store-metrics/05-CONTEXT.md` — D-5-07 / D-5-09 / D-5-10 decisions.
- `/Users/jcat/Documents/Brain/projects/forton-lab/decisions.md` — пять записей 2026-05-14 (credential mapping + RuStore Q3 closed).
- `.planning/phases/01.5-monthly-approval-bot/015-secrets-bootstrap.md` — паттерн runbook + Phase 1.5 PAT rotation.
- `.github/secrets_metadata.json` — машино-читаемый источник истины для `weekly_planner.py` rotation reminders (Phase 5.x).
