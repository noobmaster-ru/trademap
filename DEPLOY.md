# Деплой на сервер (Ubuntu / Debian)

Схема: приложение в контейнере, снаружи — Caddy с автоматическим HTTPS и паролем.
Наружу открыты только порты 80 и 443; само приложение доступно лишь внутри
docker-сети.

```
интернет ──▶ Caddy (443, HTTPS + пароль) ──▶ app:8765 ──▶ api.trademap.org
```

---

## 1. Что нужно до начала

- Сервер с Ubuntu 22.04/24.04 или Debian 12, доступ по SSH с правами `sudo`.
- Домен, A-запись которого указывает на IP сервера (без этого Let's Encrypt
  не выпишет сертификат).
- Открытые порты 80 и 443.
- Логин и пароль от TradeMap.

Проверить, что домен уже смотрит на сервер:

```bash
dig +short trademap.example.com     # должен вернуть IP вашего сервера
curl -s ifconfig.me                 # выполнить НА сервере — его внешний IP
```

---

## 2. Установка Docker

Команды для Ubuntu и Debian. Выполняются на сервере под пользователем с `sudo`.

```bash
# Убираем пакеты из репозиториев дистрибутива — они устаревшие и конфликтуют
# с официальными. Ошибки «пакет не найден» здесь нормальны.
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
  sudo apt-get remove -y $pkg
done

# Ключ и репозиторий Docker
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL "https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg" \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Сам Docker и плагин compose
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

# Автозапуск после перезагрузки сервера
sudo systemctl enable --now docker
```

Проверка:

```bash
sudo docker run --rm hello-world
sudo docker compose version
```

Чтобы не писать `sudo` перед каждой командой:

```bash
sudo usermod -aG docker $USER
newgrp docker          # или просто перезайти по SSH
```

> Учтите: членство в группе `docker` равносильно правам root на этой машине.

---

## 3. Файрвол

Если включён `ufw`:

```bash
sudo ufw allow 22/tcp     # сначала SSH, иначе можно отрезать себе доступ
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

---

## 4. Загрузка проекта на сервер

На сервере:

```bash
sudo mkdir -p /opt/trademap
sudo chown $USER:$USER /opt/trademap
git clone https://github.com/noobmaster-ru/trademap.git /opt/trademap
cd /opt/trademap
```

Для приватного репозитория понадобится доступ — проще всего через
personal access token:

```bash
git clone https://<токен>@github.com/noobmaster-ru/trademap.git /opt/trademap
```

`.env` в репозитории нет и быть не должно — он в `.gitignore`, а секреты
заполняются прямо на сервере на следующем шаге.

---

## 5. Настройка

На сервере сначала задайте пароль для входа в приложение:

```bash
cd /opt/trademap
sudo apt-get install -y apache2-utils   # даёт htpasswd, если его ещё нет
./setup-auth.sh
```

Скрипт спросит логин и пароль, посчитает bcrypt-хеш и запишет его в `.env`.
Считает локально, через `htpasswd` — в сеть не ходит, поэтому лимиты
Docker Hub на этом шаге не мешают.

> **Почему не вручную.** Bcrypt-хеш выглядит как `$2a$14$Zx9k...`, а docker
> compose трактует `$` в `.env` как подстановку переменной. Вставленный как есть,
> хеш обрежется до `$2a$14`, и вход перестанет работать — молча, без ошибок.
> Скрипт экранирует `$` в `$$`, как того требует compose. Если делаете вручную,
> удвойте каждый `$`.

Затем откройте `.env` и заполните остальное:

```ini
TRADEMAP_USERNAME=ваш-email-в-trademap
TRADEMAP_PASSWORD=ваш-пароль-в-trademap

TRADEMAP_DOMAIN=trademap.example.com
TRADEMAP_ACME_EMAIL=you@example.com
```

Проверить, что хеш дошёл до Caddy целиком:

```bash
docker compose config | grep BASICAUTH_HASH
```

Строка должна содержать хеш от `$$2a$$14$$` и до конца (около 60 символов).
Если оборвалась на `$$2a$$14` — `$` не экранированы.

> **Осторожно:** `docker compose config` печатает содержимое `.env` целиком,
> включая пароль от TradeMap. Не пересылайте вывод этой команды.

> Две пары логин/пароль здесь разные и не связаны:
> `TRADEMAP_USERNAME/PASSWORD` — ваш аккаунт на trademap.org,
> `TRADEMAP_BASICAUTH_*` — вход в само это приложение.

---

## 6. Запуск

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f
```

Первый запуск: Caddy запрашивает сертификат у Let's Encrypt — обычно 10–30 секунд.
В логах должно появиться `certificate obtained successfully`.

Откройте `https://trademap.example.com`, введите логин и пароль из
`TRADEMAP_BASICAUTH_*`. Значок в правом верхнем углу должен показать
«вход выполнен» — значит, приложение авторизовалось в TradeMap само.

Проверка изнутри сервера, без браузера:

```bash
curl -u admin:ВАШ_ПАРОЛЬ https://trademap.example.com/api/auth/status
```

Ожидаемо: `"authenticated":true,"obtainedVia":"password"`.

---

## 7. Обслуживание

```bash
docker compose logs -f app          # логи приложения
docker compose restart app          # перезапуск
docker compose down                 # остановить всё
docker compose up -d --build        # обновить после правки кода
docker compose pull && docker compose up -d   # обновить Caddy
```

Обновление кода (после `git push` с локальной машины):

```bash
ssh user@server
cd /opt/trademap
git pull
docker compose up -d --build
```

`git pull` не тронет `.env` — он не отслеживается.

Токен и кэш справочников лежат в томе `trademap-data` и переживают пересборку.
Сбросить их:

```bash
docker compose down
docker volume rm trademap_trademap-data
docker compose up -d
```

---

## Особенности этой конфигурации

**Вход в TradeMap.** На сервере используется прямой вход по логину и паролю
(`grant_type=password`) — запасной путь через окно браузера отключён
(`TRADEMAP_ALLOW_BROWSER_LOGIN=0`), потому что графической среды там нет.
Токен обновляется сам; при перезапуске контейнера берётся из тома.

**Playwright в образ не входит** — он нужен только для браузерного входа.
Благодаря этому образ весит ~260 МБ вместо ~700 МБ.

**Ограничение скорости никуда не делось.** API TradeMap блокирует клиента при
частых обращениях — в наших замерах блокировка держалась около получаса.
На сервере это опаснее, чем локально: запросы идут с одного IP от всех
пользователей сразу. Если доступ получат несколько человек, имеет смысл
снизить темп в `.env`:

```ini
TRADEMAP_MAX_TASKS=100
```

**У приложения нет разграничения прав.** Базовая авторизация Caddy — это один
общий пароль на всех: любой, кто его знает, работает от вашей учётной записи
TradeMap. Для более узкого доступа лучше не открывать домен наружу, а ходить
через SSH-туннель:

```bash
# в docker-compose.yml заменить у caddy ports на "127.0.0.1:8080:80"
ssh -L 8765:localhost:8080 user@server
```

---

## Если что-то пошло не так

**Сертификат не выпускается.** Проверьте, что домен резолвится в IP сервера
(`dig +short ваш-домен`) и что порт 80 открыт — Let's Encrypt проверяет
владение доменом именно через него. Смотрите `docker compose logs caddy`.

**502 Bad Gateway.** Приложение ещё поднимается или упало:
`docker compose logs app`, `docker compose ps` (статус должен быть `healthy`).

**«без входа — месячные данные закрыты».** Проверьте `TRADEMAP_USERNAME`
и `TRADEMAP_PASSWORD` в `.env`, затем `docker compose restart app`.
Диагностика внутри контейнера:

```bash
docker compose exec app python -m app.auth --check
```

**403 «You have been blacklisted».** Это TradeMap, не ваш сервер. Только ждать;
затем дробить выгрузки на части.

**`429 Too Many Requests` при загрузке образов.** Docker Hub ограничивает
анонимные загрузки по IP, и на VPS этот лимит часто уже исчерпан соседями.
Ошибка выглядит так:

```
failed to resolve reference "docker.io/library/caddy:2-alpine":
unexpected status from HEAD request ...: 429 Too Many Requests
```

Три способа обойти, по возрастанию усилий:

1. **Войти в Docker Hub** — авторизованным даётся заметно больший лимит.
   Бесплатной учётной записи достаточно:
   ```bash
   docker login
   ```
2. **Подключить зеркало** — учётная запись не нужна:
   ```bash
   sudo mkdir -p /etc/docker
   echo '{"registry-mirrors": ["https://mirror.gcr.io"]}' | sudo tee /etc/docker/daemon.json
   sudo systemctl restart docker
   docker compose up -d --build
   ```
3. **Просто подождать** — лимит восстанавливается сам в течение нескольких часов.

Генерации пароля это не касается: `setup-auth.sh` считает хеш локально через
`htpasswd` и в сеть не ходит.
