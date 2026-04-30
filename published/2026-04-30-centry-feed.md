---
source: media/Centry/episode-2-activity-feed.mp4
source_compressed: ffmpeg -vf "crop=498:996:710:20,scale=-2:1920,pad=1080:1920:(ow-iw)/2:0:color=black"
  -c:v libx264 -crf 20 -preset slow -c:a copy -movflags +faststart (10.98 MB → 15.06
  MB, crop content + scale + pad to 1080x1920 9:16 per RULES §3.1 + faststart)
title: Centry — Лента активности
video: assets/video/centry-feed.mp4
vk_post_id: 7
vk_posted_at: '2026-04-30T18:18:03.565013+00:00'
youtube_posted_at: '2026-04-30T18:18:05.532824+00:00'
youtube_privacy: public
youtube_tags:
- centry
- лента активности
- места
- пользователи
- планировщик
- встречи
- ios
- android
youtube_title: 'Centry — Лента активности: места и активность пользователей'
youtube_video_id: JrhN2Z_ief0
---

Centry.

Главный экран — Лента активности.
В ленте отображаются места и активность пользователей вокруг этих мест.
Лента собирается из мест, пользующихся наибольшей популярностью.

centryweb.ru