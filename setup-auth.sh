#!/usr/bin/env bash
# Генерирует хеш пароля для входа в приложение и записывает его в .env.
#
# Зачем отдельный скрипт: bcrypt-хеш содержит символы «$», а docker compose
# трактует их в .env как подстановку переменных. Хеш $2a$14$Zx9k... превратился
# бы в $2a$14 — авторизация просто перестала бы работать, причём молча.
# Здесь «$» экранируются в «$$», как того требует compose.

set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Нужен docker — он используется для генерации хеша." >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo "==> .env не найден, создаю из .env.example"
  cp .env.example .env
fi

read -rp "Логин для входа в приложение [admin]: " USER_NAME
USER_NAME="${USER_NAME:-admin}"

read -rsp "Пароль: " PASSWORD; echo
read -rsp "Повторите пароль: " PASSWORD2; echo
if [ "$PASSWORD" != "$PASSWORD2" ]; then
  echo "Пароли не совпадают." >&2
  exit 1
fi
if [ ${#PASSWORD} -lt 8 ]; then
  echo "Пароль короче 8 символов — это единственная защита приложения." >&2
  exit 1
fi

# Алгоритм задаём явно: hash-password умеет ещё и argon2id, а директива
# basic_auth в Caddyfile по умолчанию ожидает именно bcrypt. При расхождении
# вход просто не пускал бы, без внятной причины.
echo "==> Считаю bcrypt-хеш..."
HASH="$(docker run --rm caddy:2-alpine caddy hash-password \
  --algorithm bcrypt --plaintext "$PASSWORD")"

# Экранируем $ -> $$ для docker compose.
ESCAPED="${HASH//\$/\$\$}"

# Заменяем строки в .env, сохраняя остальное содержимое.
set_var() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env; then
    # Разделитель | — в bcrypt-хеше его быть не может.
    sed -i.bak "s|^${key}=.*|${key}=${value}|" .env && rm -f .env.bak
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

set_var TRADEMAP_BASICAUTH_USER "$USER_NAME"
set_var TRADEMAP_BASICAUTH_HASH "$ESCAPED"

chmod 600 .env

echo
echo "Готово. В .env записано:"
echo "  TRADEMAP_BASICAUTH_USER=$USER_NAME"
echo "  TRADEMAP_BASICAUTH_HASH=${ESCAPED:0:12}... (символы \$ экранированы как \$\$ — так и должно быть)"
echo
echo "Проверить, что compose видит хеш целиком:"
echo "  docker compose config | grep BASICAUTH_HASH"
echo
echo "Дальше: заполните в .env домен и учётную запись TradeMap, затем"
echo "  docker compose up -d --build"
