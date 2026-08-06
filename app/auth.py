"""Получение и обновление OIDC-токена TradeMap.

Токен нужен только для месячных (monthly) данных: годовые и квартальные ряды
API отдаёт анонимно.

Учётные данные вводит сам пользователь на форме входа — в приложении их нет
и в .env они не нужны. Пароль сразу меняется на токены (grant_type=password)
и нигде не сохраняется; на диск ложатся только access- и refresh-токены,
по отдельному файлу на каждого пользователя.

Дальше токен обновляется сам по refresh_token, пока тот действует.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import httpx

from . import config


class AuthError(RuntimeError):
    """Не удалось получить токен. Сообщение пригодно для показа пользователю."""


class InvalidCredentials(AuthError):
    """TradeMap отверг именно логин или пароль (а не сам способ входа).

    Выделено отдельно, потому что на форме входа это единственный случай,
    в котором нужно сказать «неверные данные», а не «сервис недоступен».
    """


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


def _token_file(username: str) -> Path:
    """Отдельный файл на каждого пользователя.

    В имени — хеш логина, а не сам логин: так в кэше не лежит список того,
    кто пользуется инструментом.
    """
    digest = hashlib.sha256(username.strip().lower().encode()).hexdigest()[:16]
    return config.TOKEN_DIR / f"{digest}.json"


def _load_token(username: str) -> Optional[Token]:
    try:
        raw = json.loads(_token_file(username).read_text())
        return Token(**raw)
    except (OSError, ValueError, TypeError):
        return None


def _save_token(username: str, token: Token) -> None:
    config.ensure_dirs()
    path = _token_file(username)
    path.write_text(json.dumps(asdict(token), indent=2))
    os.chmod(path, 0o600)


def _forget_token(username: str) -> None:
    _token_file(username).unlink(missing_ok=True)


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


async def _password_grant(
    client: httpx.AsyncClient, username: str, password: str
) -> Token:
    """Прямой вход по логину/паролю. Может быть запрещён для клиента TradeMap."""
    resp = await client.post(
        config.TOKEN_ENDPOINT,
        data={
            "grant_type": "password",
            "client_id": config.OIDC_CLIENT_ID,
            "username": username,
            "password": password,
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
        raise InvalidCredentials("TradeMap не принял эти логин и пароль.")
    raise AuthError(f"Вход не удался ({resp.status_code}): {err}")


class TokenProvider:
    """Держит живой access_token одного пользователя TradeMap."""

    def __init__(self, username: str) -> None:
        self.username = username
        self._token: Optional[Token] = _load_token(username)
        self._lock = asyncio.Lock()

    @property
    def cached(self) -> Optional[Token]:
        return self._token

    @property
    def has_token(self) -> bool:
        return bool(self._token and (self._token.is_fresh or self._token.refresh_token))

    def status(self) -> dict:
        token = self._token
        return {
            "authenticated": bool(token and token.is_fresh),
            "expiresIn": token.expires_in if token else 0,
            "obtainedVia": token.obtained_via if token else None,
            "canRefresh": bool(token and token.refresh_token),
        }

    async def login(self, password: str) -> None:
        """Меняет пароль на токены. Пароль после этого нигде не сохраняется."""
        async with self._lock:
            async with httpx.AsyncClient(
                timeout=60, headers={"User-Agent": config.USER_AGENT}
            ) as client:
                self._token = await _password_grant(client, self.username, password)
            _save_token(self.username, self._token)

    async def get(self) -> str:
        """Живой токен. Обновляется сам, пока действует refresh_token."""
        async with self._lock:
            if self._token and self._token.is_fresh:
                return self._token.access_token

            if not (self._token and self._token.refresh_token):
                raise AuthError(
                    "Сессия TradeMap истекла. Выйдите и войдите заново."
                )

            async with httpx.AsyncClient(
                timeout=60, headers={"User-Agent": config.USER_AGENT}
            ) as client:
                try:
                    self._token = await _refresh(client, self._token.refresh_token)
                except AuthError as exc:
                    # Refresh-токен отозван или протух — без пароля не восстановить.
                    _forget_token(self.username)
                    self._token = None
                    raise AuthError(
                        f"Сессия TradeMap истекла ({exc}). Выйдите и войдите заново."
                    ) from exc
            _save_token(self.username, self._token)
            return self._token.access_token

    def invalidate(self) -> None:
        """Помечает токен протухшим — следующий запрос обновит его."""
        if self._token:
            self._token.expires_at = 0

    def forget(self) -> None:
        self._token = None
        _forget_token(self.username)


class TokenRegistry:
    """По одному провайдеру на пользователя.

    Каждый входит своей учётной записью TradeMap, поэтому общего токена быть
    не может: и данные, и лимиты у аккаунтов разные.
    """

    def __init__(self) -> None:
        self._providers: dict[str, TokenProvider] = {}

    def for_user(self, username: str) -> TokenProvider:
        key = username.strip().lower()
        provider = self._providers.get(key)
        if provider is None:
            provider = TokenProvider(username.strip())
            self._providers[key] = provider
        return provider

    def forget(self, username: str) -> None:
        key = username.strip().lower()
        provider = self._providers.pop(key, None)
        if provider is not None:
            provider.forget()


token_registry = TokenRegistry()


async def verify_credentials(username: str, password: str) -> TokenProvider:
    """Проверяет пару логин/пароль прямо в TradeMap.

    Это и есть вход в приложение: своей базы пользователей у него нет —
    пускаем тех, кого пускает сам TradeMap.
    """
    provider = token_registry.for_user(username)
    await provider.login(password)
    return provider


# --- Диагностика: python -m app.auth --check --------------------------------

async def _check(username: str, password: str) -> int:
    print(f"Пользователь: {username}")
    print(f"Кэш токенов:  {config.TOKEN_DIR}")

    try:
        provider = await verify_credentials(username, password)
    except AuthError as exc:
        print(f"\n[!] Войти не удалось: {exc}")
        return 1

    token = provider.cached
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
    access = await provider.get()
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
    parser.add_argument("--user", help="логин; по умолчанию берётся TRADEMAP_USERNAME из .env")
    parser.add_argument("--reset", action="store_true", help="удалить кэш токенов и флаги")
    args = parser.parse_args()

    if args.reset:
        for path in config.TOKEN_DIR.glob("*.json"):
            path.unlink(missing_ok=True)
        _ROPC_BLOCKED_FILE.unlink(missing_ok=True)
        print("Кэш токенов очищен.")
        if not args.check:
            return

    username = args.user or config.USERNAME
    if not username:
        username = input("Логин TradeMap: ").strip()
    password = config.PASSWORD if (username == config.USERNAME and config.PASSWORD) else ""
    if not password:
        password = getpass.getpass("Пароль TradeMap: ")

    raise SystemExit(asyncio.run(_check(username, password)))


if __name__ == "__main__":
    main()
