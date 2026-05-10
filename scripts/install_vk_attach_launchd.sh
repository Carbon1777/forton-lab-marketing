#!/usr/bin/env bash
# Установить LaunchAgent для автоматической подготовки vk_attach.
# Использование: bash scripts/install_vk_attach_launchd.sh
set -euo pipefail

PLIST_SRC="$(dirname "$0")/com.fortonlab.vk-attach-prep.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.fortonlab.vk-attach-prep.plist"

mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"
echo "  ✓ скопирован plist → $PLIST_DST"

# Если уже загружен — выгрузим и перезагрузим
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load -w "$PLIST_DST"
echo "  ✓ launchctl load OK"

# Сразу запустить
launchctl start com.fortonlab.vk-attach-prep
echo "  ✓ запущен впервые"

echo
echo "Логи: tail -f /tmp/fortonlab-vk-attach.log"
echo "Статус: launchctl list | grep fortonlab"
echo "Запустить вручную: launchctl start com.fortonlab.vk-attach-prep"
echo "Удалить: launchctl unload -w $PLIST_DST"
