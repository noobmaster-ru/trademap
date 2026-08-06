"""Вход в приложение — он же вход в TradeMap.

Своей базы пользователей у инструмента нет: логин и пароль проверяются прямо
в TradeMap, и пускаем тех, кого пускает он. Пароль после проверки нигде не
сохраняется — сразу меняется на токены.

В cookie лежит только имя пользователя, подписанное ключом сессии.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response

from . import config
from .auth import AuthError, InvalidCredentials, TokenProvider, token_registry

COOKIE_NAME = "trademap_session"
STATIC_DIR = Path(__file__).parent / "static"

# Пути, доступные без входа: сама форма, её оформление и проверка живости.
PUBLIC_PATHS = {"/login", "/health", "/static/login.css", "/favicon.ico"}


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.session_secret(), salt="trademap-login")


async def authenticate(username: str, password: str) -> Optional[str]:
    """Проверяет учётные данные в TradeMap.

    Возвращает имя пользователя при успехе и None, если TradeMap их отверг.
    Ошибки самого сервиса (недоступен, грант запрещён) пробрасываются наверх:
    их нельзя показывать как «неверный пароль».
    """
    username = username.strip()
    if not username or not password:
        return None
    try:
        await token_registry.for_user(username).login(password)
    except InvalidCredentials:
        return None
    return username


def tokens_for(request: Request) -> Optional[TokenProvider]:
    """Провайдер токенов вошедшего пользователя."""
    user = current_user(request)
    return token_registry.for_user(user) if user else None


def issue_session(response: Response, username: str, *, request_is_https: bool) -> None:
    token = _serializer().dumps({"u": username, "t": int(time.time())})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=config.SESSION_MAX_AGE_SEC,
        httponly=True,
        samesite="lax",
        # Secure нельзя включать безусловно: локально приложение работает по http,
        # и браузер просто выбросил бы cookie.
        # Secure только по HTTPS: локально приложение работает по http,
        # и браузер просто выбросил бы такую cookie.
        secure=request_is_https,
        path="/",
    )


def clear_session(response: Response, username: Optional[str]) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")
    if username:
        # Вместе с сессией забываем и токены TradeMap этого пользователя.
        token_registry.forget(username)


def current_user(request: Request) -> Optional[str]:
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
    if is_public(request.url.path):
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
