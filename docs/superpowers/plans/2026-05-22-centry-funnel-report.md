# Centry Funnel Report — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Еженедельный детерминированный отчёт-воронка Centry (установки → новые профили → регистрация USER → активация) в служебный ТГ-канал «Планировщик», по вторникам ~15:12 МСК.

**Architecture:** Изолированный python-модуль `src/centry_funnel/` в `marketing-v3`, по образцу `src/diktum_funnel/`, но под 4-ступенчатую воронку с исключением cold-start. Источники: AppMetrica Reporting API (установки, app 6301660) + Supabase RPC `get_centry_funnel_metrics` (профили/гости/USER/активация). Доставка через planner-бот. Снапшоты для дельт WoW. Не трогает ни `store_metrics`, ни `diktum_funnel`.

**Tech Stack:** Python 3.14, `requests` (через `src.store_metrics._http.fetch_with_retry`), pytest + `unittest.mock`, GitHub Actions, Supabase Postgres (SECURITY DEFINER RPC).

**Spec:** [docs/superpowers/specs/2026-05-22-centry-funnel-report-design.md](../specs/2026-05-22-centry-funnel-report-design.md)

---

## Окружение (фиксированные пути — изоляция от параллельной Diktum-сессии)

- **Worktree (рабочий каталог, ветка `feat/centry-funnel`):**
  `/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel`
- **Python venv (из основного чекаута, переиспользуем):**
  `/Users/jcat/Documents/Forton Lab/marketing-v3/.venv/bin/python`
- **Прогон тестов:**
  `cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel" && PYTHONPATH=. "/Users/jcat/Documents/Forton Lab/marketing-v3/.venv/bin/python" -m pytest <path> -v`
- **GitHub repo:** `Carbon1777/forton-lab-marketing`
- **Centry Supabase project ref:** `lqgzvolirohuettizkhx` (MCP-доступ для миграции RPC).
- **Источник service-key Centry (значение НЕ печатать):** `/Users/jcat/Documents/Doc/centry-flutter/scripts/.env` → `SUPABASE_SERVICE_KEY`.
- 🔒 Секреты в чат/лог не печатать. Адресный `git add`. Все коммиты — на `feat/centry-funnel`, НЕ на `main`.

---

## File Structure

**В worktree (`marketing-v3`, ветка `feat/centry-funnel`):**
- `.github/workflows/centry_funnel_smoke.yml` — Этап 0, ручная проверка доступа из CI
- `.github/workflows/centry_funnel.yml` — еженедельный workflow
- `src/centry_funnel/__init__.py` — пакет
- `src/centry_funnel/models.py` — `FunnelWeek` dataclass (4 ступени)
- `src/centry_funnel/appmetrica.py` — клиент AppMetrica (установки + источники, app 6301660)
- `src/centry_funnel/supabase_src.py` — клиент Supabase RPC (профили/гости/USER/активация)
- `src/centry_funnel/snapshot.py` — персистентность WoW (`.metrics/centry_funnel_snapshots.json`)
- `src/centry_funnel/digest.py` — рендер HTML-сообщения
- `src/centry_funnel/cli.py` — оркестрация
- `tests/centry_funnel/` — unit-тесты
- `tests/fixtures/centry_funnel/` — JSON-фикстуры ответов API

**В проекте Centry (через MCP, репо не затрагивается):**
- Миграция RPC `get_centry_funnel_metrics` на `lqgzvolirohuettizkhx`.

**Переиспользуется (импорт, не копирование):**
- `src.store_metrics._http.fetch_with_retry` — HTTP с ретраями.
- `src.store_metrics.models.WeekDelta` — расчёт дельты WoW + стрелка (`.arrow`).

---

## Task 1: GitHub Secrets + smoke-test доступа (Этап 0 — GATE)

Цель — фактически убедиться, что AppMetrica (app 6301660) и Centry Supabase доступны из GH-раннера (США, без VPN) ДО постройки модуля.

**Files:**
- Create: `.github/workflows/centry_funnel_smoke.yml`

- [ ] **Step 1: Добавить недостающие секреты в репозиторий**

`APPMETRICA_OAUTH_TOKEN`, `TG_PLANNER_BOT_TOKEN`, `TG_OWNER_CHAT_ID`, `BOT_DISPATCH_PAT` уже есть (проверить). Добавить два новых с суффиксом `_CENTRY`. Значение service-key брать программно (НЕ печатать).

```bash
REPO=Carbon1777/forton-lab-marketing
CENTRY_ENV=/Users/jcat/Documents/Doc/centry-flutter/scripts/.env
gh secret set SUPABASE_URL_CENTRY --repo "$REPO" --body "https://lqgzvolirohuettizkhx.supabase.co"
gh secret set SUPABASE_SERVICE_ROLE_KEY_CENTRY --repo "$REPO" \
  --body "$(grep '^SUPABASE_SERVICE_KEY=' "$CENTRY_ENV" | cut -d= -f2-)"
gh secret list --repo "$REPO" | grep -E "SUPABASE_URL_CENTRY|SUPABASE_SERVICE_ROLE_KEY_CENTRY|APPMETRICA_OAUTH_TOKEN"
```

Expected: три секрета перечислены в выводе `gh secret list` (`APPMETRICA_OAUTH_TOKEN` — уже был, два `_CENTRY` — новые).

- [ ] **Step 2: Создать smoke-test workflow**

```yaml
# .github/workflows/centry_funnel_smoke.yml
name: centry_funnel_smoke

# Этап 0 — ручная проверка доступа к AppMetrica (Centry app 6301660) + Centry
# Supabase из GH-раннера (США, без VPN). Запускать через "Run workflow".
# Gate: оба = 200. НЕ путать с funnel_smoke.yml (Diktum).
on:
  workflow_dispatch: {}

jobs:
  smoke:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - name: AppMetrica reachability (Centry app 6301660)
        env:
          TOKEN: ${{ secrets.APPMETRICA_OAUTH_TOKEN }}
        run: |
          CODE=$(curl -s -o /dev/null -w "%{http_code}" \
            -H "Authorization: OAuth $TOKEN" \
            "https://api.appmetrica.yandex.ru/management/v1/application/6301660")
          echo "AppMetrica HTTP=$CODE"
          test "$CODE" = "200"

      - name: Centry Supabase reachability
        env:
          URL: ${{ secrets.SUPABASE_URL_CENTRY }}
          KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY_CENTRY }}
        run: |
          CODE=$(curl -s -o /dev/null -w "%{http_code}" \
            -H "apikey: $KEY" -H "Authorization: Bearer $KEY" \
            "$URL/rest/v1/")
          echo "Supabase HTTP=$CODE"
          test "$CODE" = "200"
```

- [ ] **Step 3: Закоммитить, запушить ветку (чтобы GH видел workflow)**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
git add .github/workflows/centry_funnel_smoke.yml
git commit -m "ci(centry-funnel): smoke-test доступа к AppMetrica + Centry Supabase из CI"
git push -u origin feat/centry-funnel
```

Expected: ветка `feat/centry-funnel` на origin, workflow виден.

- [ ] **Step 4: Запустить smoke-test и проверить gate**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
gh workflow run centry_funnel_smoke.yml --ref feat/centry-funnel
sleep 25
gh run list --workflow=centry_funnel_smoke.yml --limit 1
gh run view --log $(gh run list --workflow=centry_funnel_smoke.yml --limit 1 --json databaseId -q '.[0].databaseId') | grep -E "AppMetrica HTTP|Supabase HTTP"
```

Expected: оба шага зелёные, в логе `AppMetrica HTTP=200` и `Supabase HTTP=200`.

🚦 **GATE:** если любой ≠ 200 — СТОП, разбираемся с доступом, не продолжаем план. Если оба 200 — переходим к Task 2.

---

## Task 2: Supabase RPC `get_centry_funnel_metrics` (проект Centry через MCP)

**Files:** миграция применяется через MCP `apply_migration` на `lqgzvolirohuettizkhx` (репо не затрагивается).

- [ ] **Step 1: Применить миграцию**

MCP `apply_migration`, name: `get_centry_funnel_metrics_rpc`, query:

```sql
create or replace function public.get_centry_funnel_metrics(p_from date, p_to date)
returns table(new_profiles int, guests int, users int, activations int)
language sql
security definer
stable
set search_path = public, pg_temp
as $$
  with real_users as (
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
    select m.app_user_id, min(m.joined_at) as first_at
    from public.core_plan_members m
    where exists (select 1 from real_users ru where ru.id = m.app_user_id)
    group by m.app_user_id
  )
  select
    (select count(*)::int from new_in_week)                       as new_profiles,
    (select count(*)::int from new_in_week where state = 'GUEST') as guests,
    (select count(*)::int from new_in_week where state = 'USER')  as users,
    (select count(*)::int from first_membership
       where (first_at at time zone 'Europe/Moscow')::date between p_from and p_to) as activations;
$$;

revoke all on function public.get_centry_funnel_metrics(date, date) from public;
grant execute on function public.get_centry_funnel_metrics(date, date) to service_role;

comment on function public.get_centry_funnel_metrics(date, date) is
  'Воронка Centry за период МСК (cold-start исключён): new_profiles, guests (GUEST), users (USER), activations (первое членство в плане за период). Только service_role. marketing-v3 centry_funnel.';
```

- [ ] **Step 2: Проверить RPC фактически (известный период)**

MCP `execute_sql`:
```sql
select * from public.get_centry_funnel_metrics('2026-05-11','2026-05-17');
```
Expected: одна строка. Сверка с превью из SPEC: за W20 (11–17 мая) `new_profiles=5, guests=0, users=5, activations=1`.

- [ ] **Step 3: Проверить вызов через REST (как будет ходить CI)**

```bash
CENTRY_ENV=/Users/jcat/Documents/Doc/centry-flutter/scripts/.env
KEY=$(grep '^SUPABASE_SERVICE_KEY=' "$CENTRY_ENV" | cut -d= -f2-)
curl -s -X POST "https://lqgzvolirohuettizkhx.supabase.co/rest/v1/rpc/get_centry_funnel_metrics" \
  -H "apikey: $KEY" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"p_from":"2026-05-11","p_to":"2026-05-17"}'
```
Expected: JSON-массив с одной строкой `[{"new_profiles":5,"guests":0,"users":5,"activations":1}]`. Если 200 + данные — RPC готова к использованию из CI.

---

## Task 3: Пакет + модель `FunnelWeek`

**Files:**
- Create: `src/centry_funnel/__init__.py`
- Create: `src/centry_funnel/models.py`
- Create: `tests/centry_funnel/__init__.py`
- Test: `tests/centry_funnel/test_models.py`

- [ ] **Step 1: Создать пустые пакеты**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
mkdir -p src/centry_funnel tests/centry_funnel tests/fixtures/centry_funnel
printf '"""Centry funnel weekly report."""\n' > src/centry_funnel/__init__.py
printf '' > tests/centry_funnel/__init__.py
```

- [ ] **Step 2: Написать failing-тест модели**

```python
# tests/centry_funnel/test_models.py
from __future__ import annotations

import datetime as dt

from src.centry_funnel.models import FunnelWeek


def test_funnel_week_holds_all_fields():
    fw = FunnelWeek(
        week_start=dt.date(2026, 5, 12),
        week_end=dt.date(2026, 5, 18),
        installs_total=18, installs_organic=16, installs_ads=2,
        new_profiles=7, guests=5, users=2, activations=1,
    )
    assert fw.installs_total == 18
    assert fw.new_profiles == 7
    assert fw.guests == 5
    assert fw.users == 2
    assert fw.activations == 1
    assert fw.appmetrica_error is None
    assert fw.supabase_error is None


def test_funnel_week_allows_none_on_source_failure():
    fw = FunnelWeek(
        week_start=dt.date(2026, 5, 12),
        week_end=dt.date(2026, 5, 18),
        installs_total=None, installs_organic=None, installs_ads=None,
        new_profiles=7, guests=5, users=2, activations=1,
        appmetrica_error="401 token expired",
    )
    assert fw.installs_total is None
    assert fw.appmetrica_error == "401 token expired"
```

- [ ] **Step 3: Запустить тест — убедиться, что падает**

Run: `cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel" && PYTHONPATH=. "/Users/jcat/Documents/Forton Lab/marketing-v3/.venv/bin/python" -m pytest tests/centry_funnel/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.centry_funnel.models'`

- [ ] **Step 4: Написать модель**

```python
# src/centry_funnel/models.py
"""Domain model — одна неделя воронки Centry (4 ступени)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class FunnelWeek:
    """Воронка Centry за одну ISO-неделю (Пн–Вс), TZ Europe/Moscow.

    None в installs_* — AppMetrica недоступна (см. appmetrica_error);
    None в new_profiles/guests/users/activations — Supabase недоступен
    (supabase_error). Digest рендерит «—» для None.

    Семантика (cold-start исключён):
      new_profiles — реальные app_users, созданные в неделе;
      guests/users — из них state=GUEST / state=USER (guests+users=new_profiles);
      activations  — реальные юзеры, чьё первое членство в плане — в этой неделе.
    """
    week_start: dt.date
    week_end: dt.date
    installs_total: int | None
    installs_organic: int | None
    installs_ads: int | None
    new_profiles: int | None
    guests: int | None
    users: int | None
    activations: int | None
    appmetrica_error: str | None = None
    supabase_error: str | None = None
```

- [ ] **Step 5: Запустить тест — убедиться, что проходит**

Run: тот же pytest-командой из Step 3.
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
git add src/centry_funnel/__init__.py src/centry_funnel/models.py tests/centry_funnel/__init__.py tests/centry_funnel/test_models.py
git commit -m "feat(centry-funnel): модель FunnelWeek (4 ступени)"
```

---

## Task 4: AppMetrica клиент (Centry app 6301660)

**Files:**
- Create: `src/centry_funnel/appmetrica.py`
- Create: `tests/fixtures/centry_funnel/appmetrica_installs.json`
- Test: `tests/centry_funnel/test_appmetrica.py`

- [ ] **Step 1: Создать фикстуру ответа AppMetrica**

```json
{
  "data": [
    {"dimensions": [{"name": "Органика"}], "metrics": [16.0]},
    {"dimensions": [{"name": "Реклама"}], "metrics": [2.0]}
  ],
  "total_rows": 2
}
```
Сохранить в `tests/fixtures/centry_funnel/appmetrica_installs.json`.

- [ ] **Step 2: Написать failing-тесты**

```python
# tests/centry_funnel/test_appmetrica.py
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.centry_funnel import appmetrica

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "centry_funnel"
INSTALLS = json.loads((FIXTURES / "appmetrica_installs.json").read_text(encoding="utf-8"))


def _mock_resp(payload: dict, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload
    m.raise_for_status.return_value = None
    return m


def test_fetch_installs_splits_organic_and_ads():
    with patch.object(appmetrica, "fetch_with_retry", return_value=_mock_resp(INSTALLS)) as f:
        result = appmetrica.fetch_installs(
            dt.date(2026, 5, 12), dt.date(2026, 5, 18), token="t"
        )
    assert result.total == 18
    assert result.organic == 16
    assert result.ads == 2
    assert result.by_publisher["Реклама"] == 2
    _, kwargs = f.call_args
    assert kwargs["params"]["id"] == "6301660"
    assert kwargs["params"]["metrics"] == "ym:ts:installDevices"
    assert kwargs["params"]["dimensions"] == "ym:ts:publisher"


def test_fetch_installs_empty_data_is_zero():
    with patch.object(appmetrica, "fetch_with_retry", return_value=_mock_resp({"data": []})):
        result = appmetrica.fetch_installs(dt.date(2026, 5, 12), dt.date(2026, 5, 18), token="t")
    assert result.total == 0
    assert result.organic == 0
    assert result.ads == 0
```

- [ ] **Step 3: Запустить — убедиться, что падает**

Run: `cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel" && PYTHONPATH=. "/Users/jcat/Documents/Forton Lab/marketing-v3/.venv/bin/python" -m pytest tests/centry_funnel/test_appmetrica.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.centry_funnel.appmetrica'`

- [ ] **Step 4: Реализовать клиент**

```python
# src/centry_funnel/appmetrica.py
"""AppMetrica Reporting API — установки Centry по источникам.

Метрика ym:ts:installDevices, разбивка по ym:ts:publisher (Органика / реклама).
App id 6301660 (Centry). TZ приложения — Europe/Moscow. Токен общий
(APPMETRICA_OAUTH_TOKEN, scope appmetrica:read).
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

from src.store_metrics._http import fetch_with_retry

APP_ID = "6301660"
STAT_URL = "https://api.appmetrica.yandex.ru/stat/v1/data"
ORGANIC_NAME = "Органика"


@dataclass(frozen=True)
class InstallsBySource:
    total: int
    organic: int
    ads: int
    by_publisher: dict[str, int]


def _token() -> str:
    t = os.environ.get("APPMETRICA_OAUTH_TOKEN")
    if not t:
        raise RuntimeError("APPMETRICA_OAUTH_TOKEN missing")
    return t


def fetch_installs(
    week_start: dt.date, week_end: dt.date, token: str | None = None
) -> InstallsBySource:
    """Установки за период [week_start, week_end] с разбивкой по источнику."""
    token = token or _token()
    resp = fetch_with_retry(
        STAT_URL,
        method="GET",
        headers={"Authorization": f"OAuth {token}"},
        params={
            "id": APP_ID,
            "date1": week_start.isoformat(),
            "date2": week_end.isoformat(),
            "metrics": "ym:ts:installDevices",
            "dimensions": "ym:ts:publisher",
            "accuracy": "full",
            "lang": "ru",
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    by_publisher: dict[str, int] = {}
    for row in payload.get("data", []):
        name = row["dimensions"][0]["name"]
        value = int(row["metrics"][0] or 0)
        by_publisher[name] = value
    total = sum(by_publisher.values())
    organic = by_publisher.get(ORGANIC_NAME, 0)
    ads = total - organic
    return InstallsBySource(
        total=total, organic=organic, ads=ads, by_publisher=by_publisher
    )
```

- [ ] **Step 5: Запустить — убедиться, что проходит**

Run: тот же pytest из Step 3.
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
git add src/centry_funnel/appmetrica.py tests/centry_funnel/test_appmetrica.py tests/fixtures/centry_funnel/appmetrica_installs.json
git commit -m "feat(centry-funnel): клиент AppMetrica (установки по источникам, app 6301660)"
```

---

## Task 5: Supabase RPC клиент

**Files:**
- Create: `src/centry_funnel/supabase_src.py`
- Create: `tests/fixtures/centry_funnel/supabase_rpc.json`
- Test: `tests/centry_funnel/test_supabase_src.py`

- [ ] **Step 1: Создать фикстуру ответа RPC**

```json
[
  {"new_profiles": 5, "guests": 0, "users": 5, "activations": 1}
]
```
Сохранить в `tests/fixtures/centry_funnel/supabase_rpc.json`.

- [ ] **Step 2: Написать failing-тесты**

```python
# tests/centry_funnel/test_supabase_src.py
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.centry_funnel import supabase_src

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "centry_funnel"
RPC = json.loads((FIXTURES / "supabase_rpc.json").read_text(encoding="utf-8"))


def _mock_resp(payload, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload
    m.raise_for_status.return_value = None
    return m


def test_fetch_funnel_parses_single_row():
    with patch.object(supabase_src, "fetch_with_retry", return_value=_mock_resp(RPC)) as f:
        result = supabase_src.fetch_funnel(
            dt.date(2026, 5, 11), dt.date(2026, 5, 17),
            url="https://x.supabase.co", key="k",
        )
    assert result.new_profiles == 5
    assert result.guests == 0
    assert result.users == 5
    assert result.activations == 1
    _, kwargs = f.call_args
    assert kwargs["method"] == "POST"
    assert kwargs["json_body"] == {"p_from": "2026-05-11", "p_to": "2026-05-17"}
    assert kwargs["headers"]["apikey"] == "k"


def test_fetch_funnel_empty_is_zero():
    with patch.object(supabase_src, "fetch_with_retry", return_value=_mock_resp([])):
        result = supabase_src.fetch_funnel(
            dt.date(2026, 5, 11), dt.date(2026, 5, 17),
            url="https://x.supabase.co", key="k",
        )
    assert result.new_profiles == 0
    assert result.guests == 0
    assert result.users == 0
    assert result.activations == 0
```

- [ ] **Step 3: Запустить — убедиться, что падает**

Run: `cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel" && PYTHONPATH=. "/Users/jcat/Documents/Forton Lab/marketing-v3/.venv/bin/python" -m pytest tests/centry_funnel/test_supabase_src.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.centry_funnel.supabase_src'`

- [ ] **Step 4: Реализовать клиент**

```python
# src/centry_funnel/supabase_src.py
"""Supabase RPC get_centry_funnel_metrics — профили/гости/USER/активация.

Вызывает SECURITY DEFINER функцию через PostgREST с service-role ключом Centry.
Env: SUPABASE_URL_CENTRY, SUPABASE_SERVICE_ROLE_KEY_CENTRY.
RPC возвращает массив из одной строки.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass

from src.store_metrics._http import fetch_with_retry


@dataclass(frozen=True)
class FunnelDB:
    new_profiles: int
    guests: int
    users: int
    activations: int


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} missing")
    return v


def fetch_funnel(
    week_start: dt.date,
    week_end: dt.date,
    url: str | None = None,
    key: str | None = None,
) -> FunnelDB:
    """Воронка Centry за период через RPC get_centry_funnel_metrics."""
    url = url or _env("SUPABASE_URL_CENTRY")
    key = key or _env("SUPABASE_SERVICE_ROLE_KEY_CENTRY")
    resp = fetch_with_retry(
        f"{url}/rest/v1/rpc/get_centry_funnel_metrics",
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json_body={"p_from": week_start.isoformat(), "p_to": week_end.isoformat()},
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return FunnelDB(new_profiles=0, guests=0, users=0, activations=0)
    r = rows[0]
    return FunnelDB(
        new_profiles=int(r["new_profiles"]),
        guests=int(r["guests"]),
        users=int(r["users"]),
        activations=int(r["activations"]),
    )
```

- [ ] **Step 5: Запустить — убедиться, что проходит**

Run: тот же pytest из Step 3.
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
git add src/centry_funnel/supabase_src.py tests/centry_funnel/test_supabase_src.py tests/fixtures/centry_funnel/supabase_rpc.json
git commit -m "feat(centry-funnel): клиент Supabase RPC (профили/гости/USER/активация)"
```

---

## Task 6: Снапшоты для WoW

**Files:**
- Create: `src/centry_funnel/snapshot.py`
- Test: `tests/centry_funnel/test_snapshot.py`

- [ ] **Step 1: Написать failing-тесты**

```python
# tests/centry_funnel/test_snapshot.py
from __future__ import annotations

import datetime as dt

from src.centry_funnel import snapshot
from src.centry_funnel.models import FunnelWeek


def _fw(week_start: dt.date, installs: int, new_profiles: int) -> FunnelWeek:
    return FunnelWeek(
        week_start=week_start, week_end=week_start + dt.timedelta(days=6),
        installs_total=installs, installs_organic=installs, installs_ads=0,
        new_profiles=new_profiles, guests=new_profiles, users=0, activations=0,
    )


def test_store_and_get_prev_week(tmp_path):
    path = tmp_path / "centry_funnel_snapshots.json"
    data = {}
    prev_week = dt.date(2026, 5, 5)
    curr_week = dt.date(2026, 5, 12)
    data = snapshot.store_week(data, _fw(prev_week, installs=10, new_profiles=3))
    snapshot.save(path, data)

    loaded = snapshot.load(path)
    prev = snapshot.get_prev(loaded, curr_week)
    assert prev is not None
    assert prev["installs_total"] == 10
    assert prev["new_profiles"] == 3


def test_get_prev_missing_returns_none():
    assert snapshot.get_prev({}, dt.date(2026, 5, 12)) is None


def test_load_missing_file_returns_empty(tmp_path):
    assert snapshot.load(tmp_path / "nope.json") == {}
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel" && PYTHONPATH=. "/Users/jcat/Documents/Forton Lab/marketing-v3/.venv/bin/python" -m pytest tests/centry_funnel/test_snapshot.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать снапшоты**

```python
# src/centry_funnel/snapshot.py
"""Snapshot persistence — храним прошлые недели для Δ WoW.

Файл: .metrics/centry_funnel_snapshots.json
Format (per ISO week):
    {"2026-W20": {"week_start": "2026-05-11", "installs_total": 18,
                  "installs_organic": 16, "installs_ads": 2,
                  "new_profiles": 7, "guests": 5, "users": 2, "activations": 1}}
Храним последние 8 недель.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .models import FunnelWeek

MAX_WEEKS_KEPT = 8


def _week_key(date: dt.date) -> str:
    iso = date.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _prune(data: dict, keep: int) -> dict:
    weeks = sorted(data.keys())
    if len(weeks) <= keep:
        return data
    keep_set = set(weeks[-keep:])
    return {k: v for k, v in data.items() if k in keep_set}


def save(path: Path, data: dict) -> None:
    pruned = _prune(data, MAX_WEEKS_KEPT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(pruned, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def store_week(data: dict, fw: FunnelWeek) -> dict:
    """Добавить/обновить запись недели. Mutates and returns data."""
    data[_week_key(fw.week_start)] = {
        "week_start": fw.week_start.isoformat(),
        "installs_total": fw.installs_total,
        "installs_organic": fw.installs_organic,
        "installs_ads": fw.installs_ads,
        "new_profiles": fw.new_profiles,
        "guests": fw.guests,
        "users": fw.users,
        "activations": fw.activations,
    }
    return data


def get_prev(data: dict, current_week_start: dt.date) -> dict | None:
    """Запись за неделю ДО current_week_start, или None."""
    prev_week_start = current_week_start - dt.timedelta(days=7)
    return data.get(_week_key(prev_week_start))
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: тот же pytest из Step 2.
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
git add src/centry_funnel/snapshot.py tests/centry_funnel/test_snapshot.py
git commit -m "feat(centry-funnel): снапшоты для дельт неделя-к-неделе"
```

---

## Task 7: Рендер digest

**Files:**
- Create: `src/centry_funnel/digest.py`
- Test: `tests/centry_funnel/test_digest.py`

- [ ] **Step 1: Написать failing-тесты**

```python
# tests/centry_funnel/test_digest.py
from __future__ import annotations

import datetime as dt

from src.centry_funnel.digest import render_digest
from src.centry_funnel.models import FunnelWeek


def _fw(**over) -> FunnelWeek:
    base = dict(
        week_start=dt.date(2026, 5, 12), week_end=dt.date(2026, 5, 18),
        installs_total=18, installs_organic=16, installs_ads=2,
        new_profiles=7, guests=5, users=2, activations=1,
    )
    base.update(over)
    return FunnelWeek(**base)


def test_digest_contains_funnel_numbers():
    text = render_digest(_fw(), prev=None)
    assert "Centry" in text
    assert "18" in text          # установки
    assert "7" in text           # новые профили
    assert "гости 5" in text
    assert "регистрации 2" in text
    assert "12" in text and "18" in text   # даты периода


def test_digest_renders_conversion():
    text = render_digest(_fw(), prev=None)
    assert "39%" in text         # 7/18


def test_digest_handles_none_installs():
    text = render_digest(_fw(installs_total=None, installs_organic=None,
                             installs_ads=None, appmetrica_error="401"),
                         prev=None)
    assert "—" in text
    assert "7" in text           # профили всё равно показаны


def test_digest_shows_wow_delta_when_prev_present():
    prev = {"installs_total": 15, "new_profiles": 5, "activations": 0}
    text = render_digest(_fw(), prev=prev)
    assert "📈" in text          # установки выросли 15→18
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel" && PYTHONPATH=. "/Users/jcat/Documents/Forton Lab/marketing-v3/.venv/bin/python" -m pytest tests/centry_funnel/test_digest.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать рендер**

```python
# src/centry_funnel/digest.py
"""HTML digest для ТГ-канала «Планировщик» — воронка Centry (4 ступени).

Формат:
    📊 Centry — воронка за 12–18 мая

    Установки         18  (орг 16 / реклама 2)  📈 +3
    └ Новые профили    7  (39% от устан.)        📈 +2
          гости 5 · регистрации 2 (USER)
    └ Активация         1  (вступления в план)    →
"""
from __future__ import annotations

from .models import FunnelWeek
from src.store_metrics.models import WeekDelta

_MONTHS_RU = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _fmt(value: int | None) -> str:
    return "—" if value is None else str(value)


def _pct(part: int | None, whole: int | None) -> str:
    if part is None or whole is None or whole == 0:
        return "—"
    return f"{round(part / whole * 100)}%"


def _delta_str(curr: int | None, prev: int | None) -> str:
    d = WeekDelta.compute(curr, prev)
    if d.arrow == "—" or prev is None:
        return ""
    sign = "+" if (curr or 0) >= (prev or 0) else "−"
    diff = abs((curr or 0) - (prev or 0))
    return f"  {d.arrow} {sign}{diff}"


def _date_range(fw: FunnelWeek) -> str:
    s, e = fw.week_start, fw.week_end
    if s.month == e.month:
        return f"{s.day}–{e.day} {_MONTHS_RU[e.month]}"
    return f"{s.day} {_MONTHS_RU[s.month]} – {e.day} {_MONTHS_RU[e.month]}"


def render_digest(fw: FunnelWeek, prev: dict | None) -> str:
    """Собрать HTML-сообщение воронки. prev — запись прошлой недели или None."""
    p_inst = prev.get("installs_total") if prev else None
    p_np = prev.get("new_profiles") if prev else None
    p_act = prev.get("activations") if prev else None

    installs_line = _fmt(fw.installs_total)
    if fw.installs_total is not None:
        installs_line += (
            f"  (орг {_fmt(fw.installs_organic)} / реклама {_fmt(fw.installs_ads)})"
        )
    installs_line += _delta_str(fw.installs_total, p_inst)

    np_line = (
        f"{_fmt(fw.new_profiles)}  ({_pct(fw.new_profiles, fw.installs_total)} от устан.)"
        + _delta_str(fw.new_profiles, p_np)
    )

    split_line = f"гости {_fmt(fw.guests)} · регистрации {_fmt(fw.users)} (USER)"

    act_line = (
        f"{_fmt(fw.activations)}  (вступления в план)"
        + _delta_str(fw.activations, p_act)
    )

    lines = [
        f"📊 <b>Centry — воронка за {_date_range(fw)}</b>",
        "",
        f"Установки        {installs_line}",
        f"└ Новые профили   {np_line}",
        f"      {split_line}",
        f"└ Активация       {act_line}",
    ]

    if fw.appmetrica_error:
        lines += ["", f"⚠️ AppMetrica: {fw.appmetrica_error}"]
    if fw.supabase_error:
        lines += ["", f"⚠️ Supabase: {fw.supabase_error}"]

    return "\n".join(lines)
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: тот же pytest из Step 2.
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
git add src/centry_funnel/digest.py tests/centry_funnel/test_digest.py
git commit -m "feat(centry-funnel): рендер HTML-digest воронки (4 ступени)"
```

---

## Task 8: CLI оркестрация

**Files:**
- Create: `src/centry_funnel/cli.py`
- Test: `tests/centry_funnel/test_cli.py`

- [ ] **Step 1: Написать failing-тесты**

```python
# tests/centry_funnel/test_cli.py
from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from src.centry_funnel import cli
from src.centry_funnel.appmetrica import InstallsBySource
from src.centry_funnel.supabase_src import FunnelDB


def test_report_week_is_previous_iso_week():
    # вторник 19 мая 2026 → отчёт за пред. неделю Пн 11 – Вс 17 мая
    ws, we = cli._report_week(dt.date(2026, 5, 19))
    assert ws == dt.date(2026, 5, 11)
    assert we == dt.date(2026, 5, 17)


def test_main_collects_renders_sends_saves(tmp_path):
    snap = tmp_path / "centry_funnel_snapshots.json"
    with patch.object(cli.appmetrica, "fetch_installs",
                      return_value=InstallsBySource(18, 16, 2, {"Органика": 16})), \
         patch.object(cli.supabase_src, "fetch_funnel",
                      return_value=FunnelDB(new_profiles=7, guests=5, users=2, activations=1)), \
         patch.object(cli, "send_to_planner", return_value=True) as send:
        rc = cli.main(today=dt.date(2026, 5, 19), snapshots_path=snap)
    assert rc == 0
    send.assert_called_once()
    sent_text = send.call_args.args[0]
    assert "Centry" in sent_text and "18" in sent_text
    assert snap.exists()


def test_main_graceful_when_appmetrica_fails(tmp_path):
    snap = tmp_path / "centry_funnel_snapshots.json"
    with patch.object(cli.appmetrica, "fetch_installs",
                      side_effect=RuntimeError("401 token")), \
         patch.object(cli.supabase_src, "fetch_funnel",
                      return_value=FunnelDB(new_profiles=7, guests=5, users=2, activations=1)), \
         patch.object(cli, "send_to_planner", return_value=True) as send:
        rc = cli.main(today=dt.date(2026, 5, 19), snapshots_path=snap)
    assert rc == 0           # отчёт всё равно ушёл
    sent_text = send.call_args.args[0]
    assert "—" in sent_text  # установки прочерком
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel" && PYTHONPATH=. "/Users/jcat/Documents/Forton Lab/marketing-v3/.venv/bin/python" -m pytest tests/centry_funnel/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать CLI**

```python
# src/centry_funnel/cli.py
"""Entrypoint — собирает воронку Centry за прошлую неделю, рендерит, шлёт в ТГ,
сохраняет снапшот. Вызывается из .github/workflows/centry_funnel.yml.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Final

import requests

from . import appmetrica, supabase_src
from .digest import render_digest
from .models import FunnelWeek
from .snapshot import get_prev, load, save, store_week

SNAPSHOTS_PATH: Final[Path] = Path(".metrics/centry_funnel_snapshots.json")


def _iso_week_start(date: dt.date) -> dt.date:
    return date - dt.timedelta(days=date.weekday())


def _report_week(today: dt.date) -> tuple[dt.date, dt.date]:
    """Прошлая ISO-неделя: (понедельник, воскресенье)."""
    last_monday = _iso_week_start(today) - dt.timedelta(days=7)
    return last_monday, last_monday + dt.timedelta(days=6)


def send_to_planner(text: str) -> bool:
    """sendMessage в ТГ-канал «Планировщик» (TG_PLANNER_BOT_TOKEN/CHAT_ID)."""
    token = os.environ.get("TG_PLANNER_BOT_TOKEN")
    chat_id = os.environ.get("TG_OWNER_CHAT_ID")
    if not (token and chat_id):
        sys.stderr.write("WARN: TG creds missing — digest не отправлен\n")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code == 200:
            return True
        sys.stderr.write(f"ERROR: TG HTTP {r.status_code}: {r.text[:200]}\n")
        return False
    except requests.RequestException as exc:
        sys.stderr.write(f"ERROR: TG send failed: {exc!r}\n")
        return False


def _collect(week_start: dt.date, week_end: dt.date) -> FunnelWeek:
    installs_total = installs_organic = installs_ads = None
    appmetrica_error = None
    try:
        inst = appmetrica.fetch_installs(week_start, week_end)
        installs_total, installs_organic, installs_ads = inst.total, inst.organic, inst.ads
    except Exception as exc:  # graceful — отчёт уходит без установок
        appmetrica_error = f"{type(exc).__name__}: {str(exc)[:80]}"
        sys.stderr.write(f"WARN: AppMetrica failed: {appmetrica_error}\n")

    new_profiles = guests = users = activations = None
    supabase_error = None
    try:
        db = supabase_src.fetch_funnel(week_start, week_end)
        new_profiles, guests, users, activations = (
            db.new_profiles, db.guests, db.users, db.activations
        )
    except Exception as exc:
        supabase_error = f"{type(exc).__name__}: {str(exc)[:80]}"
        sys.stderr.write(f"WARN: Supabase failed: {supabase_error}\n")

    return FunnelWeek(
        week_start=week_start, week_end=week_end,
        installs_total=installs_total, installs_organic=installs_organic,
        installs_ads=installs_ads, new_profiles=new_profiles, guests=guests,
        users=users, activations=activations,
        appmetrica_error=appmetrica_error, supabase_error=supabase_error,
    )


def main(today: dt.date | None = None, snapshots_path: Path | None = None) -> int:
    today = today or dt.date.today()
    snapshots_path = snapshots_path or SNAPSHOTS_PATH

    week_start, week_end = _report_week(today)
    sys.stderr.write(f"INFO: centry funnel digest for {week_start}–{week_end}\n")

    fw = _collect(week_start, week_end)

    data = load(snapshots_path)
    prev = get_prev(data, week_start)

    digest = render_digest(fw, prev)
    print(digest)   # для GH Actions log
    ok = send_to_planner(digest)

    data = store_week(data, fw)
    save(snapshots_path, data)

    return 0 if ok else 1


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: тот же pytest из Step 2.
Expected: 3 passed

- [ ] **Step 5: Прогнать весь модуль**

Run: `cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel" && PYTHONPATH=. "/Users/jcat/Documents/Forton Lab/marketing-v3/.venv/bin/python" -m pytest tests/centry_funnel/ -v`
Expected: все тесты модуля passed (2+2+2+3+4+3 = 16).

- [ ] **Step 6: Commit**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
git add src/centry_funnel/cli.py tests/centry_funnel/test_cli.py
git commit -m "feat(centry-funnel): CLI-оркестрация (collect → render → send → snapshot)"
```

---

## Task 9: Еженедельный workflow

**Files:**
- Create: `.github/workflows/centry_funnel.yml`

- [ ] **Step 1: Создать workflow**

```yaml
# .github/workflows/centry_funnel.yml
name: centry_funnel

# Еженедельный отчёт-воронка Centry (install → профиль → регистрация → активация).
# Cron Вт 12:12 UTC = 15:12 МСК (через 5 мин после Diktum-воронки, +12 offset
# против GH-cron-drift). Период отчёта = прошлая неделя Пн–Вс.
# ВНИМАНИЕ: schedule срабатывает только на default-ветке (main) — активируется
# после мержа feat/centry-funnel в main. workflow_dispatch работает на любой ветке.
on:
  schedule:
    - cron: "12 12 * * 2"
  workflow_dispatch: {}

permissions:
  contents: write   # commit snapshot JSON

concurrency:
  group: centry-funnel
  cancel-in-progress: false

jobs:
  collect-and-send:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 1
          token: ${{ secrets.BOT_DISPATCH_PAT }}

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Collect + render + send funnel
        env:
          TG_PLANNER_BOT_TOKEN: ${{ secrets.TG_PLANNER_BOT_TOKEN }}
          TG_OWNER_CHAT_ID: ${{ secrets.TG_OWNER_CHAT_ID }}
          APPMETRICA_OAUTH_TOKEN: ${{ secrets.APPMETRICA_OAUTH_TOKEN }}
          SUPABASE_URL_CENTRY: ${{ secrets.SUPABASE_URL_CENTRY }}
          SUPABASE_SERVICE_ROLE_KEY_CENTRY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY_CENTRY }}
        run: PYTHONPATH=. python -m src.centry_funnel.cli

      - name: Commit updated snapshot
        env:
          GIT_AUTHOR_NAME: forton-metrics-bot
          GIT_AUTHOR_EMAIL: forton-metrics-bot@users.noreply.github.com
          GIT_COMMITTER_NAME: forton-metrics-bot
          GIT_COMMITTER_EMAIL: forton-metrics-bot@users.noreply.github.com
        run: |
          if git diff --quiet .metrics/centry_funnel_snapshots.json 2>/dev/null; then
            echo "snapshot unchanged — no commit"
            exit 0
          fi
          git add .metrics/centry_funnel_snapshots.json
          git commit -m "auto: weekly centry funnel snapshot [skip ci]"
          git pull --rebase
          git push
```

- [ ] **Step 2: Закоммитить и запушить**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
git add .github/workflows/centry_funnel.yml
git commit -m "ci(centry-funnel): еженедельный workflow (Вт 15:12 МСК)"
git push
```

---

## Task 10: E2E через workflow_dispatch (verification)

**Files:** нет (проверка)

- [ ] **Step 1: Запустить workflow вручную на ветке**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
gh workflow run centry_funnel.yml --ref feat/centry-funnel
sleep 35
gh run list --workflow=centry_funnel.yml --limit 1
```

- [ ] **Step 2: Проверить лог и доставку**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
gh run view --log $(gh run list --workflow=centry_funnel.yml --limit 1 --json databaseId -q '.[0].databaseId') | grep -A8 "Centry — воронка"
```
Expected: в логе видна воронка Centry, workflow зелёный, сообщение пришло в ТГ-канал «Планировщик».

- [ ] **Step 3: Проверить, что снапшот закоммичен ботом**

```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
git pull
test -f .metrics/centry_funnel_snapshots.json && echo "snapshot OK" && cat .metrics/centry_funnel_snapshots.json
```
Expected: файл существует, содержит запись отчётной недели.

- [ ] **Step 4: (опционально) удалить smoke-test workflow**

После успешного E2E `centry_funnel_smoke.yml` больше не нужен:
```bash
cd "/Users/jcat/Documents/Forton Lab/marketing-v3-wt-centry-funnel"
git rm .github/workflows/centry_funnel_smoke.yml
git commit -m "ci(centry-funnel): удалить smoke-test после успешного E2E"
git push
```

- [ ] **Step 5: Финал — мерж в main (активирует cron)**

Через skill `superpowers:finishing-a-development-branch`. Cron `schedule` начнёт срабатывать только после попадания `centry_funnel.yml` в `main`. Мерж координируется так, чтобы не конфликтовать с параллельной Diktum-сессией (файлы disjoint — конфликтов быть не должно).

---

## Self-Review notes

- **Spec coverage:** Этап 0 smoke (GATE) → Task 1; AppMetrica installs+источники (app 6301660) → Task 4; Supabase RPC (4 поля, cold-start excl) → Task 2+5; модель 4 ступени → Task 3; снапшоты/WoW → Task 6; формат сообщения (гости/USER/активация) → Task 7; расписание Вт 15:12 → Task 9; graceful errors → Task 8 (`_collect`); секреты `_CENTRY` → Task 1; ограничения (токен/загрязнение/малые числа) — в digest-пометках и SPEC.
- **Cross-repo/изоляция:** все коммиты на `feat/centry-funnel` в worktree; RPC через MCP в Centry. Не трогает `main`, `store_metrics`, `diktum_funnel`.
- **DRY:** `fetch_with_retry` и `WeekDelta` импортируются из `store_metrics` (сигнатуры сверены: `params=`/`json_body=`, `WeekDelta.compute(curr,prev).arrow`). `send_to_planner` — своя копия (10 строк) ради изоляции доставки.
- **Type consistency:** `InstallsBySource(total, organic, ads, by_publisher)`, `FunnelDB(new_profiles, guests, users, activations)`, `FunnelWeek(...)` с теми же полями, `fetch_funnel(week_start, week_end, url, key)`, `render_digest(fw, prev)`, `main(today, snapshots_path)`, `_report_week(today)` — согласованы между задачами и тестами.
