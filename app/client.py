"""Клиент неофициального JSON API TradeMap (https://www.trademap.org/api).

Все имена параметров и допустимые значения собраны здесь — если ITC поменяет API,
править нужно будет только этот модуль.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Optional

import httpx

from . import config
from .auth import AuthError, token_provider

FREQUENCIES = ("yearly", "quarterly", "monthly")
OUTPUTS = ("byPartner", "byProduct", "byCountry")
INDICATORS = ("VAL", "QTY")
TRADE_FLOWS = ("I", "E")

# Месячные данные закрыты для анонимов, остальное — нет.
AUTH_REQUIRED_FREQUENCIES = {"monthly"}

# Допустимые значения hsLevel по версии самого API (проверено его валидацией).
# 10 — уровень национальных тарифных линий (NTL), длина кода при этом зависит
# от страны: у Австралии это 8 и 10 знаков, у Индии — 8.
HS_LEVELS = (2, 4, 6, 10)
NTL_HS_LEVEL = 10

# Данные на уровне национальных кодов требуют входа при ЛЮБОЙ частоте,
# включая годовую: "Access to yearly data at the NTL level requires login."
NTL_CODE_MIN_LENGTH = 7


class TradeMapError(RuntimeError):
    """Ошибка обращения к API. Текст пригоден для показа пользователю."""


class BlacklistedError(TradeMapError):
    """API временно отказал в обслуживании из-за слишком частых обращений.

    Повторять запросы в этом состоянии бессмысленно и вредно: прогон нужно
    прекратить целиком и переждать.
    """


class LoginRequiredError(TradeMapError):
    """Нужен вход в TradeMap (месячные данные).

    Как и блокировка, относится ко всему прогону сразу: без токена одинаково
    провалятся все задачи, поэтому нет смысла собирать это в список замечаний
    по каждой комбинации.
    """


class TradeMapClient:
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY)
        self._spacing_lock = asyncio.Lock()

    async def __aenter__(self) -> "TradeMapClient":
        self._client = httpx.AsyncClient(
            base_url=config.API_ROOT,
            timeout=httpx.Timeout(90.0, connect=20.0),
            headers={
                "User-Agent": config.USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en",
                # Те же заголовки, что шлёт сам интерфейс TradeMap: запрос
                # приходит из его же приложения, просто запущенного локально.
                "Referer": "https://www.trademap.org/",
                "Origin": "https://www.trademap.org",
            },
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("TradeMapClient используется вне контекстного менеджера")
        return self._client

    async def _pace(self) -> None:
        """Разносит запросы во времени, чтобы не выглядеть агрессивным скрейпером."""
        async with self._spacing_lock:
            await asyncio.sleep(config.REQUEST_SPACING_SEC)

    async def _get(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        *,
        needs_auth: bool = False,
    ) -> Any:
        headers: dict[str, str] = {}
        if needs_auth:
            # allow_browser=False: окно браузера должно открываться только по
            # явному нажатию «Войти», а не посреди обычной выгрузки данных.
            try:
                headers["Authorization"] = f"Bearer {await token_provider.get(allow_browser=False)}"
            except AuthError as exc:
                raise LoginRequiredError(
                    f"Месячные данные требуют входа в TradeMap.\n{exc}\n"
                    "Нажмите «Войти» вверху страницы или заполните .env."
                ) from exc

        last_error = ""
        async with self._semaphore:
            for attempt in range(config.MAX_RETRIES):
                await self._pace()
                try:
                    resp = await self._http.get(path, params=params, headers=headers)
                except httpx.RequestError as exc:
                    last_error = f"сеть: {exc}"
                    await asyncio.sleep(2**attempt + random.random())
                    continue

                if resp.status_code == 200:
                    return resp.json()

                if resp.status_code == 401:
                    if not needs_auth:
                        raise TradeMapError(_explain_401(resp))
                    # Токен мог протухнуть на лету — обновляем ровно один раз.
                    if attempt == 0:
                        token_provider.invalidate()
                        try:
                            token = await token_provider.get(allow_browser=False)
                        except AuthError as exc:
                            raise TradeMapError(str(exc)) from exc
                        headers["Authorization"] = f"Bearer {token}"
                        continue
                    raise TradeMapError(_explain_401(resp))

                if resp.status_code == 400:
                    # Валидация сервера — повторять бессмысленно, текст точный.
                    raise TradeMapError(f"TradeMap отклонил запрос: {_body_text(resp)}")

                if resp.status_code == 403:
                    raise BlacklistedError(
                        "TradeMap временно перестал отвечать на запросы к данным "
                        f"(HTTP 403: {_body_text(resp)}).\n"
                        "Это защита от избыточной нагрузки: она срабатывает при частых "
                        "обращениях и держится долго — в наших замерах больше получаса. "
                        "Снять её со своей стороны нельзя, только переждать.\n"
                        "Справочники кэшированы, поэтому интерфейс продолжит работать. "
                        "Когда доступ вернётся, дробите большие выборки на части."
                    )

                if resp.status_code in (429, 500, 502, 503, 504):
                    last_error = f"HTTP {resp.status_code}"
                    await asyncio.sleep(2**attempt + random.random())
                    continue

                raise TradeMapError(f"HTTP {resp.status_code}: {_body_text(resp)}")

        raise TradeMapError(f"Запрос не удался после {config.MAX_RETRIES} попыток ({last_error})")

    # --- Справочники --------------------------------------------------------

    async def countries(self) -> list[dict]:
        return await self._get("/countries")

    async def products_hs(self) -> list[dict]:
        return await self._get("/products/HS")

    async def products_ntl(self, *, country: str, product: str = "") -> list[dict]:
        """Национальные тарифные линии страны.

        Без параметра product эндпоинт отдаёт лишь горстку кодов (у Австралии —
        десяток произвольных), поэтому ветку HS нужно указывать явно: тогда
        возвращаются все национальные коды внутри неё.
        """
        params: dict[str, Any] = {"country": country}
        if product:
            params["product"] = product
        return await self._get("/products/NTL", params)

    async def coverage_latest(self) -> list[dict]:
        return await self._get("/coverage/goods/latest")

    # --- Данные -------------------------------------------------------------

    async def time_series(
        self,
        *,
        frequency: str,
        output: str,
        country: str,
        partner: str,
        product: str,
        trade_flow: str,
        indicator: str,
        period_from: int,
        period_to: int,
        currency: str = "USD",
        direct_mirror: str = "D",
        hs_level: Optional[int] = None,
    ) -> dict:
        """Возвращает все страницы одного среза, склеенные в один ответ.

        Пагинация обязательна: pageSize больше 500 сервер молча игнорирует и
        отдаёт 25 записей на страницу, поэтому опираемся на фактический
        nbRecordPerPage из ответа, а не на то, что запросили.
        """
        if frequency not in FREQUENCIES:
            raise TradeMapError(f"Неизвестная частота: {frequency}")
        if output not in OUTPUTS:
            raise TradeMapError(f"Неизвестный режим вывода: {output}")
        if indicator not in INDICATORS:
            raise TradeMapError(f"Неизвестный показатель: {indicator}")
        if trade_flow not in TRADE_FLOWS:
            raise TradeMapError(f"Неизвестное направление: {trade_flow}")

        params: dict[str, Any] = {
            "country": country,
            "partner": partner,
            "product": product,
            "tradeFlow": trade_flow,
            "indicator": indicator,
            "directMirror": direct_mirror,
            "periodFrom": period_from,
            "periodTo": period_to,
            "page": 1,
            "pageSize": config.MAX_PAGE_SIZE,
            "sortBy": "",
            "sortDir": "desc",
        }
        # currency сервер требует только для стоимостного показателя.
        if indicator == "VAL":
            params["currency"] = currency
        if output == "byProduct":
            params["hsLevel"] = hs_level or 6

        path = f"/goods/timeSeries/{frequency}/{output}"
        # Вход нужен и для месячных рядов, и для любого обращения к национальным
        # тарифным линиям — как к конкретному длинному коду, так и к запросу
        # их списком через hsLevel=10.
        at_ntl_level = (
            len(product) >= NTL_CODE_MIN_LENGTH and product.isdigit()
        ) or (output == "byProduct" and (hs_level or 0) == NTL_HS_LEVEL)
        needs_auth = frequency in AUTH_REQUIRED_FREQUENCIES or at_ntl_level

        first = await self._get(path, params, needs_auth=needs_auth)
        records = list(first.get("records") or [])

        per_page = int(first.get("nbRecordPerPage") or 0)
        total = int(first.get("nbRecords") or 0)
        # Сколько страниц на самом деле, исходя из принятого сервером размера.
        pages = first.get("nbPages") or 1
        if per_page > 0:
            pages = max(1, -(-total // per_page))

        for page in range(2, int(pages) + 1):
            chunk = await self._get(path, {**params, "page": page}, needs_auth=needs_auth)
            records.extend(chunk.get("records") or [])

        return {
            "records": records,
            "aggregateRecords": first.get("aggregateRecords") or [],
            "sources": first.get("sources") or [],
            "nbRecords": total,
            "fetched": len(records),
        }


def _body_text(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        if "errors" in payload:
            parts = [f"{k}: {'; '.join(v)}" for k, v in payload["errors"].items()]
            return " | ".join(parts)
        return str(payload.get("title") or payload)[:300]
    return str(payload)[:300]


def _explain_401(resp: httpx.Response) -> str:
    detail = _body_text(resp)
    return (
        f"TradeMap требует вход: {detail}\n"
        "Впишите TRADEMAP_USERNAME и TRADEMAP_PASSWORD в файл .env, "
        "либо нажмите «Войти» в интерфейсе. "
        "Годовые и квартальные ряды доступны и без входа."
    )
