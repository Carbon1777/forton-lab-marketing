---
quick_id: 260711-lc7
slug: youtube-html-description
date: 2026-07-11
status: complete
---

# Summary — YouTube HTML → plain text

**Что сделано:** `src/youtube_post.py` больше не отдаёт в YouTube сырой
HTML из тела поста. Новый `strip_html()` превращает TG-ссылки
`<a href="URL">текст</a>` в `текст: URL`, снимает прочие теги и декодирует
сущности; `derive_description()` и `derive_title()` его используют. Символы
`<`/`>` в snippet больше не появляются → `invalidDescription`/`invalidTitle`
устранены на будущее.

**Проверка:** `pytest tests/test_publisher_filters.py -q` → 14 passed
(3 новых регресс-теста, включая кейс `unia-jul11-dvoe`). Ручная прогонка
реального тела поста — в описании нет `<`/`>`, ссылка и подпись сохранены.

**Тот конкретный ролик** (`unia-jul11-dvoe`) юзер залил в YouTube вручную —
доливать не требуется.
