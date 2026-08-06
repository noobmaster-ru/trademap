# Деплой на сервер (Ubuntu / Debian)

Схема: приложение в контейнере, снаружи — Caddy с автоматическим HTTPS.
Вход закрыт страницей логина; пускает она по учётной записи trademap.org.
Наружу открыты только порты 80 и 443; само приложение доступно лишь внутри
docker-сети.

```
интернет ──▶ Caddy (443, HTTPS) ──▶ app:8765 (страница входа) ──▶ api.trademap.org
```

---

## 1. Что нужно до начала

- Сервер с Ubuntu 22.04/24.04 или Debian 12, доступ по SSH с правами `sudo`.
- Домен, A-запись которого указывает на IP сервера (без этого Let's Encrypt
  не выпишет сертификат).
- Открытые порты 80 и 443.
- Учётная запись на trademap.org у каждого, кто будет пользоваться.

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

Единственное обязательное — домен:

```bash
cd /opt/trademap
cp .env.example .env
nano .env
```

```ini
TRADEMAP_DOMAIN=trademap.example.com
```

> Это должен быть **ваш реальный домен**, а не оставленный из примера
> `trademap.example.com` — иначе сертификат не выпустится никогда.

Паролей в `.env` нет и не нужно: каждый пользователь входит своей учётной
записью trademap.org на странице логина. Ключ подписи сессии приложение
генерирует само и хранит в томе.

## 6. Запуск

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f
```

Первый запуск: Caddy запрашивает сертификат у Let's Encrypt — обычно 10–30 секунд.
В логах должно появиться `certificate obtained successfully`.

Откройте `https://trademap.example.com` — появится страница входа. Введите
e-mail и пароль **от trademap.org**: отдельной учётной записи у инструмента
нет. После входа бейдж справа сверху покажет «TradeMap подключён».

Запросы идут от имени вошедшего, поэтому у каждого пользователя свои лимиты
и свой доступ к месячным рядам.

Проверка изнутри сервера, без браузера:

```bash
# Вход и сохранение cookie сессии
curl -c /tmp/c.txt -X POST https://trademap.example.com/login \
  -d "username=ВАШ_ЛОГИН" --data-urlencode "password=ВАШ_ПАРОЛЬ"

# Проверка состояния подключения к TradeMap
curl -b /tmp/c.txt https://trademap.example.com/api/auth/status
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

**Вход.** Пользователь вводит учётные данные trademap.org на странице логина,
приложение меняет их на токены (`grant_type=password`) и пароль не сохраняет.
Токены лежат в томе по файлу на пользователя и переживают перезапуск.

**Ограничение скорости никуда не делось.** API TradeMap блокирует клиента при
частых обращениях — в наших замерах блокировка держалась около получаса.
На сервере это опаснее, чем локально: запросы идут с одного IP от всех
пользователей сразу. Если доступ получат несколько человек, имеет смысл
снизить темп в `.env`:

```ini
TRADEMAP_MAX_TASKS=100
```

**Каждый работает от себя.** Общего пароля больше нет: у кого есть учётная
запись trademap.org, тот и войдёт — под своими лимитами и своим доступом
к месячным рядам. Сессия живёт две недели (`TRADEMAP_SESSION_MAX_AGE`
в секундах), кнопка «Выйти» сбрасывает её вместе с токенами. Для более узкого доступа лучше не открывать домен наружу, а ходить
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

**«сессия TradeMap истекла».** Refresh-токен отозван или устарел — нажмите
«Выйти» и войдите заново.

**Не пускает с верным паролем.** Проверьте учётные данные на самом
trademap.org: приложение лишь передаёт их дальше и своей базы не имеет.

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

