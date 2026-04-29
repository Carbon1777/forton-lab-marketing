# Forton Lab — Marketing Auto-Publisher

Автоматический публикатор контента для каналов студии Forton Lab.

## Каналы

- **Telegram:** [@fortonlab](https://t.me/fortonlab) — автомат, текст + фото.
- **VK:** [vk.com/fortonlab](https://vk.com/fortonlab) — автомат, **только текст**; фото пользователь прикрепляет вручную (см. ниже).
- **Дзен:** [dzen.ru/fortonlab](https://dzen.ru/fortonlab) — автомат через TG-кросспостинг.

## Архитектура

```
queue/<slug>.md  →  PR  →  merge  →  GitHub Actions  →  TG + VK
```

`tg_post.py` публикует в Telegram (текст + фото), затем переносит файл
`queue/<slug>.md` → `published/<date>-<slug>.md`.

`vk_post.py` смотрит на `published/`, публикует **текстом-only** в VK любые
посты без `vk_post_id` в frontmatter.

## VK + картинки — ручной режим

VK API закрывает scope `photos` и `wall` для user-токенов
(выдаются только индивидуально через `devsupport@corp.vk.com`),
а community-токен заблокирован для `photos.getWallUploadServer`.
Поэтому фото в VK прикрепляются вручную:

1. При подготовке поста Cowork кладёт картинку в `~/Documents/vk_attach/`
   с именем, совпадающим с базовым именем поста (например
   `2026-04-29-welcome.png` для `published/2026-04-29-welcome.md`).
2. GitHub Actions публикует пост текстом — пользователь видит файл в
   `~/Documents/vk_attach/` = «новый пост опубликован, надо прикрепить фото».
3. Пользователь открывает пост в VK → «Редактировать» → прикрепляет
   фото из папки → сохраняет → удаляет файл из `~/Documents/vk_attach/`.
4. Папка пустая = всё прикреплено.

Подробности отказа от автоматизации — `../VK_IMAGES_TASK.md` (архив).

## Статус

v0.1 — TG автомат + VK текстовый автомат.
