"""Hybrid per-app weekly metrics report.

ОДНО богатое word-based TG-сообщение НА КАЖДОЕ приложение (Centry + Diktum).
Изолированный модуль: переиспользует store_metrics / *_funnel импортом, НЕ
меняет их публичный API. Шлёт per-app в канал «Планировщик» в понедельник.
"""
