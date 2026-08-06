"""Конфигурация приложения: чтение .env, пути кэша, константы API TradeMap."""

from __future__ import annotations

import os
import secrets
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
OIDC_CLIENT_ID = "TradeMap"
OIDC_SCOPE = "openid offline_access profile TradeMap.API Account.API"

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
# Токены хранятся по одному файлу на пользователя: каждый входит своей
# учётной записью TradeMap, и смешивать их нельзя.
TOKEN_DIR = CACHE_DIR / "tokens"
REFERENCE_CACHE_DIR = CACHE_DIR / "reference"

# --- Сессия -----------------------------------------------------------------
# Вход в приложение — это вход в TradeMap: своей базы пользователей нет,
# пускаем тех, кого пускает сам TradeMap. Здесь только ключ подписи cookie.
#
# Если ключ не задан, он генерируется сам и сохраняется в кэше: так деплой
# не требует лишних действий. Явно задать имеет смысл, только если нужно,
# чтобы сессии переживали очистку кэша.
_SESSION_SECRET_ENV = os.getenv("TRADEMAP_SESSION_SECRET", "").strip()

# Сколько живёт сессия без повторного входа.
SESSION_MAX_AGE_SEC = int(os.getenv("TRADEMAP_SESSION_MAX_AGE", str(14 * 24 * 3600)))


def session_secret() -> str:
    if _SESSION_SECRET_ENV:
        return _SESSION_SECRET_ENV
    ensure_dirs()
    path = CACHE_DIR / "session.key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        os.chmod(path, 0o600)
    return path.read_text().strip()

# Справочники меняются редко — держим сутки.
REFERENCE_TTL_SEC = 24 * 3600


def ensure_dirs() -> None:
    CACHE_DIR.mkdir(mode=0o700, exist_ok=True)
    REFERENCE_CACHE_DIR.mkdir(mode=0o700, exist_ok=True)
    TOKEN_DIR.mkdir(mode=0o700, exist_ok=True)


def has_credentials() -> bool:
    return bool(USERNAME and PASSWORD)
