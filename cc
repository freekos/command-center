#!/usr/bin/env bash
# Command Center launcher — vibe-kanban-style:
#   one command -> start local server (if not already up) -> open it in the browser.
# Idempotent: re-running just opens the dashboard (no double-start).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PORT="${CC_PORT:-8787}"
URL="http://127.0.0.1:${PORT}"
PY="$(command -v python3 || true)"
LOG="$DIR/server.log"

[ -n "$PY" ] || { echo "❌ python3 не найден в PATH"; exit 1; }
[ -f "$DIR/server.py" ] || { echo "❌ нет $DIR/server.py"; exit 1; }

is_up() { curl -s -m 2 -o /dev/null "$URL/" 2>/dev/null; }

if is_up; then
  echo "✓ Командный центр уже работает → $URL"
else
  echo "▶ Поднимаю Командный центр на :$PORT …"
  # detached so it survives this terminal closing (real persistence for a login session)
  ( cd "$DIR" && nohup "$PY" server.py >>"$LOG" 2>&1 & disown ) || true
  # wait up to ~10s for it to answer
  for _ in $(seq 1 50); do
    if is_up; then break; fi
    sleep 0.2
  done
  if is_up; then
    echo "✓ Готов → $URL"
  else
    echo "❌ сервер не ответил, лог: $LOG"; tail -n 20 "$LOG" 2>/dev/null || true; exit 1
  fi
fi

# open in the system default browser (bypasses cmux internal browser)
if command -v open >/dev/null 2>&1; then
  open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL"
fi
