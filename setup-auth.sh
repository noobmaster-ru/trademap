#!/usr/bin/env bash
# Настраивает вход в приложение: логин, хеш пароля и ключ подписи сессии.
# Всё записывается в .env.
#
# Зачем отдельный скрипт: bcrypt-хеш содержит символы «$», а docker compose
# трактует их в .env как подстановку переменных. Хеш $2a$14$Zx9k... превратился
# бы в $2a$14 — авторизация просто перестала бы работать, причём молча.
# Здесь «$» экранируются в «$$», как того требует compose.

set -euo pipefail
cd "$(dirname "$0")"

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

# Хеш считаем локально через htpasswd: Docker Hub ограничивает анонимные
# загрузки образов, и на сервере легко упереться в «429 Too Many Requests»
# ровно на этом шаге. Сеть здесь не нужна вовсе.
#
# Алгоритм фиксируем явно (-B = bcrypt, -C 14 = стоимость): директива
# basic_auth в Caddyfile по умолчанию ожидает именно bcrypt, а caddy
# hash-password умеет отдавать ещё и argon2id — при расхождении вход
# не пускал бы без внятной причины.
echo "==> Считаю bcrypt-хеш..."

if command -v htpasswd >/dev/null 2>&1; then
  RAW="$(htpasswd -bnBC 14 "" "$PASSWORD" | tr -d ':\n')"
  # htpasswd помечает хеш префиксом $2y$, Caddy привычнее $2a$.
  # Алгоритм один и тот же, отличается только маркер версии.
  HASH="$(printf '%s' "$RAW" | sed 's/^\$2y\$/\$2a\$/')"
elif command -v docker >/dev/null 2>&1; then
  echo "    htpasswd не найден, пробую через docker..."
  HASH="$(docker run --rm caddy:2-alpine caddy hash-password \
    --algorithm bcrypt --plaintext "$PASSWORD")"
else
  echo "Нужен htpasswd. Установите его:" >&2
  echo "  sudo apt-get install -y apache2-utils" >&2
  exit 1
fi

case "$HASH" in
  \$2*\$*\$*) : ;;   # ожидаемый вид bcrypt-хеша
  *) echo "Получился неожиданный хеш: ${HASH:0:20}..." >&2; exit 1 ;;
esac

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

set_var TRADEMAP_APP_USER "$USER_NAME"
set_var TRADEMAP_APP_PASSWORD_HASH "$ESCAPED"

# Ключом подписывается cookie сессии. Меняется только по необходимости:
# новый ключ разлогинивает всех, поэтому существующий не трогаем.
if ! grep -qE "^TRADEMAP_SESSION_SECRET=.+" .env; then
  SECRET="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  set_var TRADEMAP_SESSION_SECRET "$SECRET"
  echo "==> Сгенерирован ключ подписи сессии"
else
  echo "==> Ключ подписи сессии уже есть, оставляю прежний"
fi

chmod 600 .env

echo
echo "Готово. В .env записано:"
echo "  TRADEMAP_APP_USER=$USER_NAME"
echo "  TRADEMAP_APP_PASSWORD_HASH=${ESCAPED:0:12}... (символы \$ экранированы как \$\$ — так и должно быть)"
echo "  TRADEMAP_SESSION_SECRET=... (ключ подписи cookie)"
echo
echo "Проверить, что compose видит хеш целиком:"
echo "  docker compose config | grep APP_PASSWORD_HASH"
echo
echo "Дальше: заполните в .env домен и учётную запись TradeMap, затем"
echo "  docker compose up -d --build"
