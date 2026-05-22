# Centry — еженедельный отчёт-воронка (install → профиль → регистрация → активация)

**Дата:** 2026-05-22
**Статус:** согласован, готов к планированию
**Репозиторий реализации:** `marketing-v3` (ветка `feat/centry-funnel`, изолированный worktree)

## Контекст и проблема

В дашборде AppMetrica видны только установки/клики, а реальная воронка Centry
(установки → онбординг → регистрация email → ядровое действие) живёт в Supabase
Centry. Нигде не сводится автоматически. Образец уже сделан для Diktum
(модуль `src/diktum_funnel/`); для Centry повторяем подход, но воронка
**структурно другая**.

**Два отличия Centry от Diktum, выявленные исследованием БД (2026-05-22):**

1. **Cold-start загрязнение.** В `app_users` 756 строк, из них **611 —
   синтетические cold-start юзеры** (`cold_start_registry`). Реальных всего
   **145** (113 GUEST + 32 USER). Без исключения cold-start любая метрика
   бессмысленна.
2. **Две ступени идентичности.** GUEST (`auth_user_id IS NULL`, анонимная
   device-identity после онбординга) → USER (`state='USER'`, реальный
   email-аккаунт). У Diktum такого нет (signUp сразу создаёт USER).

**Решение, выбранное на брейншторме:**
- Детерминированный python-скрипт (без LLM в сборе данных).
- Доставка в существующий служебный ТГ-канал «Планировщик».
- Еженедельно, по вторникам ~15:12 МСК (через 5 мин после Diktum-воронки).
- **Отдельный** изолированный модуль `src/centry_funnel/`, не трогающий ни
  `store_metrics`, ни `diktum_funnel`.

## Цели

- Раз в неделю слать в ТГ воронку Centry за прошедшую ISO-неделю (Пн–Вс, МСК).
- Показывать 4 ступени: установки → новые профили (с разбивкой гости/USER) →
  активация, с конверсиями и дельтой неделя-к-неделе.
- Разделять установки на органику и рекламу.
- **Везде исключать cold-start.**

## Не-цели (YAGNI)

- Нет LLM-интерпретации/«гипотез».
- Нет дашборда/веб-интерфейса — только сообщение в ТГ.
- Нет ретроспективного бэкфилла — снапшоты копятся с первого запуска.
- Нет разбивки по платформам iOS/Android в v1.
- **Не обобщаем модуль на 2 продукта.** Воронки Centry и Diktum различаются
  формой (4 vs 3 ступени, cold-start, GUEST/USER, другой источник активации).
  Параметризация дала бы больше ветвлений, чем экономии, и потребовала бы
  переписать уже идущую реализацию `diktum_funnel`. Переиспользуем только
  низкоуровневое импортом.

## Архитектура

Новый модуль `src/centry_funnel/` в `marketing-v3`, по структуре зеркалит
`src/diktum_funnel/`, но под 4-ступенчатую воронку. Полностью изолирован: свой
workflow, свой снапшот, своё сообщение.

```
src/centry_funnel/
  __init__.py
  appmetrica.py   — клиент AppMetrica Reporting API (installs по источникам)
  supabase_src.py — вызов RPC get_centry_funnel_metrics через REST
  models.py       — dataclass FunnelWeek (4 ступени)
  digest.py       — render_digest(fw, prev) -> HTML-строка для ТГ
  snapshot.py     — load/save/get_prev (.metrics/centry_funnel_snapshots.json)
  cli.py          — оркестрация: collect → build → render → send → save
```

**Переиспользуется импортом (НЕ копируется):**
- `src.store_metrics._http.fetch_with_retry` — HTTP с ретраями.
- `src.store_metrics.models.WeekDelta` — расчёт дельты WoW + стрелка.

Поток (повторяет `diktum_funnel.cli.main`):
1. Отчётная неделя = прошлая ISO-неделя (Пн–Вс, МСК).
2. `appmetrica.fetch_installs(week_start, week_end)` → установки орг/реклама.
3. `supabase_src.fetch_funnel(week_start, week_end)` → профили/гости/USER/активация.
4. Загрузить прошлый снапшот → посчитать дельты WoW.
5. `digest.render_digest(fw, prev)` → HTML.
6. Отправить в ТГ (`TG_PLANNER_BOT_TOKEN` + `TG_OWNER_CHAT_ID`).
7. Сохранить снапшот недели в `.metrics/centry_funnel_snapshots.json`, закоммитить ботом.

## Этап 0 — smoke-test доступа (де-рискинг, делается ПЕРВЫМ)

Перед постройкой модуля проверить сетевую доступность обоих источников из
реальной среды — раннер GitHub Actions (США, без VPN). Локальные проверки
нерепрезентативны (разработчик ходит из РФ через VPN).

Отдельный workflow `.github/workflows/centry_funnel_smoke.yml` (НЕ трогаем
существующий `funnel_smoke.yml` от Diktum), только `workflow_dispatch`. Шаги:
1. `curl` к AppMetrica `GET /management/v1/application/6301660` с
   `Authorization: OAuth $APPMETRICA_OAUTH_TOKEN` → ожидаем HTTP 200.
2. `curl` к Centry Supabase REST `GET $SUPABASE_URL_CENTRY/rest/v1/` с
   `apikey`/`Bearer $SUPABASE_SERVICE_ROLE_KEY_CENTRY` → ожидаем HTTP 200.
3. Печать обоих HTTP-кодов в лог.

**🚦 GATE:** строим основной модуль только после `200 / 200`. Прецедент:
`store_metrics` и `diktum_funnel` уже ходят из GH Actions без VPN. Риск низкий,
но проверяем фактом.

RPC `get_centry_funnel_metrics` на этом этапе ещё не существует — её доступ
проверяется отдельно после деплоя миграции.

## Источники данных

### Установки — AppMetrica Reporting API
- Эндпоинт: `GET https://api.appmetrica.yandex.ru/stat/v1/data`
- Заголовок: `Authorization: OAuth $APPMETRICA_OAUTH_TOKEN` (общий токен,
  scope `appmetrica:read`, уже видит Centry — отдельный токен НЕ нужен).
- Приложение: `id=6301660` (Centry; константа в коде, не секрет).
- Установки: `metrics=ym:ts:installDevices`.
- Разбивка по источнику: `dimensions=ym:ts:publisher` (Органика / реклама).
- TZ приложения — Europe/Moscow.
- ⚠️ Нельзя смешивать метрики разных префиксов (ошибка 4011). Метрика
  `ym:ts:installDevices` — та же, что у Diktum (проверена).

### Профили + регистрация + активация — Supabase RPC (Centry)
Создать read-only функцию (новая миграция в проекте Centry
`lqgzvolirohuettizkhx` через MCP `apply_migration`):

```sql
create or replace function public.get_centry_funnel_metrics(p_from date, p_to date)
returns table(new_profiles int, guests int, users int, activations int)
language sql
security definer
stable
set search_path = public, pg_temp
as $$
  with real_users as (
    -- реальные юзеры = все app_users, КРОМЕ синтетических cold-start
    select a.id, a.state::text as state, a.created_at
    from public.app_users a
    where not exists (
      select 1 from public.cold_start_registry c where c.app_user_id = a.id
    )
  ),
  new_in_week as (
    select * from real_users
    where (created_at at time zone 'Europe/Moscow')::date between p_from and p_to
  ),
  first_membership as (
    -- активация-СОБЫТИЕ: первое в жизни членство в плане
    select m.app_user_id, min(m.joined_at) as first_at
    from public.core_plan_members m
    where exists (select 1 from real_users ru where ru.id = m.app_user_id)
    group by m.app_user_id
  )
  select
    (select count(*)::int from new_in_week)                              as new_profiles,
    (select count(*)::int from new_in_week where state = 'GUEST')        as guests,
    (select count(*)::int from new_in_week where state = 'USER')         as users,
    (select count(*)::int from first_membership
       where (first_at at time zone 'Europe/Moscow')::date between p_from and p_to) as activations;
$$;

revoke all on function public.get_centry_funnel_metrics(date, date) from public;
grant execute on function public.get_centry_funnel_metrics(date, date) to service_role;

comment on function public.get_centry_funnel_metrics(date, date) is
  'Воронка Centry за период МСК (cold-start исключён): new_profiles (новые app_users), guests (state GUEST), users (state USER), activations (первое членство в плане за период). Только service_role. Используется marketing-v3 centry_funnel.';
```

**Семантика воронки (event-модель, согласована на брейншторме):**

| Поле | Что | Тип метрики |
|---|---|---|
| `new_profiles` | реальные `app_users` с `created_at` в неделе (онбординг пройден) | когорта по дате создания |
| `guests` | из new_profiles те, кто сейчас state=GUEST (без email) | когортный срез по тек. состоянию |
| `users` | из new_profiles те, кто сейчас state=USER (email-аккаунт) = «регистрации» | когортный срез по тек. состоянию |
| `activations` | реальные юзеры, чьё **первое** членство в плане (`core_plan_members.joined_at`) в неделе | событие за неделю |

`guests + users = new_profiles` (state — взаимоисключающие).

**Почему event-модель для активации, а не когортная (как у Diktum).**
Проверено на боевых данных: при когортном счёте («из юзеров недели W сколько
активировались») получается активация=0 во всех свежих неделях — все 5 реальных
активаций пришлись на старые когорты (юзер создан давно, активировался позже).
Event-модель («сколько первых вступлений в план произошло на этой неделе») даёт
ненулевые значения и читается как «воронка за неделю». Следствие: в редкую тихую
неделю `activations` может относиться к юзеру из старой когорты (т.е. не быть
подмножеством `users` этой недели). Это допустимо и честно помечается в digest
как «вступления в план за неделю».

- `SECURITY DEFINER` — функция читает все строки независимо от RLS. Вызов из CI
  идёт под `service_role` (RLS и так обходит), `SECURITY DEFINER` — страховка и
  единообразие с Diktum.
- Вызов из CI: `POST $SUPABASE_URL_CENTRY/rest/v1/rpc/get_centry_funnel_metrics`
  с заголовками `apikey` и `Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY_CENTRY`,
  телом `{"p_from":"...","p_to":"..."}`. PostgREST вернёт массив из одной строки.
- Преимущество перед прямым psql: не нужен DB-пароль в CI, доступ ограничен
  одной read-only функцией.

## Секреты (GitHub Secrets репозитория `marketing-v3` = `Carbon1777/forton-lab-marketing`)

| Secret | Назначение | Статус |
|---|---|---|
| `APPMETRICA_OAUTH_TOKEN` | OAuth-токен Яндекса (`y0_…`), scope `appmetrica:read` | ✅ уже есть (общий, от Diktum) |
| `SUPABASE_URL_CENTRY` | `https://lqgzvolirohuettizkhx.supabase.co` | ❌ добавить |
| `SUPABASE_SERVICE_ROLE_KEY_CENTRY` | service_role JWT Centry для вызова RPC | ❌ добавить |
| `TG_PLANNER_BOT_TOKEN` / `TG_OWNER_CHAT_ID` | доставка в «Планировщик» | ✅ уже есть |
| `BOT_DISPATCH_PAT` | коммит снапшота ботом | ✅ уже есть |

- Именование с суффиксом `_CENTRY` — по конвенции репо (`ASC_APP_ID_CENTRY`,
  `GPLAY_PACKAGE_CENTRY`, `RUSTORE_PACKAGE_CENTRY`). Не конфликтует с
  Diktum-секретами `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` (без суффикса).
- `APPMETRICA_APPLICATION_ID=6301660` — константа в коде, не секрет.
- 🔒 Значение `SUPABASE_SERVICE_ROLE_KEY_CENTRY` берётся программно из
  `centry-flutter/scripts/.env` (`SUPABASE_SERVICE_KEY`), в чат/лог не печатается.
  Ключ `exp` 2036 (~10 лет) — ротация в обозримом будущем не нужна.

## Формат сообщения в ТГ (черновик)

```
📊 Centry — воронка за 12–18 мая

Установки         18   (орг 16 / реклама 2)        📈 +3
└ Новые профили    7   (39% от устан.)             📈 +2
      гости 5 · регистрации 2 (USER)
└ Активация         1   (вступления в план)         →

⚠️ Установки до ~22.05 — в осн. обновления старой базы, не новые юзеры
```

- HTML parse_mode, `disable_web_page_preview=true`.
- Дельты «н/н» из снапшота прошлой недели; при отсутствии — прочерк.
- Пометка про «грязные» установки первых недель — как у Diktum (SDK Centry шлёт
  с 19 мая 2026; чистые данные с ~22–23.05, когда схлынет волна обновлений
  старой базы).
- Точная вёрстка дорабатывается в `digest.py`.

## Расписание

`.github/workflows/centry_funnel.yml`, по образцу `diktum_funnel`:
- `cron: "12 12 * * 2"` — вторник 15:12 МСК (UTC+3; +12 мин offset против
  GH-cron-drift и чтобы не слиться с Diktum-воронкой 15:07).
- `workflow_dispatch: {}` для ручного запуска.
- `concurrency: group=centry-funnel, cancel-in-progress=false`.
- `permissions: contents: write` для коммита снапшота.
- Период отчёта = прошедшая ISO-неделя (Пн–Вс).

## Снапшоты

`.metrics/centry_funnel_snapshots.json` — словарь
`{ "YYYY-Www": {week_start, installs_total, installs_organic, installs_ads, new_profiles, guests, users, activations} }`.
Хранится последние 8 недель. Коммитится обратно ботом (`BOT_DISPATCH_PAT`, шаг
«Commit updated snapshot» по образцу `store_metrics`/`diktum_funnel`).

Снапшот фиксирует значения **на момент отчёта** (вторник после недели W).
Когортные срезы `guests`/`users` теоретически могут «дрейфовать» позже (гость
недели W зарегистрируется через 2 недели), но снапшот заморожен — историческое
число стабильно, дельты WoW считаются по сохранённым снапшотам.

## Обработка ошибок (graceful degradation)

- AppMetrica 401 → понятная ошибка «токен истёк/невалиден»; установки = None,
  отчёт всё равно уходит с пометкой.
- Supabase RPC ошибка → профили/гости/USER/активация = None с пометкой, отчёт уходит.
- Отправка в ТГ — единственный шаг, чей провал делает workflow «красным»
  (`send_to_planner` возвращает bool, exit 1 при фейле).

## Известные ограничения

1. **OAuth-токен Яндекса истекает (~1 год).** Через год — ручной перевыпуск
   через браузер. Скрипт даёт явную 401-ошибку. Токен общий для всех приложений
   аккаунта (Centry + Diktum) — перевыпуск чинит оба.
2. **Первые 1–2 недели после интеграции SDK установки загрязнены обновлениями
   старой базы** (Centry SDK с 19 мая 2026). В отчёте первых недель — пометка.
3. **Маленькие абсолютные числа** — честная реальность ранней стадии Centry
   (145 реальных юзеров, 5 активаций всего на 2026-05-22). Отчёт показывает
   правду, не приукрашивает.

## Тестирование

- Unit-тесты в `tests/centry_funnel/` по образцу `tests/diktum_funnel/`:
  парсинг ответа AppMetrica, парсинг RPC (одна строка из 4 чисел), расчёт
  конверсий и дельт WoW, рендер digest (snapshot-тест строки), graceful-ветки
  (None при ошибках источника).
- HTTP-вызовы мокаются.
- RPC проверяется отдельно SQL-запросом + REST-вызовом на проде Centry после
  деплоя миграции.
- Прогон: `cd <worktree> && PYTHONPATH=. <marketing-v3>/.venv/bin/python -m pytest tests/centry_funnel/ -v`.

## Изоляция от параллельной работы

Реализация Centry-воронки ведётся в **отдельном git worktree** на ветке
`feat/centry-funnel`, т.к. в `marketing-v3` на `main` параллельно идёт
реализация `diktum_funnel`. Файлы не пересекаются (`centry_funnel` vs
`diktum_funnel`, `centry_funnel_smoke.yml` vs `funnel_smoke.yml`,
`centry_funnel_snapshots.json` vs `funnel_snapshots.json`). Слияние в `main` —
после готовности, чистым (disjoint paths). Миграция RPC идёт в проект Centry
через MCP (репо не затрагивает).
