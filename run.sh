#!/usr/bin/env bash
# Запуск TradeMap Explorer. Первый запуск сам создаст venv и поставит зависимости.
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"

if [ ! -d "$VENV" ]; then
  echo "==> Создаю виртуальное окружение..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  echo "==> Устанавливаю зависимости (кроме playwright — он нужен не всегда)..."
  "$VENV/bin/pip" install --quiet $(grep -vE '^\s*(#|$|playwright)' requirements.txt)
fi

if [ ! -f .env ]; then
  echo "==> .env не найден, создаю из .env.example"
  cp .env.example .env
  echo "    Впишите в .env логин и пароль от TradeMap, если нужны месячные данные."
fi

PORT="$(grep -E '^TRADEMAP_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d ' ' || true)"
PORT="${PORT:-8765}"

echo "==> TradeMap Explorer: http://127.0.0.1:${PORT}"
exec "$VENV/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
