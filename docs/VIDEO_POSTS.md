# Видео-посты (Reels/Shorts) — конвейер

Решение владельца 2026-10-27: ролики (9:16, 25–30 с) публикуются через тот же конвейер,
что и текстовые посты: превью в Telegram → «Публикуй» → во все каналы.

## Расписание
- Видео: Пн / Ср / Пт, **в 20:00 МСК** (текст с картинками: Вт / Чт / Сб, днём — нахлёста нет).
- Очередь продуктов по 6 роликов, по кругу: Листвия → Lucea → Лапуля → Diktum → …
- Превью видео приходит в **18:00 МСК** даты публикации (за 2 часа до 20:00): CF cron `0 9,15,17 * * *` в 15:00 UTC запускает `preview_bot.yml` с `mode=video` (обычные посты — тик 09:00 UTC в режиме `regular`). Вручную: `gh workflow run preview_bot.yml -f mode=video`. Нажатие «Публикуй» ставит
  запись в статус `scheduled`; в 20:00 МСК CF Worker (cron `0 17 * * *`) запускает
  `publish_scheduled.yml` → `src/dispatch_scheduled.py` → `publish.yml` на каждый slug.
- Если «Публикуй» нажато уже после времени `publish_at_msk` (или поля нет) — публикация сразу.
- Бот вечернего тика живёт до одобрения (после 20:00 «Публикуй» публикует сразу); не одобрено — запись истекает по TTL.

## Каналы
| Канал | Как | Модуль |
|---|---|---|
| Telegram | sendVideo | `tg_post.py` |
| Дзен | кросспост из TG (≤20 МБ) | — |
| YouTube Shorts | Data API, заголовок из `youtube_title` | `youtube_post.py` |
| VK | текст на стену автоматом; видео крепит владелец | `vk_post.py` + `manual_package.py` |
| Rutube | вручную (публичного API нет) | `manual_package.py` |
| Instagram Reels, Facebook Reels | Graph API | `meta_post.py` |

Заголовок YouTube через API берётся **не из имени файла**, а из `youtube_title`
(если не задан — `title`, иначе slug). `video_plan_entry` по умолчанию подставляет первое
предложение текста. Имя файла в репозитории всё равно осмысленное: `assets/video/<slug>.mp4`.

`manual_package.py` после публикации присылает владельцу в личку (планировщик-бот) видео
и текст для ручной выкладки в VK/Rutube (каналы `vk`/`rutube` в `channels:`).

## Добавить ролик в план
```bash
python -m src.video_plan_entry \
  --video "~/Documents/Forton Lab/media/Listvia/Promo/listvia_promo_02_opredelitel.mp4" \
  --slug listvia-promo-02-opredelitel --date 2026-11-02 --product listvia \
  --descriptions "~/Documents/Forton Lab/media/Listvia/Promo/описания.txt" --number 2 \
  --youtube-tags "Листвия,растения,уход за растениями"
```
Скрипт копирует файл в `assets/video/`, проверяет ≤19 МБ, вертикаль, ≤90 с, считает sha256,
прогоняет brand-lint и дописывает запись в `plans/monthly_plan_YYYY-MM.md` (месяц должен
существовать). Для немедленной публикации после одобрения: `--publish-at ""`.

## Instagram / Facebook: настройка (один раз)
1. `developers.facebook.com` → «Начать» → регистрация разработчика (аккаунт владельца Facebook, подтверждение телефоном).
2. Создать приложение (тип «Бизнес»), привязать к бизнес-порталу Forton Lab. Режим «В разработке» — App Review для своих аккаунтов не нужен (проверить на первой публикации).
3. Добавить продукты: Facebook Login for Business и Instagram. Права: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`, `instagram_content_publish`, `business_management`.
4. Graph API Explorer → токен пользователя с этими правами → обменять на долгоживущий → получить **Page access token** страницы «Forton Lab» (он бессрочный).
5. GitHub Secrets репозитория: `META_PAGE_TOKEN`, `META_PAGE_ID` (страница Facebook), `META_IG_USER_ID` (Instagram Business аккаунт `forton_lab`). Значения в чат и в репозиторий не класть.
6. Без `META_PAGE_TOKEN` шаг Meta в `publish.yml` сам пропускается — остальные каналы не страдают.

Реквизиты API (эндпоинты, `rupload.facebook.com`, лимиты Reels: MP4/H.264, до 90 с) собраны из
документации Meta; в коде они **не проверялись на живом токене** — первая публикация это проверка.

## Деплой CF Worker
Один cron `0 9,15,17 * * *` (12:00 / 18:00 / 20:00 МСК; на Workers Free лимит 5 триггеров на аккаунт — поэтому три тика в одном выражении) активируется только после
`cd cf-worker/forton-cron-trigger && npx wrangler deploy` (мапа в `src/index.js` и `wrangler.jsonc` уже синхронизированы).

## Тест
Запись `video-pipeline-test` (дата 2026-10-28, публикация сразу после одобрения; на YouTube пойдёт публично):
после публикации владелец удаляет пост во всех каналах и убирает запись/видео из репозитория.
