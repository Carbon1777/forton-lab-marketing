---
phase: 05-store-metrics
artifact: runbook
audience: Forton (manual setup)
when_to_run: ОДИН раз ДО первого запуска `store_metrics.yml` (Пн 09:37 МСК)
estimated_time: 15-20 минут
---

# Runbook — Bootstrap GitHub Secrets для Phase 5 (store_metrics)

> Этот документ читает **юзер**, не агент. Выполни шаги 1→4 один раз, после — еженедельная процедура CSV (см. секцию «Weekly manual CSV procedure» в конце). После Шага 4 можно запускать `store_metrics.yml` руками для smoke и/или ждать первого cron Пн 09:37 МСК.
>
> Источник всех значений: `~/.config/forton-lab/keys.env` (права 0600) + два файла кредов рядом. **НЕ commit-ить значения в repo.**

## Canonical pivot 2026-05-15 — manual CSV installs

После 4 итераций hotfix выяснилось:

- **Apple Reporter Token** (UUID) — для **deprecated legacy itc-reporter API**. Modern Sales Reports API отвергает его как "improperly configured bearer token". Integrations path (JWT из ASC API Key) заблокирован cert recovery.
- **Google Play GCS bucket** — access fails, скорее всего IAM scope или hidden whitespace в env. Diagnostic step output masked GH Actions secret-redaction — undebuggable remotely.
- **RuStore API статистики** — не существует (Brain decision 2026-05-14, 9 read-only methods, ни одного со статистикой).

**Canonical solution для всех 3 сторов:** installs читаются из ручных CSV в `.metrics/{asc,gplay,rustore}_weekly/<YYYY-Www>.csv`. Юзер 5 минут в воскресенье скачивает 3 CSV из админок, кладёт в репо. Понедельничный digest 09:37 МСК их читает.

**Ratings:** ASC через iTunes RSS (no auth, всегда работает). RuStore через JWS RSA reviews API. GPlay через androidpublisher v3 (опционально, SA credentials — если работает, отлично, если нет — graceful fallback).

## Что добавляем

8 GH Secrets в `Carbon1777/forton-lab-marketing` (было 14 — старые `ASC_REPORTER_ACCESS_TOKEN`, `ASC_VENDOR_NUMBER`, `GPLAY_DEVELOPER_ID` больше не нужны манифесту, но в Secrets их можно оставить — модуль их игнорирует). Plus уже существующие (`TG_PLANNER_BOT_TOKEN`, `TG_OWNER_CHAT_ID`, `BOT_DISPATCH_PAT`, `ANTHROPIC_API_KEY`) — их **не** трогаем.

URL панели секретов: `https://github.com/Carbon1777/forton-lab-marketing/settings/secrets/actions`.

Каждый секрет добавляется через `New repository secret` (зелёная кнопка) → ввести имя и значение → `Add secret`.

## Шаг 1 — Apple App Store (2 secrets, required)

App IDs нужны для iTunes RSS lookups + для фильтрации строк в CSV.

| Secret name | Значение | Источник в `keys.env` | Notes |
|---|---|---|---|
| `ASC_APP_ID_CENTRY` | `6761648930` | (не в keys.env — resolved researcher 2026-05-14) | Numeric Apple App ID для Centry. |
| `ASC_APP_ID_DIKTUM` | `6763641709` | (не в keys.env — resolved researcher 2026-05-14) | Numeric Apple App ID для Diktum. |

### Optional (deprecated, можно НЕ добавлять)

| Secret name | Статус | Notes |
|---|---|---|
| `ASC_REPORTER_ACCESS_TOKEN` | deprecated | UUID для itc-reporter API, modern Sales Reports его отвергает. Модуль `asc.py` его не читает. Можно удалить из GH Secrets или оставить — не используется. |
| `ASC_VENDOR_NUMBER` | deprecated | Не используется модулем `asc.py` после canonical pivot. |

## Шаг 2 — Google Play (2 required + 1 optional)

Package names — required, для фильтрации CSV + для reviews API path. Service Account JSON — опциональный, только для reviews (если падает — installs всё равно работают).

| Secret name | Значение | Источник | Notes |
|---|---|---|---|
| `GPLAY_PACKAGE_CENTRY` | `website.centry.app` | `GPLAY_PACKAGE_CENTRY=website.centry.app` | Required. Package name для CSV filter + reviews API path. |
| `GPLAY_PACKAGE_DIKTUM` | `ru.diktumweb.diktum` | `GPLAY_PACKAGE_DIKTUM=ru.diktumweb.diktum` | Required. |
| `GOOGLE_PLAY_SA_JSON` | Весь raw JSON содержимым (multi-line, ~2.5KB) | Файл `~/.config/forton-lab/google_play_sa.json` (cat файла → paste весь output в Secret value) | Optional. Без неё reviews=None, installs работают. Service Account `play-metrics-reader@forton-lab-publisher.iam.gserviceaccount.com`. |

### Optional (deprecated, можно НЕ добавлять)

| Secret name | Статус | Notes |
|---|---|---|
| `GPLAY_DEVELOPER_ID` | deprecated | Был только для конструкции GCS bucket name `pubsite_prod_rev_<id>`. Модуль `play.py` его не читает после canonical pivot. |

**Как скопировать JSON:** `cat ~/.config/forton-lab/google_play_sa.json | pbcopy` → вставить в Secret value целиком, GH сам сохранит multi-line. Перед `Add secret` визуально проверь что JSON начинается с `{` и заканчивается `}`.

## Шаг 3 — RuStore (5 secrets, required for reviews path)

JWS RSA-SHA512 auth + 2 package names. Manual rotation only — RuStore не декларирует TTL. Этот блок обслуживает только reviews path (installs через CSV).

| Secret name | Значение | Источник | Notes |
|---|---|---|---|
| `RUSTORE_PRIVATE_KEY` | RSA 2048 PKCS#8 PEM (multi-line, начинается с `-----BEGIN PRIVATE KEY-----`) | Файл `~/.config/forton-lab/rustore_private.pem` (cat → paste весь output) | Public-пара зарегистрирована в RuStore Console под `RUSTORE_KEY_ID`. |
| `RUSTORE_KEY_ID` | `2351028465` | `RUSTORE_KEY_ID=2351028465` | Numeric ID ключа в Console. |
| `RUSTORE_COMPANY_ID` | `2351526569` | `RUSTORE_COMPANY_ID=2351526569` | Numeric ID компании в Console. |
| `RUSTORE_PACKAGE_CENTRY` | `website.centry.app` | (тот же что `GPLAY_PACKAGE_CENTRY`) | Same package name as Google Play. |
| `RUSTORE_PACKAGE_DIKTUM` | `ru.diktumweb.diktum` | (тот же что `GPLAY_PACKAGE_DIKTUM`) | Same package name as Google Play. |

**Как скопировать PEM:** `cat ~/.config/forton-lab/rustore_private.pem | pbcopy` → вставить в Secret value целиком (включая BEGIN/END маркеры).

## Шаг 4 — Anthropic (1 secret, может уже существовать)

| Secret name | Значение | Источник | Notes |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-api03-...` | `keys.env` (`ANTHROPIC_API_KEY=...`) | Уже должен быть с Phase 1/2. Без ключа digest идёт **без** секции «💡 Гипотезы недели». |

## Шаг 5 — TG (already exist from earlier phases)

`TG_PLANNER_BOT_TOKEN` и `TG_OWNER_CHAT_ID` уже добавлены в Phase 0 / Phase 1.5. Никакого action не требуется.

## Verification — smoke run

После того как все секреты добавлены и Phase 5 PR смерджен в `main`:

1. **Положи 3 CSV вручную за текущую ISO-неделю** (см. секцию «Weekly manual CSV procedure» ниже).
2. Открой `https://github.com/Carbon1777/forton-lab-marketing/actions/workflows/store_metrics.yml`
3. Нажми `Run workflow` → ветка `main` → `Run workflow`.
4. Подожди ~3-5 минут.
5. Проверь:
   - Run завершился зелёным (нет красных steps).
   - В TG-канал «Планировщик» (бот `@fortonlab_planner_bot`) пришёл digest вида `📊 Forton Lab — неделя <NN> <DD–DD месяц>` с реальными installs из CSV.
   - В repo `forton-lab-marketing` появился новый коммит автора `forton-metrics-bot`: `auto: weekly store metrics snapshot [skip ci]`, обновляющий `.metrics/store_snapshots.json`.

Если **все три пункта подтвердились** — Phase 5 готов к проду. Cron Пн 09:37 МСК подхватит дальше автоматически.

## Weekly manual CSV procedure (еженедельная процедура, 5 минут)

Каждое воскресенье 18:00 МСК `weekly_csv_reminder.yml` шлёт нудж в TG. Процедура:

### 1. Apple App Store

1. Открой `https://appstoreconnect.apple.com` → Sales and Trends → Reports.
2. Выбери `Sales` → `Weekly` → нужная неделя (прошлая, понедельник-воскресенье).
3. Нажми `Download` — получишь файл вида `S_W_94183271_20260518.txt.gz` (Apple Sales Report, TSV формат).
4. Распакуй: `gunzip S_W_*.txt.gz` → получится `S_W_*.txt`.
5. Переименуй в `<YYYY-Www>.csv` (например `2026-W20.csv`) — расширение .csv ОК, модуль автодетектит TSV.
6. Положи в `marketing-v3/.metrics/asc_weekly/2026-W20.csv` и закоммить.

### 2. Google Play

1. Открой `https://play.google.com/console` → выбери приложение → Statistics.
2. Выбери диапазон: прошлая неделя (Пн-Вс).
3. В правом верхнем углу выбери `Export CSV` → метрика `Installs` или общий export.
4. Файл скачается в UTF-16 LE BOM формате (так и должно быть, модуль это поддерживает).
5. Переименуй в `<YYYY-Www>.csv` (например `2026-W20.csv`).
6. Положи в `marketing-v3/.metrics/gplay_weekly/2026-W20.csv` и закоммить.

### 3. RuStore

1. Открой `https://console.rustore.ru` → Аналитика → Скачать CSV.
2. Период: прошлая ISO-неделя (понедельник-воскресенье).
3. Переименуй файл в `<YYYY-Www>.csv` (например `2026-W20.csv`).
4. Положи в `marketing-v3/.metrics/rustore_weekly/2026-W20.csv` и закоммить.

### Commit all 3

```bash
cd marketing-v3
git add .metrics/asc_weekly/2026-W20.csv \
       .metrics/gplay_weekly/2026-W20.csv \
       .metrics/rustore_weekly/2026-W20.csv
git commit -m "data: weekly CSV uploads for 2026-W20"
git push
```

Или через GitHub UI: открой каждый файл по очереди → Add file → Upload files.

### Что если забыл положить какой-то CSV

Digest всё равно придёт в понедельник 09:37 МСК, но в секции 🚨 Алерты появится строка типа `ASC CSV не положен — installs см. ASC UI` для соответствующего стора. Installs по этому стору будут прочерком в digest. Это **не блокирует** digest и не ломает workflow.

## Token rotation calendar

`secrets_metadata.json` (`.github/secrets_metadata.json`) отслеживает TTL и порядок ротации:

| Secret | Expires | Action date | Шаги ротации |
|---|---|---|---|
| `BOT_DISPATCH_PAT` | **2026-08-08** (90d) | **2026-07-25** (за 14 дней) | GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens → создать новый PAT с тем же name + scopes (Contents:write, Actions:write, Metadata:read), repo binding `forton-lab-marketing` only. Обнови `BOT_DISPATCH_PAT` в GH Secrets, потом revoke старый. |
| `RUSTORE_KEY_ID` | без TTL | manual rotation only if compromised | RuStore Console → Разработчик → API RuStore → Создание ключа → создать новый ключ, скачать private key, обновить `RUSTORE_KEY_ID` и `RUSTORE_PRIVATE_KEY` в GH Secrets, удалить старый ключ в Console. |
| `GOOGLE_PLAY_SA_JSON` | без TTL | manual rotation only if compromised | Google Cloud Console → IAM → Service Accounts → пересоздать ключ для `play-metrics-reader@forton-lab-publisher.iam.gserviceaccount.com`, скачать JSON, обновить Secret. |

**Note:** `ASC_REPORTER_ACCESS_TOKEN` rotation reminder снят с календаря — секрет больше не используется модулем после canonical pivot.

## Шаг 6 — Что проверить если красное

| Симптом в логах workflow | Причина | Как чинить |
|---|---|---|
| `KeyError: 'ASC_APP_ID_CENTRY'` / `GPLAY_PACKAGE_CENTRY` / `RUSTORE_KEY_ID` / etc. | Секрет не добавлен или имя другое | Шаг 1-3, имена case-sensitive |
| Digest вышел но installs прочерком для какого-то стора | CSV за нужную ISO-неделю не положен в `.metrics/<store>_weekly/` | Положи CSV, run workflow повторно |
| Digest упал с `csv encoding not recognized` | CSV битый (не UTF-8/UTF-16) | Скачай заново с консоли стора, не редактируй вручную |
| `403 Forbidden` от androidpublisher | Service Account не приглашён в Play Console или JSON битый | Play Console → Пользователи и разрешения, проверь `play-metrics-reader@...` активен. Также OK ignore — installs всё равно через CSV. |
| RuStore `401 Invalid signature` | `RUSTORE_PRIVATE_KEY` не соответствует `RUSTORE_KEY_ID` | Убедись что PEM скопирован целиком (включая BEGIN/END). Только rating-секция будет прочерком, installs через CSV работают. |
| Anthropic timeout / `429 rate limit` | API down или превышен budget cap | Это **не** блокирует digest — `hypothesis.py` делает 3 retry → fallback. |
| Snapshot commit step падает с `403` | `BOT_DISPATCH_PAT` не имеет Contents:write или expired | Шаг 2 из Phase 1.5 runbook — пересоздай PAT |

## Источники

- `~/.config/forton-lab/keys.env` — все credentials (права 0600, не commit-ить).
- `~/.config/forton-lab/google_play_sa.json` — Service Account JSON.
- `~/.config/forton-lab/rustore_private.pem` — RuStore RSA private key.
- `.planning/phases/05-store-metrics/05-CONTEXT.md` — D-5-07 / D-5-09 / D-5-10 / D-5-15 (canonical pivot) decisions.
- `/Users/jcat/Documents/Brain/projects/forton-lab/decisions.md` — записи 2026-05-14 (credential mapping + RuStore Q3 closed) и 2026-05-15 (canonical manual-CSV pivot для всех 3 сторов).
- `.planning/phases/01.5-monthly-approval-bot/015-secrets-bootstrap.md` — паттерн runbook + Phase 1.5 PAT rotation.
- `.github/secrets_metadata.json` — машино-читаемый источник истины для `weekly_planner.py` rotation reminders.
