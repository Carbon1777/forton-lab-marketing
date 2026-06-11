---
quick_id: "260611-ls6"
slug: monthly-report
status: complete
date: "2026-06-11"
---

# Summary: Ежемесячный per-app отчёт

## Результат

Создан новый модуль `src/monthly_report/` и workflow `monthly_report.yml`.
5-го числа каждого месяца в TG-канал «Планировщик» прилетает отчёт
за предыдущий полный месяц по всем 5 продуктам студии с MoM-сравнением.

## Созданные файлы

| Файл | Что делает |
|------|-----------|
| `src/monthly_report/__init__.py` | Пустой пакет |
| `src/monthly_report/snapshot.py` | Ключи "YYYY-MM", max 13 месяцев, load/save/store_month/get_prev_installs |
| `src/monthly_report/render.py` | word-based plain text, 9 блоков, MoM в блоке 8 |
| `src/monthly_report/cli.py` | main(), _report_month(), dry-run поддержка |
| `.github/workflows/monthly_report.yml` | cron "0 6 5 * *", 4 секрета (TG+AppMetrica+Supabase×2) |

## Архитектурные решения

- Store installs пропущены: заглушка «в недельных отчётах». AppMetrica покрывает
  все 5 продуктов через arbitrary date range без изменений store-модулей.
- Reuse `ProductReport` dataclass из `hybrid_report.models` (week_start/week_end
  держат month_start/month_end).
- Workflow минимальный (4 секрета vs 20 в store_metrics.yml) — только нужные источники.
- Snapshot `.metrics/monthly_snapshots.json` отдельный от weekly snapshots.

## Проверка

```
dry-run выдал 5 сообщений в STDOUT ✓
рендер с мок-данными: Centry — отчёт за июнь 2026 ✓
_report_month(2026-07-05) → 2026-06-01..2026-06-30 ✓
граничный случай января → декабрь предыдущего года ✓
импорты чистые, все зависимости в .venv ✓
```

## Никакие существующие файлы не изменены ✓
