"""Модель запроса, разворот мультивыбора и сборка единого датасета.

Логика режимов повторяет TradeMap: один из трёх срезов (страна / продукт /
партнёр) становится осью вывода — его значения приходят из ответа списком, —
а два оставшихся фиксируются. Мультивыбор применяется к фиксированным осям:
каждая их комбинация — это отдельный запрос к API.

    byPartner — фиксируем страну и продукт, в строках партнёры
    byProduct — фиксируем страну и партнёра, в строках продукты
    byCountry — фиксируем продукт и партнёра, в строках страны-репортёры
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import config
from .client import BlacklistedError, LoginRequiredError, TradeMapClient, TradeMapError
from .reference import WORLD_CODE, Labels

# Ось вывода по режиму: это значение приходит из ответа, а не задаётся запросом.
AXIS_OF_OUTPUT = {"byPartner": "partner", "byProduct": "product", "byCountry": "country"}

# Оси, по которым работает мультивыбор (то, что фиксируется в запросе).
FIXED_AXES = {
    "byPartner": ("country", "product"),
    "byProduct": ("country", "partner"),
    "byCountry": ("product", "partner"),
}

# Предел разворота на один прогон — настраивается через TRADEMAP_MAX_TASKS.
# Держится намеренно скромным: API TradeMap блокирует клиента при избыточной
# нагрузке, а каждая задача — это ещё и несколько страниц пагинации.
MAX_TASKS = config.MAX_TASKS


@dataclass(frozen=True)
class Cell:
    """Одно значение: показатель для конкретной комбинации и периода."""

    reporter: str
    partner: str
    product: str
    period: int
    indicator: str
    value: float
    unit: Optional[str]
    flag: Optional[str]
    is_aggregate: bool


@dataclass
class QuerySpec:
    frequency: str = "monthly"
    output: str = "byPartner"
    reporters: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    partners: list[str] = field(default_factory=lambda: [WORLD_CODE])
    trade_flow: str = "I"
    indicators: list[str] = field(default_factory=lambda: ["VAL", "QTY"])
    period_from: int = 0
    period_to: int = 0
    currency: str = "USD"
    direct_mirror: str = "D"
    hs_level: int = 6

    def validate(self) -> None:
        if self.output not in AXIS_OF_OUTPUT:
            raise TradeMapError(f"Неизвестный режим вывода: {self.output}")
        if not self.indicators:
            raise TradeMapError("Не выбран ни один показатель (Value / Quantity).")
        if self.period_from > self.period_to:
            raise TradeMapError("Начало периода позже его конца.")

        axis = AXIS_OF_OUTPUT[self.output]
        required = {
            "country": (self.reporters, "Выберите хотя бы одну страну."),
            "product": (self.products, "Выберите хотя бы один продукт."),
            "partner": (self.partners, "Выберите хотя бы одного партнёра."),
        }
        for name, (values, message) in required.items():
            # Ось вывода задавать не обязательно — она придёт из ответа.
            if name != axis and not values:
                raise TradeMapError(message)

    def tasks(self) -> list[dict]:
        """Разворачивает мультивыбор в список запросов к API."""
        axis = AXIS_OF_OUTPUT[self.output]

        # На оси вывода в запрос уходит «якорь»: для страны и партнёра это World,
        # для продукта — выбранные коды (это и есть детализация внутри группы).
        countries = self.reporters or [WORLD_CODE]
        partners = self.partners or [WORLD_CODE]
        products = self.products or ["ALL"]

        if axis == "country":
            countries = [WORLD_CODE]
        elif axis == "partner":
            partners = [WORLD_CODE]

        combos = list(itertools.product(countries, partners, products, self.indicators))
        if len(combos) > MAX_TASKS:
            raise TradeMapError(
                f"Слишком много комбинаций: {len(combos)} запросов при пределе {MAX_TASKS}. "
                "Сократите выбор стран или продуктов."
            )
        return [
            {"country": c, "partner": p, "product": pr, "indicator": ind}
            for c, p, pr, ind in combos
        ]

    def post_filter(self) -> tuple[str, set[str]] | None:
        """Фильтр по оси вывода: пользователь выбрал конкретные значения.

        В режимах byPartner/byCountry API всегда возвращает полный список, поэтому
        сужение делаем у себя, уже по полученным данным.
        """
        axis = AXIS_OF_OUTPUT[self.output]
        if axis == "partner" and self.partners and set(self.partners) != {WORLD_CODE}:
            return "partner", set(self.partners)
        if axis == "country" and self.reporters:
            return "reporter", set(self.reporters)
        return None


@dataclass
class Dataset:
    cells: list[Cell]
    periods: list[int]
    sources: list[dict]
    spec: QuerySpec
    labels: Labels
    warnings: list[str]

    # Индексы строятся один раз: выгрузка может содержать десятки тысяч значений,
    # и обход всех ячеек ради каждой строки превратил бы сборку в квадратичную.
    _keys: list[tuple[str, str, str]] = field(init=False, repr=False)
    _units: dict[tuple[str, str, str], str] = field(init=False, repr=False)
    _aggregates: set[tuple[str, str, str]] = field(init=False, repr=False)
    _lookup: dict[tuple[str, str, str, int, str], Cell] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._keys = []
        self._units = {}
        self._aggregates = set()
        self._lookup = {}

        seen: set[tuple[str, str, str]] = set()
        for cell in self.cells:
            key = (cell.reporter, cell.partner, cell.product)
            if key not in seen:
                seen.add(key)
                self._keys.append(key)
            if cell.is_aggregate:
                self._aggregates.add(key)
            if cell.indicator == "QTY" and cell.unit and key not in self._units:
                self._units[key] = cell.unit
            self._lookup[(*key, cell.period, cell.indicator)] = cell

    @property
    def is_empty(self) -> bool:
        return not self.cells

    def keys(self) -> list[tuple[str, str, str]]:
        """Уникальные комбинации (репортёр, партнёр, продукт) в порядке появления."""
        return self._keys

    def unit_for(self, key: tuple[str, str, str]) -> str:
        return self._units.get(key, "")

    def is_aggregate(self, key: tuple[str, str, str]) -> bool:
        return key in self._aggregates

    def lookup(self) -> dict[tuple[str, str, str, int, str], Cell]:
        return self._lookup


def _records_to_cells(
    payload: dict, indicator: str, *, is_aggregate: bool
) -> Iterable[Cell]:
    key = "aggregateRecords" if is_aggregate else "records"
    for record in payload.get(key) or []:
        for point in record.get("data") or []:
            value = point.get("value")
            if value is None:
                continue
            yield Cell(
                reporter=record.get("reporterCd", ""),
                partner=record.get("partnerCd", ""),
                product=record.get("productCd", ""),
                period=int(point["period"]),
                indicator=indicator,
                value=float(value),
                unit=point.get("unit"),
                flag=point.get("flag"),
                is_aggregate=is_aggregate,
            )


async def run(spec: QuerySpec, tokens=None) -> Dataset:
    """Выполняет все запросы разворота и собирает их в один датасет.

    tokens — провайдер токенов вошедшего пользователя; нужен для месячных
    рядов и национальных кодов, остальное API отдаёт анонимно.
    """
    spec.validate()
    tasks = spec.tasks()
    warnings: list[str] = []

    async with TradeMapClient(tokens) as client:

        async def fetch(task: dict) -> tuple[dict, dict] | None:
            try:
                payload = await client.time_series(
                    frequency=spec.frequency,
                    output=spec.output,
                    country=task["country"],
                    partner=task["partner"],
                    product=task["product"],
                    trade_flow=spec.trade_flow,
                    indicator=task["indicator"],
                    period_from=spec.period_from,
                    period_to=spec.period_to,
                    currency=spec.currency,
                    direct_mirror=spec.direct_mirror,
                    hs_level=spec.hs_level,
                )
                return task, payload
            except (BlacklistedError, LoginRequiredError):
                # Обе беды относятся ко всему прогону — прекращаем сразу,
                # не размазывая одну и ту же причину по списку замечаний.
                raise
            except TradeMapError as exc:
                # Одна пустая или отвергнутая комбинация не должна ронять весь прогон,
                # но пользователь обязан о ней узнать.
                warnings.append(
                    f"{task['country']}/{task['product']}/{task['partner']} "
                    f"[{task['indicator']}]: {exc}"
                )
                return None

        results = await asyncio.gather(*(fetch(task) for task in tasks))

    # Ошибка входа воспроизведётся в каждой задаче — не заваливаем ими интерфейс.
    if all(result is None for result in results) and warnings:
        raise TradeMapError(warnings[0])

    cells: list[Cell] = []
    sources: dict[tuple, dict] = {}

    for result in results:
        if result is None:
            continue
        task, payload = result
        indicator = task["indicator"]
        cells.extend(_records_to_cells(payload, indicator, is_aggregate=False))
        cells.extend(_records_to_cells(payload, indicator, is_aggregate=True))
        for source in payload.get("sources") or []:
            sources[(source.get("countryCd"), source.get("labelEn"))] = source

    filt = spec.post_filter()
    if filt:
        axis, allowed = filt
        attr = "partner" if axis == "partner" else "reporter"
        # Итоговые строки (World) сохраняем: они дают контекст выбранным партнёрам.
        cells = [c for c in cells if c.is_aggregate or getattr(c, attr) in allowed]

    periods = sorted({c.period for c in cells})
    ntl_countries = tuple({c.reporter for c in cells if len(c.product) > 6})
    labels = await Labels.build(ntl_countries=ntl_countries)

    if not cells and not warnings:
        warnings.append(
            "TradeMap не вернул данных по этому запросу. "
            "Проверьте, что выбранный период попадает в доступный диапазон страны."
        )

    return Dataset(
        cells=cells,
        periods=periods,
        sources=list(sources.values()),
        spec=spec,
        labels=labels,
        warnings=warnings,
    )


# --- Форматирование периодов ------------------------------------------------

def format_period(period: int, frequency: str) -> str:
    text = str(period)
    if frequency == "yearly":
        return text
    if frequency == "quarterly" and len(text) == 6:
        return f"{text[:4]}-Q{int(text[4:])}"
    if frequency == "monthly" and len(text) == 6:
        return f"{text[:4]}-{text[4:]}"
    return text


def period_list(first: int, last: int, frequency: str) -> list[int]:
    """Все допустимые периоды в диапазоне — для выпадающих списков интерфейса."""
    if not first or not last:
        return []
    if frequency == "yearly":
        return list(range(first, last + 1))

    steps = 4 if frequency == "quarterly" else 12
    result: list[int] = []
    year, unit = divmod(first, 100)
    end_year, end_unit = divmod(last, 100)
    while (year, unit) <= (end_year, end_unit):
        result.append(year * 100 + unit)
        unit += 1
        if unit > steps:
            year, unit = year + 1, 1
    return result
