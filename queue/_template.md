---
# Title — used for YouTube and as fallback if there's no body.
title: Optional post title

# Image path, relative to repo root. Optional. For Telegram → sendPhoto, VK → manual attach.
# image: assets/seal.png

# Video path, relative to repo root. Optional. For Telegram → sendVideo, YouTube → videos.insert.
# Mutually exclusive with `image` (video wins if both set).
# video: assets/video/centry-promo.mp4

# YouTube-specific overrides (all optional):
# youtube_title: Custom title for YT (max 100 chars, else falls back to `title`)
# youtube_description: Multi-line description, signature appended automatically
# youtube_tags: [centry, planner, friends, ios, android]
# youtube_privacy: public        # public | unlisted | private (default public)
# youtube_category: "22"         # 22 = People & Blogs (default)
---

Тело поста. Plain text или HTML-теги, которые поддерживает Telegram:
<b>жирный</b>, <i>курсив</i>, <u>подчёркнутый</u>, <s>зачёркнутый</s>,
<a href="https://example.com">ссылка</a>, <code>моноширинный</code>.

Списки делать через — длинное тире или цифры. Markdown-списки `-` не работают.

Файлы в queue/ публикуются автоматически по push в main, если имя файла НЕ
начинается с подчёркивания. Этот файл (_template.md) игнорируется.

Лимиты:
— текст без картинки: 4096 символов.
— подпись к картинке/видео: 1024 символа.
— видео для Telegram: до 50 MB (sendVideo через хостовый Bot API).
— видео для YouTube: до 256 GB / 12 ч; resumable upload.
