---
phase: 05-store-metrics
artifact: runbook
audience: Forton (manual setup)
when_to_run: ОДИН раз ДО первого запуска `store_metrics.yml` (Пн 09:37 МСК)
estimated_time: 10-15 минут
---

# Runbook — Bootstrap GitHub Secrets для Phase 5 (store_metrics)

> Этот документ читает **юзер**, не агент. Выполни шаги 1→5 один раз, после — никакой еженедельной процедуры (full-auto mode 2026-05-15). После Шага 5 можно запускать `store_metrics.yml` руками для smoke и/или ждать первого cron Пн 09:37 МСК.
>
> Источник всех значений: `~/.config/forton-lab/keys.env` (права 0600) + два файла кредов рядом. **НЕ commit-ить значения в repo.**

## Canonical full-auto mode (2026-05-15 final)

Финальное архитектурное решение после серии итераций:

- **Apple App Store — installs blocked, ratings auto.** Apple Integrations API Key generation заблокирован Apple cert recovery (Brain decision 2026-05-14, user confirmed 2026-05-15 — ongoing). Без API Key Modern Sales Reports JWT path невозможен. Reporter Token (UUID) — для deprecated itc-reporter API, modern endpoint его отвергает. **Текущее поведение:** installs=None с явным error «Apple Integrations API не настроен — ждём cert recovery». Ratings продолжают работать через iTunes RSS (no auth). Когда Apple Support починит cert — см. секцию «Apple unblock procedure» ниже.
- **Google Play — full auto.** Installs через GCS bucket `pubsite_prod_rev_<developer_id>` → CSV stats. Reviews через androidpublisher v3. Android Developer API enabled в GCP (user confirmed 2026-05-15: Status = Enabled). Service Account приглашён в Play Console с правом «Просмотр информации и массовое скачивание отчётов» (Brain decision 2026-05-14).
- **RuStore — installs blocked permanently, reviews via JWS.** RuStore Public API физически не отдаёт installs endpoint — это constraint Mail.ru (Brain Q3 2026-05-14, verified full Console method list). Это **не временная блокировка** как у Apple — это отсутствующая функциональность в продукте Mail.ru. **Текущее поведение:** installs=None с явным error про Mail.ru. Reviews продолжают пытаться работать через JWS RSA-SHA512 auth (если HTTP 400 на auth сохраняется — rating=None, но это не блокирует digest).

**Manual CSV mode removed** — юзер отверг ручную загрузку 2026-05-15 («я не буду ничего загружать руками»). Никаких weekly reminder workflows нет.

## Что добавляем

8 GH Secrets в `Carbon1777/forton-lab-marketing`. Plus уже существующие (`TG_PLANNER_BOT_TOKEN`, `TG_OWNER_CHAT_ID`, `BOT_DISPATCH_PAT`, `ANTHROPIC_API_KEY`) — их **не** трогаем.

URL панели секретов: `https://github.com/Carbon1777/forton-lab-marketing/settings/secrets/actions`.

Каждый секрет добавляется через `New repository secret` (зелёная кнопка) → ввести имя и значение → `Add secret`.

## Шаг 1 — Apple App Store (2 secrets, required for ratings)

App IDs нужны для iTunes RSS lookups (auth-free). Installs path заблокирован cert recovery — никаких installs secrets сейчас не добавляем.

| Secret name | Значение | Источник в `keys.env` | Notes |
|---|---|---|---|
| `ASC_APP_ID_CENTRY` | `6761648930` | (не в keys.env — resolved researcher 2026-05-14) | Numeric Apple App ID для Centry. |
| `ASC_APP_ID_DIKTUM` | `6763641709` | (не в keys.env — resolved researcher 2026-05-14) | Numeric Apple App ID для Diktum. |

### Apple unblock procedure (когда Apple Support починит cert)

Когда придёт письмо от Apple что cert recovery завершён:

1. Открой `https://appstoreconnect.apple.com` → **Users and Access** → **Integrations** → **App Store Connect API**.
2. Нажми `Generate API Key` (или `+` если первый ключ).
3. Дай имя (например `forton-metrics-reader`), Access = `Sales and Reports`.
4. Скачай `.p8` файл (Apple отдаёт только один раз — сохрани в `~/.config/forton-lab/asc_private_key.p8`).
5. Запиши **Key ID** (10-символьный, e.g. `ABC123XYZ`) и **Issuer ID** (UUID сверху страницы Integrations).
6. Добавь 3 GH Secrets:
   - `ASC_KEY_ID` = Key ID из шага 5
   - `ASC_ISSUER_ID` = Issuer UUID из шага 5
   - `ASC_PRIVATE_KEY` = весь `.p8` файл (`cat ~/.config/forton-lab/asc_private_key.p8 | pbcopy`)
7. Открой `src/store_metrics/asc.py`, разверни JWT path в `fetch_weekly` (см. комментарий «Apple Integrations API — blocked» — заменить stub на реальный JWT signing block).
8. Push, run workflow руками, проверь что installs показывает реальные числа.

### Optional (deprecated, можно удалить)

| Secret name | Статус | Notes |
|---|---|---|
| `ASC_REPORTER_ACCESS_TOKEN` | deprecated | UUID для itc-reporter API, modern Sales Reports его отвергает. Модуль `asc.py` его не читает. |
| `ASC_VENDOR_NUMBER` | deprecated | Не используется модулем `asc.py` после canonical pivot. |

## Шаг 2 — Google Play (4 required, full auto)

| Secret name | Значение | Источник | Notes |
|---|---|---|---|
| `GOOGLE_PLAY_SA_JSON` | Весь raw JSON содержимым (multi-line, ~2.5KB) | Файл `~/.config/forton-lab/google_play_sa.json` (`cat файла → paste`) | Service Account `play-metrics-reader@forton-lab-publisher.iam.gserviceaccount.com`. Должен быть приглашён в Play Console (Brain 2026-05-14 confirmed «Просмотр информации и массовое скачивание отчётов»). |
| `GPLAY_DEVELOPER_ID` | numeric developer id (e.g. `6224792403622982347`) | `keys.env` (`GPLAY_DEVELOPER_ID=...`) | Используется для конструкции GCS bucket name `pubsite_prod_rev_<id>`. |
| `GPLAY_PACKAGE_CENTRY` | `website.centry.app` | `GPLAY_PACKAGE_CENTRY=website.centry.app` | Package name для CSV filter + reviews API path. |
| `GPLAY_PACKAGE_DIKTUM` | `ru.diktumweb.diktum` | `GPLAY_PACKAGE_DIKTUM=ru.diktumweb.diktum` | Same. |

**Как скопировать JSON:** `cat ~/.config/forton-lab/google_play_sa.json | pbcopy` → вставить в Secret value целиком, GH сам сохранит multi-line. Перед `Add secret` визуально проверь что JSON начинается с `{` и заканчивается `}`.

**Pre-requisite:** Android Developer API enabled в GCP project. User confirmed 2026-05-15 (Status: Enabled на странице `https://console.cloud.google.com/apis/api/androidpublisher.googleapis.com/`).

## Шаг 3 — RuStore (5 secrets, required for reviews path; installs N/A)

JWS RSA-SHA512 auth + 2 package names. Manual rotation only — RuStore не декларирует TTL. Этот блок обслуживает ТОЛЬКО reviews path. **Installs через RuStore физически невозможны автоматически** — Mail.ru не предоставляет endpoint (Brain Q3 2026-05-14).

| Secret name | Значение | Источник | Notes |
|---|---|---|---|
| `RUSTORE_PRIVATE_KEY` | RSA 2048 PKCS#8 PEM (multi-line, начинается с `-----BEGIN PRIVATE KEY-----`) | Файл `~/.config/forton-lab/rustore_private.pem` (`cat → paste`) | Public-пара зарегистрирована в RuStore Console под `RUSTORE_KEY_ID`. |
| `RUSTORE_KEY_ID` | `2351028465` | `RUSTORE_KEY_ID=2351028465` | Numeric ID ключа в Console. |
| `RUSTORE_COMPANY_ID` | `2351526569` | `RUSTORE_COMPANY_ID=2351526569` | Numeric ID компании в Console. |
| `RUSTORE_PACKAGE_CENTRY` | `website.centry.app` | (тот же что `GPLAY_PACKAGE_CENTRY`) | Same package name as Google Play. |
| `RUSTORE_PACKAGE_DIKTUM` | `ru.diktumweb.diktum` | (тот же что `GPLAY_PACKAGE_DIKTUM`) | Same package name as Google Play. |

**Как скопировать PEM:** `cat ~/.config/forton-lab/rustore_private.pem | pbcopy` → вставить в Secret value целиком (включая BEGIN/END маркеры).

**Если reviews auth даёт HTTP 400 / 401:** это не блокирует digest. Rating=None в digest, installs всё равно None из-за Mail.ru limitation — оба прочерка для RuStore. Это canonical state до момента когда Mail.ru добавит installs endpoint И RuStore починит wire issue с JWS auth.

## Шаг 4 — Anthropic (1 secret, может уже существовать)

| Secret name | Значение | Источник | Notes |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-api03-...` | `keys.env` (`ANTHROPIC_API_KEY=...`) | Уже должен быть с Phase 1/2. Без ключа digest идёт **без** секции «💡 Гипотезы недели». |

## Шаг 5 — TG (already exist from earlier phases)

`TG_PLANNER_BOT_TOKEN` и `TG_OWNER_CHAT_ID` уже добавлены в Phase 0 / Phase 1.5. Никакого action не требуется.

## Verification — smoke run

После того как все секреты добавлены и Phase 5 PR смерджен в `main`:

1. Открой `https://github.com/Carbon1777/forton-lab-marketing/actions/workflows/store_metrics.yml`
2. Нажми `Run workflow` → ветка `main` → `Run workflow`.
3. Подожди ~3-5 минут.
4. Проверь:
   - Run завершился зелёным (нет красных steps).
   - В TG-канал «Планировщик» (бот `@fortonlab_planner_bot`) пришёл digest вида `📊 Forton Lab — неделя <NN> <DD–DD месяц>`.
   - **Google Play installs** показывает реальные числа (либо прочерк если blob ещё не сгенерён за текущий ISO-week — google nightly).
   - **App Store installs** показывает прочерк + алерт «Apple Integrations API не настроен — ждём cert recovery». Это **ожидаемое поведение** до Apple unblock.
   - **RuStore installs** показывает прочерк + алерт «RuStore Public API не отдаёт installs (Mail.ru ограничение)». Это **permanent state** до момента когда Mail.ru добавит endpoint.
   - **App Store ratings** показывают реальные числа через iTunes RSS.
   - **Google Play ratings** показывают реальные числа через androidpublisher (или прочерк если SA permissions не дошли — graceful fallback).
   - **RuStore ratings** показывают реальные числа (или прочерк если HTTP 400 на auth — graceful fallback).
   - В repo `forton-lab-marketing` появился новый коммит автора `forton-metrics-bot`: `auto: weekly store metrics snapshot [skip ci]`, обновляющий `.metrics/store_snapshots.json`.

Если **все пункты подтвердились** — Phase 5 готов к проду. Cron Пн 09:37 МСК подхватит дальше автоматически.

## Token rotation calendar

`secrets_metadata.json` (`.github/secrets_metadata.json`) отслеживает TTL и порядок ротации:

| Secret | Expires | Action date | Шаги ротации |
|---|---|---|---|
| `BOT_DISPATCH_PAT` | **2026-08-08** (90d) | **2026-07-25** (за 14 дней) | GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens → создать новый PAT с тем же name + scopes (Contents:write, Actions:write, Metadata:read), repo binding `forton-lab-marketing` only. Обнови `BOT_DISPATCH_PAT` в GH Secrets, потом revoke старый. |
| `RUSTORE_KEY_ID` | без TTL | manual rotation only if compromised | RuStore Console → Разработчик → API RuStore → Создание ключа → создать новый ключ, скачать private key, обновить `RUSTORE_KEY_ID` и `RUSTORE_PRIVATE_KEY` в GH Secrets, удалить старый ключ в Console. |
| `GOOGLE_PLAY_SA_JSON` | без TTL | manual rotation only if compromised | Google Cloud Console → IAM → Service Accounts → пересоздать ключ для `play-metrics-reader@forton-lab-publisher.iam.gserviceaccount.com`, скачать JSON, обновить Secret. |
| `ASC_PRIVATE_KEY` (когда добавлен) | без TTL | manual rotation only if compromised | ASC → Users and Access → Integrations → Revoke текущий key → Generate новый → скачать .p8 → обновить 3 Secrets (`ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_PRIVATE_KEY`). |

**Note:** `ASC_REPORTER_ACCESS_TOKEN` rotation reminder снят с календаря — секрет больше не используется модулем после canonical pivot.

## Шаг 6 — Что проверить если красное

| Симптом в логах workflow | Причина | Как чинить |
|---|---|---|
| `KeyError: 'ASC_APP_ID_CENTRY'` / `GPLAY_PACKAGE_CENTRY` / `RUSTORE_KEY_ID` / etc. | Секрет не добавлен или имя другое | Шаг 1-3, имена case-sensitive |
| Digest вышел но App Store installs прочерком + алерт «Apple Integrations API не настроен» | **Ожидаемое** state до Apple cert recovery. | Когда Apple Support починит — см. «Apple unblock procedure» в Шаге 1. |
| Digest вышел но RuStore installs прочерком + алерт «Mail.ru ограничение» | **Ожидаемое** permanent state — RuStore Public API не отдаёт installs. | Нечего фиксить — ждём пока Mail.ru добавит endpoint. |
| Digest вышел но Google Play installs прочерком (без error) | Blob за текущий ISO-week ещё не сгенерён Google nightly run. | Подождать сутки и/или запустить workflow повторно. |
| Digest вышел но Google Play installs с error `GCS access denied` | Service Account не имеет permissions на bucket | Play Console → Users and Permissions → проверь `play-metrics-reader@...` с правом «Просмотр информации и массовое скачивание отчётов». |
| `403 Forbidden` от androidpublisher (reviews fail) | Service Account не приглашён в Play Console или JSON битый | Play Console → Пользователи и разрешения, проверь `play-metrics-reader@...` активен. Installs всё равно через GCS работают. |
| RuStore `401 Invalid signature` или HTTP 400 | `RUSTORE_PRIVATE_KEY` не соответствует `RUSTORE_KEY_ID`, либо JWS body mismatch | Убедись что PEM скопирован целиком (включая BEGIN/END). Если signature валидна но всё равно 400 — это RuStore wire issue, не блокирует digest. |
| Anthropic timeout / `429 rate limit` | API down или превышен budget cap | Это **не** блокирует digest — `hypothesis.py` делает 3 retry → fallback. |
| Snapshot commit step падает с `403` | `BOT_DISPATCH_PAT` не имеет Contents:write или expired | Шаг 2 из Phase 1.5 runbook — пересоздай PAT |

## Источники

- `~/.config/forton-lab/keys.env` — все credentials (права 0600, не commit-ить).
- `~/.config/forton-lab/google_play_sa.json` — Service Account JSON.
- `~/.config/forton-lab/rustore_private.pem` — RuStore RSA private key.
- `~/.config/forton-lab/asc_private_key.p8` — Apple ASC API Key (когда сгенерён).
- `.planning/phases/05-store-metrics/05-CONTEXT.md` — D-5-07 / D-5-09 / D-5-10 / D-5-15 (canonical pivot) decisions.
- `/Users/jcat/Documents/Brain/projects/forton-lab/decisions.md` — записи 2026-05-14 (credential mapping + RuStore Q3 closed) и 2026-05-15 (canonical full-auto mode, drop manual CSV).
- `.planning/phases/01.5-monthly-approval-bot/015-secrets-bootstrap.md` — паттерн runbook + Phase 1.5 PAT rotation.
- `.github/secrets_metadata.json` — машино-читаемый источник истины для `weekly_planner.py` rotation reminders.
