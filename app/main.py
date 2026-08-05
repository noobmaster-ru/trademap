"""FastAPI-приложение: REST для интерфейса + отдача статики."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, excel, query, reference
from .auth import AuthError, token_provider
from .client import TradeMapError

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="TradeMap Explorer", docs_url=None, redoc_url=None)


# --- Модели запроса ---------------------------------------------------------

class QueryRequest(BaseModel):
    frequency: str = "monthly"
    output: str = "byPartner"
    reporters: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    partners: list[str] = Field(default_factory=lambda: [reference.WORLD_CODE])
    tradeFlow: str = "I"
    indicators: list[str] = Field(default_factory=lambda: ["VAL", "QTY"])
    periodFrom: int = 0
    periodTo: int = 0
    currency: str = config.DEFAULT_CURRENCY
    directMirror: str = "D"
    hsLevel: int = 6

    def to_spec(self) -> query.QuerySpec:
        return query.QuerySpec(
            frequency=self.frequency,
            output=self.output,
            reporters=self.reporters,
            products=self.products,
            partners=self.partners,
            trade_flow=self.tradeFlow,
            indicators=self.indicators,
            period_from=self.periodFrom,
            period_to=self.periodTo,
            currency=self.currency,
            direct_mirror=self.directMirror,
            hs_level=self.hsLevel,
        )


# --- Справочники ------------------------------------------------------------

@app.get("/api/ref/countries")
async def ref_countries():
    return await reference.countries()


@app.get("/api/ref/products")
async def ref_products(
    q: str = "",
    countries: str = "",
    root: Optional[str] = None,
    limit: int = Query(default=10000, le=20000),
):
    """Список продуктов.

    По умолчанию ограничен веткой TRADEMAP_PRODUCT_ROOT (0713 — сушёные бобовые):
    сам корень, его 6-значные подкоды и национальные тарифные линии выбранных стран.
    Передайте root= пустым, чтобы получить весь классификатор HS.
    """
    effective_root = config.PRODUCT_ROOT if root is None else root.strip()
    selected = tuple(c for c in (countries or "").split(",") if c.strip())

    items, notes = await reference.products_subtree(effective_root, selected)

    # «0713 и далее 2–4 символа»: длиннее заданного предела не показываем.
    if effective_root and config.PRODUCT_EXTRA_MAX > 0:
        items = [item for item in items if item["extra"] <= config.PRODUCT_EXTRA_MAX]

    needle = q.strip().lower()
    if needle:
        items = [
            item for item in items
            if needle in item["label"].lower() or item["code"].startswith(needle)
        ]

    depths = sorted({item["extra"] for item in items})
    return {
        "root": effective_root,
        "extraMax": config.PRODUCT_EXTRA_MAX,
        "depths": depths,
        "total": len(items),
        "items": items[:limit],
        "notes": notes,
    }


@app.get("/api/ref/coverage")
async def ref_coverage():
    return await reference.coverage_latest()


@app.get("/api/ref/periods")
async def ref_periods(frequency: str, first: int, last: int):
    periods = query.period_list(first, last, frequency)
    return [
        {"value": period, "label": query.format_period(period, frequency)}
        for period in periods
    ]


# --- Авторизация ------------------------------------------------------------

@app.get("/api/auth/status")
async def auth_status():
    return token_provider.status()


@app.get("/health")
async def health():
    """Лёгкая проверка для Docker healthcheck — без обращений к TradeMap."""
    return {"status": "ok"}


@app.post("/api/auth/login")
async def auth_login(useBrowser: bool = False):
    # На сервере окна браузера нет: запрос бы просто завис на пять минут.
    allow_browser = useBrowser and config.ALLOW_BROWSER_LOGIN
    try:
        await token_provider.get(force_login=True, allow_browser=allow_browser)
    except AuthError as exc:
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), **token_provider.status()},
        )
    return token_provider.status()


# --- Данные -----------------------------------------------------------------

def _preview(data: query.Dataset, limit: int = 200) -> dict:
    spec = data.spec
    lookup = data.lookup()
    rows = []
    for key in data.keys()[:limit]:
        reporter, partner, product = key
        for indicator in spec.indicators:
            values = [
                lookup.get((reporter, partner, product, period, indicator))
                for period in data.periods
            ]
            if not any(values):
                continue
            rows.append(
                {
                    "reporter": reporter,
                    "reporterName": data.labels.country(reporter),
                    "partner": partner,
                    "partnerName": data.labels.country(partner),
                    "product": product,
                    "productName": data.labels.product(product),
                    "indicator": indicator,
                    "unit": data.unit_for(key) if indicator == "QTY" else f"тыс. {spec.currency}",
                    "isAggregate": data.is_aggregate(key),
                    "values": [None if c is None else c.value for c in values],
                }
            )
    return {
        "periods": [
            {"value": p, "label": query.format_period(p, spec.frequency)}
            for p in data.periods
        ],
        "rows": rows,
        "totalRows": len(data.keys()),
        "truncated": len(data.keys()) > limit,
        "cellCount": len(data.cells),
        "sources": data.sources,
        "warnings": data.warnings,
    }


@app.post("/api/query")
async def run_query(request: QueryRequest):
    try:
        data = await query.run(request.to_spec())
    except (TradeMapError, AuthError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _preview(data)


@app.post("/api/export")
async def export(request: QueryRequest):
    try:
        data = await query.run(request.to_spec())
    except (TradeMapError, AuthError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if data.is_empty:
        raise HTTPException(
            status_code=400,
            detail=data.warnings[0] if data.warnings else "Нет данных для выгрузки.",
        )

    payload = excel.build_workbook(data)
    filename = excel.suggest_filename(data)
    quoted = urllib.parse.quote(filename)
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quoted}"},
    )


# --- Статика ----------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
