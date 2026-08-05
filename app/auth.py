"""Получение и обновление OIDC-токена TradeMap.

Токен нужен только для месячных (monthly) данных: годовые и квартальные ряды
API отдаёт анонимно.

Каскад способов — от самого дешёвого к самому надёжному:

1. Кэш на диске (.cache/token.json, права 0600) — если access_token ещё жив.
2. refresh_token — тихое обновление без участия пользователя.
3. Прямой вход по логину/паролю (ROPC, grant_type=password). Сервер авторизации
   объявляет поддержку этого гранта; разрешён ли он клиенту TradeMap — выясняется
   на первой попытке и запоминается, чтобы не долбиться повторно.
4. Браузерный вход (Playwright): полноценный authorization code + PKCE.
   Открывается окно браузера, пользователь входит сам (это же переживёт капчу
   и двухфакторку). Из ответа забираем refresh_token, дальше хватает п.2.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, asdict
from typing import Optional

import httpx

from . import config


class AuthError(RuntimeError):
    """Не удалось получить токен. Сообщение пригодно для показа пользователю."""


@dataclass
class Token:
    access_token: str
    refresh_token: Optional[str]
    expires_at: float
    obtained_via: str

    @property
    def is_fresh(self) -> bool:
        # 60 секунд запаса, чтобы токен не протух посреди пачки запросов.
        return bool(self.access_token) and time.time() < self.expires_at - 60

    @property
    def expires_in(self) -> int:
        return max(0, int(self.expires_at - time.time()))


def _load_token() -> Optional[Token]:
    try:
        raw = json.loads(config.TOKEN_FILE.read_text())
        return Token(**raw)
    except (OSError, ValueError, TypeError):
        return None


def _save_token(token: Token) -> None:
    config.ensure_dirs()
    config.TOKEN_FILE.write_text(json.dumps(asdict(token), indent=2))
    os.chmod(config.TOKEN_FILE, 0o600)


def _token_from_response(payload: dict, via: str) -> Token:
    return Token(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_at=time.time() + float(payload.get("expires_in", 3600)),
        obtained_via=via,
    )


# --- Флаг «ROPC для этого клиента запрещён» ---------------------------------
# Пишется в кэш, чтобы не повторять заведомо провальный запрос при каждом входе.
_ROPC_BLOCKED_FILE = config.CACHE_DIR / "ropc_unsupported"


def _ropc_known_blocked() -> bool:
    return _ROPC_BLOCKED_FILE.exists()


def _mark_ropc_blocked(reason: str) -> None:
    config.ensure_dirs()
    _ROPC_BLOCKED_FILE.write_text(reason)


async def _refresh(client: httpx.AsyncClient, refresh_token: str) -> Token:
    resp = await client.post(
        config.TOKEN_ENDPOINT,
        data={
            "grant_type": "refresh_token",
            "client_id": config.OIDC_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise AuthError(f"Обновление токена не удалось ({resp.status_code}): {resp.text[:200]}")
    return _token_from_response(resp.json(), "refresh_token")


async def _password_grant(client: httpx.AsyncClient) -> Token:
    """Прямой вход по логину/паролю. Может быть запрещён для клиента TradeMap."""
    resp = await client.post(
        config.TOKEN_ENDPOINT,
        data={
            "grant_type": "password",
            "client_id": config.OIDC_CLIENT_ID,
            "username": config.USERNAME,
            "password": config.PASSWORD,
            "scope": config.OIDC_SCOPE,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code == 200:
        return _token_from_response(resp.json(), "password")

    try:
        err = resp.json().get("error", "")
    except ValueError:
        err = resp.text[:200]

    if err in {"unsupported_grant_type", "invalid_client", "unauthorized_client"}:
        _mark_ropc_blocked(err)
        raise AuthError(f"Прямой вход по паролю запрещён для этого клиента ({err}).")
    if err == "invalid_grant":
        # Грант работает — значит, дело в самих учётных данных.
        raise AuthError(
            "TradeMap отклонил логин или пароль. Проверьте TRADEMAP_USERNAME "
            "и TRADEMAP_PASSWORD в файле .env."
        )
    raise AuthError(f"Вход не удался ({resp.status_code}): {err}")


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


async def _browser_login(client: httpx.AsyncClient) -> Token:
    """Запасной путь: настоящий вход в браузере (authorization code + PKCE)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise AuthError(
            "Для браузерного входа нужен Playwright. Установите его:\n"
            "  .venv/bin/pip install playwright && .venv/bin/playwright install chromium"
        ) from exc

    verifier, challenge = _pkce_pair()
    params = {
        "client_id": config.OIDC_CLIENT_ID,
        "redirect_uri": config.OIDC_REDIRECT_URI,
        "response_type": "code",
        "scope": config.OIDC_SCOPE,
        "state": secrets.token_urlsafe(16),
        "nonce": secrets.token_urlsafe(16),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = str(httpx.URL(config.AUTHORIZE_ENDPOINT, params=params))

    captured: dict[str, str] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        page = await browser.new_page(user_agent=config.USER_AGENT)

        async def intercept(route):
            url = route.request.url
            if "code=" in url:
                captured["url"] = url
                # Гасим переход, чтобы SPA не обменяла код раньше нас:
                # одноразовый код можно использовать только один раз.
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/auth-cb*", intercept)
        await page.goto(authorize_url)

        # Если креды заданы — заполняем форму, иначе пользователь вводит сам.
        if config.has_credentials():
            try:
                await page.fill(
                    "input[type=email], input[name*=sername], #Username",
                    config.USERNAME,
                    timeout=8000,
                )
                await page.fill("input[type=password]", config.PASSWORD, timeout=8000)
                await page.click("button[type=submit], input[type=submit]", timeout=8000)
            except Exception:
                # Вёрстка формы входа может отличаться — просто отдаём управление
                # пользователю, окно браузера уже открыто.
                pass

        deadline = time.time() + 300
        while "url" not in captured and time.time() < deadline:
            await asyncio.sleep(0.25)
        await browser.close()

    if "url" not in captured:
        raise AuthError("Вход в браузере не был завершён за 5 минут.")

    code = httpx.URL(captured["url"]).params.get("code")
    if not code:
        raise AuthError("Браузер вернулся без кода авторизации.")

    resp = await client.post(
        config.TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "client_id": config.OIDC_CLIENT_ID,
            "code": code,
            "redirect_uri": config.OIDC_REDIRECT_URI,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise AuthError(f"Обмен кода на токен не удался ({resp.status_code}): {resp.text[:200]}")
    return _token_from_response(resp.json(), "browser")


class TokenProvider:
    """Отдаёт живой access_token, обновляя его по мере надобности."""

    def __init__(self) -> None:
        self._token: Optional[Token] = _load_token()
        self._lock = asyncio.Lock()

    @property
    def cached(self) -> Optional[Token]:
        return self._token

    def status(self) -> dict:
        token = self._token
        return {
            "authenticated": bool(token and token.is_fresh),
            "expiresIn": token.expires_in if token else 0,
            "obtainedVia": token.obtained_via if token else None,
            "canRefresh": bool(token and token.refresh_token),
            "hasCredentials": config.has_credentials(),
            "browserLoginAvailable": config.ALLOW_BROWSER_LOGIN,
        }

    async def get(self, *, force_login: bool = False, allow_browser: bool = True) -> str:
        async with self._lock:
            if not force_login and self._token and self._token.is_fresh:
                return self._token.access_token

            async with httpx.AsyncClient(
                timeout=60, headers={"User-Agent": config.USER_AGENT}
            ) as client:
                errors: list[str] = []

                if not force_login and self._token and self._token.refresh_token:
                    try:
                        self._token = await _refresh(client, self._token.refresh_token)
                        _save_token(self._token)
                        return self._token.access_token
                    except AuthError as exc:
                        errors.append(str(exc))

                if config.has_credentials() and not _ropc_known_blocked():
                    try:
                        self._token = await _password_grant(client)
                        _save_token(self._token)
                        return self._token.access_token
                    except AuthError as exc:
                        errors.append(str(exc))
                        if "логин или пароль" in str(exc):
                            # Дальше в браузер идти бессмысленно — креды неверные.
                            raise

                if allow_browser:
                    self._token = await _browser_login(client)
                    _save_token(self._token)
                    return self._token.access_token

                if errors:
                    raise AuthError(" | ".join(errors))
                raise AuthError(
                    "Входить нечем: в .env не заданы TRADEMAP_USERNAME и "
                    "TRADEMAP_PASSWORD, сохранённого токена тоже нет."
                )

    def invalidate(self) -> None:
        """Помечает токен протухшим — следующий запрос обновит его."""
        if self._token:
            self._token.expires_at = 0


token_provider = TokenProvider()


# --- Диагностика: python -m app.auth --check --------------------------------

async def _check() -> int:
    print(f"Учётные данные в .env: {'заданы' if config.has_credentials() else 'НЕ заданы'}")
    print(f"Кэш токена: {config.TOKEN_FILE}")

    try:
        access = await token_provider.get()
    except AuthError as exc:
        print(f"\n[!] Токен получить не удалось: {exc}")
        return 1

    token = token_provider.cached
    assert token is not None
    print(f"Токен получен способом: {token.obtained_via}")
    print(f"Живёт ещё: {token.expires_in} с")
    print(f"refresh_token: {'есть' if token.refresh_token else 'нет'}")

    url = f"{config.API_ROOT}/goods/timeSeries/monthly/byPartner"
    params = {
        "country": "036", "partner": "000", "product": "0713",
        "periodFrom": 202501, "periodTo": 202505,
        "indicator": "VAL", "tradeFlow": "I", "directMirror": "D",
        "currency": "USD", "pageSize": 5,
    }
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": config.USER_AGENT}) as client:
        resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {access}"})

    print(f"\nПробный monthly-запрос: HTTP {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        print(f"  записей: {data.get('nbRecords')}  страниц: {data.get('nbPages')}")
        print("  [OK] Доступ к месячным данным есть.")
        return 0

    print(f"  ответ: {resp.text[:300]}")
    if resp.status_code == 401:
        print("  [!] Учётная запись не даёт доступа к месячным данным.")
        print("      Годовые и квартальные ряды при этом работают полностью.")
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Диагностика входа в TradeMap")
    parser.add_argument("--check", action="store_true", help="проверить вход и доступ к monthly")
    parser.add_argument("--reset", action="store_true", help="удалить кэш токена и флаги")
    args = parser.parse_args()

    if args.reset:
        for path in (config.TOKEN_FILE, _ROPC_BLOCKED_FILE):
            path.unlink(missing_ok=True)
        print("Кэш токена очищен.")
        if not args.check:
            return

    raise SystemExit(asyncio.run(_check()))


if __name__ == "__main__":
    main()
