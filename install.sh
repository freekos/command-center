#!/usr/bin/env bash
# Command Center installer — prepares a local config and prints how to launch.
# No persistence is touched automatically (no edits to your shell profile).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

command -v python3 >/dev/null 2>&1 || { echo "❌ нужен python3 (на macOS уже есть)"; exit 1; }
command -v git     >/dev/null 2>&1 || { echo "❌ нужен git"; exit 1; }

chmod +x "$DIR/cc" "$DIR/server.py" 2>/dev/null || true

if [ ! -f "$DIR/config.json" ]; then
  cp "$DIR/config.example.json" "$DIR/config.json"
  chmod 600 "$DIR/config.json"
  echo "✓ создан config.json (из примера, права 600)"
else
  echo "✓ config.json уже есть — не трогаю"
fi

echo
echo "Готово. Запуск:"
echo "  $DIR/cc                 # поднять локальный сервер + открыть в браузере"
echo
echo "Чтобы запускать одной командой из любого места — добавь в ~/.zshrc (или ~/.bashrc):"
echo "  alias dash=\"$DIR/cc\""
echo
echo "Опционально: для Jira-проектов держи API-токен под рукой —"
echo "  https://id.atlassian.com/manage-profile/security/api-tokens"
echo "Для VCS-видов нужен авторизованный glab (GitLab) и/или gh (GitHub)."
