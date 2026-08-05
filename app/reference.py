"""Справочники стран и продуктов с кэшем на диске.

Все три источника (/countries, /products/HS, /products/NTL) API отдаёт анонимно,
поэтому справочники доступны даже без входа.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from . import config
from .client import TradeMapClient

# Соответствие «частота -> ключ доступности в записи страны».
AVAILABILITY_KEY = {
    "yearly": "yearly246",
    "quarterly": "quarterly",
    "monthly": "monthly",
}

WORLD_CODE = "000"


def _cache_path(name: str):
    config.ensure_dirs()
    return config.REFERENCE_CACHE_DIR / f"{name}.json"


def _read_cache(name: str) -> Optional[Any]:
    path = _cache_path(name)
    try:
        if time.time() - path.stat().st_mtime > config.REFERENCE_TTL_SEC:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_cache(name: str, payload: Any) -> None:
    _cache_path(name).write_text(json.dumps(payload))


async def _cached(name: str, fetch) -> Any:
    cached = _read_cache(name)
    if cached is not None:
        return cached
    async with TradeMapClient() as client:
        payload = await fetch(client)
    _write_cache(name, payload)
    return payload


async def countries() -> list[dict]:
    """Страны с диапазонами доступности данных по каждой частоте."""
    raw = await _cached("countries", lambda c: c.countries())
    result = []
    for item in raw:
        availability = {}
        for freq, key in AVAILABILITY_KEY.items():
            span = item.get(key) or {}
            first, last = span.get("firstPeriod") or 0, span.get("lastPeriod") or 0
            availability[freq] = {
                "first": first,
                "last": last,
                "available": bool(first and last),
            }
        result.append(
            {
                "code": item["countryCd"],
                "label": item["label"],
                "isGroup": item["countryCd"] == WORLD_CODE,
                "availability": availability,
            }
        )
    result.sort(key=lambda c: (c["code"] != WORLD_CODE, c["label"]))
    return result


async def products_hs() -> list[dict]:
    """Дерево HS: коды 2, 4 и 6 знаков."""
    raw = await _cached("products_hs", lambda c: c.products_hs())
    return [
        {
            "code": item["productCd"],
            "label": item["label"],
            "level": len(item["productCd"]),
        }
        for item in raw
    ]


async def products_ntl(country: str, product: str = "") -> list[dict]:
    """Национальные тарифные линии страны внутри ветки HS.

    Ветку нужно задавать: без неё API возвращает лишь несколько случайных кодов.
    """
    key = f"products_ntl_{country}_{product or 'all'}"
    raw = await _cached(key, lambda c: c.products_ntl(country=country, product=product))
    # Один и тот же код может прийти несколько раз — схлопываем.
    seen: dict[str, dict] = {}
    for item in raw:
        code = item["productCd"]
        seen.setdefault(code, {"code": code, "label": item["label"], "level": len(code)})
    return sorted(seen.values(), key=lambda p: p["code"])


async def products_subtree(
    root: str, countries: tuple[str, ...] = ()
) -> tuple[list[dict], list[str]]:
    """Все коды внутри одной ветки HS — сам корень, его подкоды и национальные линии.

    Например, для root="0713": 0713 (4 знака), 071310/071320/… (6 знаков, общие для
    всех стран) и национальные тарифные линии выбранных стран (7 знаков и длиннее).
    Возвращает список кодов и список замечаний — справочник NTL может быть недоступен,
    и это не повод ронять весь запрос.
    """
    root = (root or "").strip()
    notes: list[str] = []

    items = [p for p in await products_hs() if not root or p["code"].startswith(root)]
    by_code = {
        p["code"]: {**p, "extra": len(p["code"]) - len(root), "source": "HS", "countries": []}
        for p in items
    }

    for country in countries:
        try:
            national = await products_ntl(country, root)
        except Exception as exc:
            notes.append(f"Национальные коды страны {country} недоступны: {exc}")
            continue
        for item in national:
            code = item["code"]
            if root and not code.startswith(root):
                continue
            entry = by_code.get(code)
            if entry is None:
                entry = {
                    **item,
                    "extra": len(code) - len(root),
                    "source": "NTL",
                    "countries": [],
                }
                by_code[code] = entry
            if country not in entry["countries"]:
                entry["countries"].append(country)

    result = sorted(by_code.values(), key=lambda p: (len(p["code"]), p["code"]))
    return result, notes


async def coverage_latest() -> dict:
    """Последние доступные периоды по каждой частоте."""
    raw = await _cached("coverage", lambda c: c.coverage_latest())
    by_type = {item["dataType"]: item for item in raw}
    return {
        "yearly": (by_type.get("Y") or {}).get("latestPeriod"),
        "quarterly": (by_type.get("Q") or {}).get("latestPeriod"),
        "monthly": (by_type.get("M") or {}).get("latestPeriod"),
    }


class Labels:
    """Подстановка человекочитаемых названий вместо кодов при выгрузке."""

    def __init__(self, country_map: dict[str, str], product_map: dict[str, str]) -> None:
        self._countries = country_map
        self._products = product_map

    @classmethod
    async def build(cls, *, ntl_countries: tuple[str, ...] = ()) -> "Labels":
        country_map = {c["code"]: c["label"] for c in await countries()}
        product_map = {p["code"]: p["label"] for p in await products_hs()}
        product_map["ALL"] = "All products"
        # Национальные коды подтягиваем только для реально задействованных стран.
        for code in ntl_countries:
            try:
                for item in await products_ntl(code):
                    product_map.setdefault(item["code"], item["label"])
            except Exception:
                # Отсутствие национальных кодов не должно ломать выгрузку.
                pass
        return cls(country_map, product_map)

    def country(self, code: str) -> str:
        return self._countries.get(code, code)

    def product(self, code: str) -> str:
        return self._products.get(code, code)
