"""Hybrid per-app weekly metrics report.

ОДНО богатое word-based TG-сообщение НА КАЖДОЕ приложение из PRODUCTS
(Diktum, Лапуля, Листвия; Centry/Lucea/Unia закрыты и убраны 2026-10-01).
Изолированный модуль: переиспользует store_metrics / *_funnel импортом, НЕ
меняет их публичный API. Шлёт per-app в канал «Планировщик» в понедельник.
"""
