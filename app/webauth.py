"""Вход в само приложение: страница логина и сессия на подписанной cookie.

Это отдельный слой от авторизации в TradeMap. Он решает, кого пускать на
страницу, и не имеет отношения к учётной записи trademap.org.

Если TRADEMAP_APP_USER / TRADEMAP_APP_PASSWORD_HASH / TRADEMAP_SESSION_SECRET
не заданы, вход не спрашивается — так удобно работать локально через ./run.sh.
На сервере эти переменные обязательны, за этим следит docker-compose.yml.
"""

from __future__ import annotations

import hmac
import time
from pathlib import Path
from typing import Optional

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response

from . import config

COOKIE_NAME = "trademap_session"
STATIC_DIR = Path(__file__).parent / "static"

# Пути, доступные без входа: сама форма, её оформление и проверка живости.
PUBLIC_PATHS = {"/login", "/health", "/static/login.css", "/favicon.ico"}


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.SESSION_SECRET, salt="trademap-login")


def verify_password(password: str) -> bool:
    """Проверяет пароль против bcrypt-хеша из .env."""
    try:
        return bcrypt.checkpw(password.encode(), config.APP_PASSWORD_HASH.encode())
    except (ValueError, TypeError):
        # Битый или пустой хеш — считаем, что пароль не подошёл, но не падаем.
        return False


def verify_user(username: str) -> bool:
    # Сравнение в постоянном времени: имя пользователя тоже секрет, пусть и слабый.
    return hmac.compare_digest(username.strip(), config.APP_USER)


def issue_session(response: Response, username: str) -> None:
    token = _serializer().dumps({"u": username, "t": int(time.time())})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=config.SESSION_MAX_AGE_SEC,
        httponly=True,
        samesite="lax",
        # Secure нельзя включать безусловно: локально приложение работает по http,
        # и браузер просто выбросил бы cookie.
        secure=not config.ALLOW_BROWSER_LOGIN,
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def current_user(request: Request) -> Optional[str]:
    if not config.auth_enabled():
        return config.APP_USER or "local"
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=config.SESSION_MAX_AGE_SEC)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("u")


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS


async def auth_middleware(request: Request, call_next):
    """Пускает дальше только со свежей сессией.

    Страницы отправляются на форму входа, запросы к /api/ получают 401 —
    иначе фронтенд получил бы HTML вместо JSON и показал невнятную ошибку.
    """
    if not config.auth_enabled() or is_public(request.url.path):
        return await call_next(request)

    if current_user(request) is not None:
        return await call_next(request)

    if request.url.path.startswith("/api/"):
        return Response(
            content='{"detail":"Сессия истекла. Войдите заново."}',
            status_code=401,
            media_type="application/json",
        )

    target = "/login"
    if request.url.path != "/":
        # Куда вернуть после успешного входа.
        target += f"?next={request.url.path}"
    return RedirectResponse(target, status_code=303)


def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")
