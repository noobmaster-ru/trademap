"""Конфигурация приложения: чтение .env, пути кэша, константы API TradeMap."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

# --- API TradeMap -----------------------------------------------------------
# Новый TradeMap (www.trademap.org) — Angular SPA поверх этого JSON API.
API_ROOT = "https://www.trademap.org/api"

# OIDC-провайдер ITC. client_id публичный (SPA), секрета нет.
IDENTITY_ROOT = "https://sts.marketanalysis.intracen.org"
TOKEN_ENDPOINT = f"{IDENTITY_ROOT}/connect/token"
AUTHORIZE_ENDPOINT = f"{IDENTITY_ROOT}/connect/authorize"
OIDC_CLIENT_ID = "TradeMap"
OIDC_SCOPE = "openid offline_access profile TradeMap.API Account.API"
OIDC_REDIRECT_URI = "https://www.trademap.org/auth-cb"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Сервер молча откатывается на pageSize=25, если запросить больше 500.
# Берём максимум: одна крупная страница вежливее двадцати мелких.
MAX_PAGE_SIZE = 500

# У API TradeMap есть защита от избыточной нагрузки: при слишком частых запросах
# он отвечает «403 Forbidden: You have been blacklisted» и какое-то время не
# обслуживает клиента. Поэтому темп сознательно низкий — приложение должно быть
# неотличимо от обычной работы человека в интерфейсе сайта.
MAX_CONCURRENCY = 2
REQUEST_SPACING_SEC = 1.0
MAX_RETRIES = 4

# --- Локальные настройки ----------------------------------------------------
USERNAME = os.getenv("TRADEMAP_USERNAME", "").strip()
PASSWORD = os.getenv("TRADEMAP_PASSWORD", "").strip()
PORT = int(os.getenv("TRADEMAP_PORT", "8765"))
DEFAULT_CURRENCY = os.getenv("TRADEMAP_CURRENCY", "USD").strip().upper() or "USD"

# Ограничение списка продуктов одной веткой HS. Пустое значение — весь классификатор.
# По умолчанию 0713 («бобовые овощи сушёные»): нужна эта группа и всё под ней.
PRODUCT_ROOT = os.getenv("TRADEMAP_PRODUCT_ROOT", "0713").strip()

# Сколько знаков сверх корня показывать; 0 — без ограничения.
#
# Глубина национальных тарифных линий у стран РАЗНАЯ, и обрезать её по числу
# знаков опасно: у Индии данные лежат на 8 знаках (0713+4), а у Австралии и США
# — на 10 (0713+6). Лимит «+4» молча выбросил бы весь национальный уровень
# двух стран из трёх, поэтому по умолчанию берём 6: это покрывает и 8-, и
# 10-значные коды. Уровни всё равно разделены в интерфейсе кнопками с числом
# кодов, так что лишнего не выберется незаметно.
PRODUCT_EXTRA_MAX = int(os.getenv("TRADEMAP_PRODUCT_EXTRA_MAX", "6"))

# Предел числа запросов на один прогон. Каждая комбинация «страна × продукт ×
# показатель» — это отдельное обращение к API (плюс страницы пагинации), а API
# блокирует клиента при избыточной нагрузке. Поднимайте осознанно.
MAX_TASKS = int(os.getenv("TRADEMAP_MAX_TASKS", "200"))

# В контейнере кэш выносится в том (TRADEMAP_CACHE_DIR=/data), чтобы токен и
# справочники переживали пересоздание контейнера.
CACHE_DIR = Path(os.getenv("TRADEMAP_CACHE_DIR", str(PROJECT_ROOT / ".cache")))
TOKEN_FILE = CACHE_DIR / "token.json"
REFERENCE_CACHE_DIR = CACHE_DIR / "reference"

# На headless-сервере запасной вход через окно браузера невозможен: показывать
# его в интерфейсе бессмысленно, а попытка запуска просто повесит запрос.
ALLOW_BROWSER_LOGIN = os.getenv("TRADEMAP_ALLOW_BROWSER_LOGIN", "1").strip() != "0"


# --- Вход в само приложение -------------------------------------------------
# Это отдельная от TradeMap защита: она решает, кого пускать на страницу.
#
# Если хеш пароля не задан, вход не спрашивается — так удобно работать локально
# через ./run.sh. На сервере переменные обязательны, за этим следит
# docker-compose.yml.
APP_USER = os.getenv("TRADEMAP_APP_USER", "").strip()
APP_PASSWORD_HASH = os.getenv("TRADEMAP_APP_PASSWORD_HASH", "").strip()

# Ключ подписи cookie сессии. Пустой означает «логин не настроен».
SESSION_SECRET = os.getenv("TRADEMAP_SESSION_SECRET", "").strip()

# Сколько живёт сессия без повторного входа.
SESSION_MAX_AGE_SEC = int(os.getenv("TRADEMAP_SESSION_MAX_AGE", str(14 * 24 * 3600)))


def auth_enabled() -> bool:
    return bool(APP_USER and APP_PASSWORD_HASH and SESSION_SECRET)

# Справочники меняются редко — держим сутки.
REFERENCE_TTL_SEC = 24 * 3600


def ensure_dirs() -> None:
    CACHE_DIR.mkdir(mode=0o700, exist_ok=True)
    REFERENCE_CACHE_DIR.mkdir(mode=0o700, exist_ok=True)


def has_credentials() -> bool:
    return bool(USERNAME and PASSWORD)
