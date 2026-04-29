# Forton Lab — Marketing Auto-Publisher

Автоматический публикатор контента для каналов студии Forton Lab.

## Каналы

- **Telegram:** [@fortonlab](https://t.me/fortonlab)
- **VK:** [vk.com/fortonlab](https://vk.com/fortonlab)
- **Дзен:** [dzen.ru/fortonlab](https://dzen.ru/fortonlab) — автомат через TG-кросспостинг

## Архитектура

```
queue/<slug>.md  →  PR  →  merge  →  GitHub Actions  →  TG + VK
```

Подробнее — см. `docs/ARCHITECTURE.md` (будет добавлено).

## Статус

🚧 v0.1 в разработке.
