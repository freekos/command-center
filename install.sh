#!/usr/bin/env bash
# Command Center installer.
#   From a clone:  git clone … && cd command-center && ./install.sh
#   One-liner:     curl -fsSL https://raw.githubusercontent.com/freekos/command-center/main/install.sh | bash
#
# Adds a `dash` alias to your shell so you launch from anywhere with one word,
# then opens the dashboard. Re-running is safe (idempotent).
set -euo pipefail

REPO="${CC_REPO:-https://github.com/freekos/command-center.git}"
DEST_DEFAULT="$HOME/.command-center"

# --- locate the package: a local clone, or fetch it (curl|bash mode) ---
SRC=""
_d="$(cd "$(dirname "${BASH_SOURCE[0]:-/nonexistent}")" 2>/dev/null && pwd || true)"
[ -n "$_d" ] && [ -f "$_d/server.py" ] && SRC="$_d"

if [ -n "$SRC" ]; then
  DIR="$SRC"
  echo "▶ Локальная копия: $DIR"
else
  command -v git >/dev/null 2>&1 || { echo "❌ нужен git"; exit 1; }
  DIR="${CC_HOME:-$DEST_DEFAULT}"
  if [ -d "$DIR/.git" ]; then
    echo "▶ Обновляю $DIR"; git -C "$DIR" pull --ff-only -q || true
  else
    echo "▶ Клонирую в $DIR"; git clone -q "$REPO" "$DIR"
  fi
fi

command -v python3 >/dev/null 2>&1 || { echo "❌ нужен python3 (на macOS уже есть)"; exit 1; }
chmod +x "$DIR/cc" "$DIR/server.py" 2>/dev/null || true

# --- local config from the example (holds tokens later -> 600, git-ignored) ---
if [ ! -f "$DIR/config.json" ]; then
  cp "$DIR/config.example.json" "$DIR/config.json"; chmod 600 "$DIR/config.json"
  echo "✓ создан config.json (права 600)"
else
  echo "✓ config.json уже есть"
fi

# --- `dash` alias, idempotent (CC_RC overrides the rc file, used for testing) ---
if [ -n "${CC_RC:-}" ]; then RC="$CC_RC"
else
  case "$(basename "${SHELL:-zsh}")" in
    zsh)  RC="$HOME/.zshrc" ;;
    bash) RC="$HOME/.bashrc" ;;
    *)    RC="$HOME/.profile" ;;
  esac
fi
if [ -f "$RC" ] && grep -q 'alias dash=' "$RC"; then
  echo "✓ alias dash уже есть в $RC — не трогаю"
else
  printf '\n# Command Center\nalias dash="%s/cc"\n' "$DIR" >> "$RC"
  echo "✓ добавил alias dash в $RC"
fi

echo
echo "Готово. Перезапусти терминал (или: source \"$RC\"), затем набери:"
echo "  dash         # поднять дашборд и открыть в браузере"
echo
echo "Jira-токен (для Jira-проектов): https://id.atlassian.com/manage-profile/security/api-tokens"
echo "VCS-виды (MR/PR): нужен авторизованный glab (GitLab) и/или gh (GitHub)."

# --- launch immediately when attached to a terminal (vibe-kanban style) ---
if [ -t 1 ]; then echo; echo "▶ Открываю дашборд…"; "$DIR/cc"; fi
