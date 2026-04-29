# Claude Code — onboarding для этого репо

Это репозиторий публикатора студии **Forton Lab** (Telegram + VK + YouTube +
Дзен через TG-кросспостинг).

## ⚠️ Перед любым действием с публикацией

**Прочитай главный регламент:**

```
~/Documents/Forton Lab/PUBLISHING_RULES.md
```

Там лежат:
- Технические лимиты каналов (TG ≤50 МБ, Дзен ≤20 МБ, YouTube ∞).
- Правило ffmpeg-препроцессинга видео для TG/Дзен (≤19 МБ + `-movflags +faststart`).
- Бренд (палитра, шрифты, ToV, запретные слова).
- Матрица типов постов и каналов (что в какие каналы идёт).
- Источники сырья (`~/Documents/Forton Lab/media/{Centry,Diktum,Forton Lab}/`).
- Дисциплина с `~/Documents/vk_attach/` (cp не mv, удалять после ручного аттача).
- Расписание planner-бота (Вт/Чт/Сб 21:00 МСК).
- Запреты.

**Если действуешь без сверки с этим файлом — это процессная ошибка.**

## Парные документы (тоже в `~/Documents/Forton Lab/`)

- `SESSION_CONTEXT.md` — журнал решений всех сессий, статус каналов.
- `ROADMAP.md` — фазы развития.
- `forton_lab_brand.md` — бренд детально.
- `SECRETS.md` — все токены и ID каналов (PAT для GitHub, токены TG/VK, OAuth-данные YouTube).

## Структура этого репо

```
.github/workflows/
├── publish.yml           ← триггер на push в queue/*.md → TG + VK + YT
└── weekly_planner.yml    ← cron Вт/Чт/Сб 21:00 МСК → TG-напоминание

src/
├── tg_post.py                       Telegram (текст / фото / видео)
├── vk_post.py                       VK (текст-only + ручной аттач)
├── youtube_post.py                  YouTube Data API + OAuth refresh_token
├── get_youtube_refresh_token.py     One-off helper, run locally
└── weekly_planner.py                Дайджест в TG-канал «Forton Lab Планировщик»

queue/<slug>.md           ← новые посты на публикацию (push сюда → запуск)
published/<slug>.md       ← опубликованные (с tg_post_id, vk_post_id, youtube_video_id, source)
assets/
├── seal.png              Печать FORTONLAB (876×876)
├── centry_icon.png       Иконка приложения Centry
├── diktum_icon.png       Иконка приложения Diktum
├── welcome.png
└── video/<slug>.mp4      Видео для постов (после ffmpeg-сжатия если >19 МБ)
```

## Что нельзя делать

- Публиковать видео >20 МБ в TG/Дзен без ffmpeg-сжатия + faststart.
- Публиковать пост, не привязанный к продукту (Centry / Diktum) или студии (Forton Lab).
- Менять бренд-параметры (палитра, шрифты, ToV) без явного согласования с пользователем.
- Упоминать имя автора (Алексей / Carbon) или техстек (Flutter, Supabase, Claude) в публичных постах.
- Публиковать в Rutube или Instagram (на холде).
- Удалять файлы из `~/Documents/Forton Lab/media/` после использования (это сырьё, оно остаётся).

## Как пушить

Cowork-sandbox блокирует `git commit` (unlink в монтированной папке).
В Cowork-сессии я готовлю файлы и даю пользователю готовую команду.
В Claude Code на маке — пуш делается обычным образом:

```bash
cd ~/Documents/Forton\ Lab/marketing-v3 && \
rm -f .git/index.lock && \
git add -A && \
git -c user.name="Carbon" -c user.email="carbon.arma3@gmail.com" \
  commit -m "<message>" && \
git pull --rebase && \
git push
```
