"""channel_metrics — еженедельный дайджест охватов СВОИХ каналов студии.

Additive-пакет (Phase «Channel analytics», 2026-07): собирает подписчиков
Telegram / VK / YouTube на УЖЕ существующих токенах (новых обязательных
секретов нет), считает Δ неделя-к-неделе по снапшоту и шлёт один дайджест
в TG-канал «Планировщик» — рядом со store/hybrid отчётами.

Зеркалит структуру src/store_metrics/ (models / snapshot / *fetchers* / digest
/ cli), но полностью самодостаточен: ничего не импортирует из других пакетов
публикатора и не меняет их поведение.

Каналы (SECRETS.md): TG @fortonlab (бот-админ → getChatMemberCount на
TG_BOT_TOKEN), VK vk.com/fortonlab (VK_GROUP_TOKEN/VK_GROUP_ID → groups.getById
members + best-effort stats.get reach), YouTube @fortonlab (публичная
статистика через опциональный YT_API_KEY — деградирует, если ключа нет).
Дзен API не имеет — в дайджест не входит.
"""
