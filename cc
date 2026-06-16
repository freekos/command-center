#!/usr/bin/env bash
# Command Center launcher.
#   cc            start (if needed) + open in the browser
#   cc update     git pull + restart + open      (updating the tool)
#   cc restart    restart the server
#   cc stop       stop the server
#   cc status     is it running?
# The server is started detached so it survives this terminal closing.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PORT="${CC_PORT:-8787}"
URL="http://127.0.0.1:${PORT}"
PY="$(command -v python3 || true)"
LOG="$DIR/server.log"

is_up(){ curl -s -m 2 -o /dev/null "$URL/" 2>/dev/null; }
stop(){ lsof -ti "tcp:$PORT" 2>/dev/null | xargs kill 2>/dev/null || true; }
start(){
  [ -n "$PY" ] || { echo "❌ python3 не найден"; exit 1; }
  [ -f "$DIR/server.py" ] || { echo "❌ нет $DIR/server.py"; exit 1; }
  ( cd "$DIR" && CC_PORT="$PORT" nohup "$PY" server.py >>"$LOG" 2>&1 & disown ) || true
  for _ in $(seq 1 50); do is_up && return 0; sleep 0.2; done
  return 1
}
open_browser(){
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"; fi
}

case "${1:-open}" in
  open|"")
    if is_up; then echo "✓ Командный центр уже работает → $URL"
    else echo "▶ Поднимаю на :$PORT …"; start || { echo "❌ не ответил, лог: $LOG"; tail -n 15 "$LOG" 2>/dev/null||true; exit 1; }; echo "✓ Готов → $URL"; fi
    open_browser ;;
  update)
    if [ -d "$DIR/.git" ]; then
      echo "▶ Обновляю код (git pull)…"
      git -C "$DIR" pull --ff-only && echo "✓ Код обновлён ($(git -C "$DIR" rev-parse --short HEAD))"
    else
      echo "⚠ $DIR — не git-репозиторий, обновить через git нельзя."
      echo "  Переустанови: curl -fsSL https://raw.githubusercontent.com/freekos/command-center/main/install.sh | bash"
    fi
    echo "▶ Перезапускаю сервер…"; stop; sleep 1
    start && { echo "✓ Готов → $URL"; open_browser; } || { echo "❌ не поднялся, лог: $LOG"; exit 1; } ;;
  restart)
    echo "▶ Перезапуск…"; stop; sleep 1
    start && echo "✓ $URL" || { echo "❌ не поднялся"; exit 1; } ;;
  stop)
    stop; echo "✓ остановлен" ;;
  status)
    is_up && echo "✓ работает → $URL" || echo "✗ не запущен" ;;
  passcode)
    "${PY:-python3}" "$DIR/server.py" passcode ;;
  expose)
    "${PY:-python3}" "$DIR/server.py" bind 0.0.0.0
    echo "▶ Перезапуск с доступом по сети…"; stop; sleep 1
    if start; then
      echo "✓ Доступен по сети на :$PORT"
      if command -v tailscale >/dev/null 2>&1; then
        ip="$(tailscale ip -4 2>/dev/null | head -1)"
        [ -n "$ip" ] && echo "  Tailscale: http://$ip:$PORT  (открой с телефона)"
      fi
      command -v ipconfig >/dev/null 2>&1 && echo "  LAN: http://$(ipconfig getifaddr en0 2>/dev/null):$PORT"
    else
      echo "❌ не поднялся — вероятно не задан пасскод. Сначала: cc passcode"; exit 1
    fi ;;
  local)
    "${PY:-python3}" "$DIR/server.py" bind 127.0.0.1
    echo "▶ Перезапуск (только локально)…"; stop; sleep 1
    start && echo "✓ только 127.0.0.1 → $URL" || { echo "❌ не поднялся"; exit 1; } ;;
  *)
    echo "cc [open|update|restart|stop|status|passcode|expose|local]" ;;
esac
