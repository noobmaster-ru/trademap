'use strict';

const WORLD = '000';

const state = {
  frequency: 'monthly',
  tradeFlow: 'I',
  output: 'byPartner',
  countries: [],
  countryByCode: new Map(),
  products: [],
  productRoot: '',
  productDepths: [],
  selected: { country: [], product: [], partner: [WORLD] },
  periods: [],
  lastPayload: null,
};

const $ = (sel) => document.querySelector(sel);

const OUTPUT_HINTS = {
  byPartner: 'Строки — партнёры. Выберите страну и продукт; список партнёров придёт целиком.',
  byProduct: 'Строки — продукты. Выберите страну и партнёра; глубина задаётся детализацией HS.',
  byCountry: 'Строки — страны-репортёры. Выберите продукт и партнёра.',
};

// Ось вывода приходит из ответа API, поэтому задавать её вручную не требуется.
const AXIS_OF_OUTPUT = { byPartner: 'partner', byProduct: 'product', byCountry: 'country' };

// --- Утилиты ----------------------------------------------------------------

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function formatPeriod(period, frequency) {
  const text = String(period);
  if (frequency === 'yearly') return text;
  if (text.length !== 6) return text;
  if (frequency === 'quarterly') return `${text.slice(0, 4)}-Q${Number(text.slice(4))}`;
  return `${text.slice(0, 4)}-${text.slice(4)}`;
}

/** Русское склонение: 1 запрос, 2 запроса, 5 запросов. */
function plural(n, one, few, many) {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 14) return many;
  const mod10 = n % 10;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

function formatNumber(value) {
  if (value === null || value === undefined) return '';
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 }).format(value);
}

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  if (resp.status === 401 && !path.startsWith('/api/auth/')) {
    location.href = '/login';
    throw new Error('Сессия истекла');
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      detail = body.detail || detail;
    } catch (_) { /* тело не JSON — оставляем статус */ }
    throw new Error(detail);
  }
  return resp.json();
}

function showMessage(kind, text, items) {
  const box = document.createElement('div');
  box.className = `msg msg-${kind}`;
  box.textContent = text;
  if (items && items.length) {
    const list = document.createElement('ul');
    for (const item of items.slice(0, 8)) {
      const li = document.createElement('li');
      li.textContent = item;
      list.appendChild(li);
    }
    if (items.length > 8) {
      const li = document.createElement('li');
      li.textContent = `…и ещё ${items.length - 8}`;
      list.appendChild(li);
    }
    box.appendChild(list);
  }
  $('#messages').appendChild(box);
}

function clearMessages() {
  $('#messages').textContent = '';
}

// --- Переключатели ----------------------------------------------------------

function initSegmented(id, onChange) {
  const group = document.getElementById(id);
  group.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (!button || button.disabled) return;
    for (const other of group.querySelectorAll('button')) other.classList.remove('active');
    button.classList.add('active');
    onChange(button.dataset.value);
  });
}

// --- Списки выбора ----------------------------------------------------------

class Picker {
  constructor(root) {
    this.root = root;
    this.kind = root.dataset.kind;
    this.search = root.querySelector('[data-search]');
    this.optionsBox = root.querySelector('[data-options]');
    this.chipsBox = root.querySelector('[data-chips]');
    this.countBox = root.querySelector('[data-count]');
    this.items = [];

    this.search.addEventListener('input', debounce(() => this.refresh(), 180));
    this.optionsBox.addEventListener('click', (event) => {
      const option = event.target.closest('.option');
      if (!option) return;
      this.toggle(option.dataset.code);
    });
    this.chipsBox.addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (button) this.toggle(button.dataset.code);
    });
  }

  get selectionKey() {
    return this.kind === 'partner' ? 'partner' : this.kind === 'product' ? 'product' : 'country';
  }

  get selection() {
    return state.selected[this.selectionKey];
  }

  toggle(code) {
    const list = this.selection;
    const index = list.indexOf(code);
    if (index >= 0) list.splice(index, 1);
    else list.push(code);
    this.renderChips();
    this.refresh();
    if (this.kind === 'country') {
      refreshPeriodRange();
      // Национальные тарифные линии свои у каждой страны — перезагружаем список.
      loadProducts().catch((error) => showMessage('warn', error.message));
    }
    if (this.kind === 'product') renderProductQuick();
    updateExportState();
  }

  renderChips() {
    this.chipsBox.textContent = '';
    for (const code of this.selection) {
      const chip = document.createElement('span');
      chip.className = 'chip';
      const label = document.createElement('span');
      label.textContent = this.labelFor(code);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.dataset.code = code;
      remove.textContent = '×';
      remove.title = 'Убрать';
      chip.append(label, remove);
      this.chipsBox.appendChild(chip);
    }
    const n = this.selection.length;
    this.countBox.textContent = n ? `${n} выбрано` : 'ничего не выбрано';
  }

  labelFor(code) {
    const found = this.items.find((item) => item.code === code);
    if (found) return `${code} · ${found.label}`;
    if (this.kind !== 'product') {
      const country = state.countryByCode.get(code);
      if (country) return `${code} · ${country.label}`;
    }
    return code;
  }

  setItems(items) {
    this.items = items;
    this.refresh();
    this.renderChips();
  }

  refresh() {
    const needle = this.search.value.trim().toLowerCase();
    let items = this.items;
    if (needle) {
      items = items.filter(
        (item) => item.code.toLowerCase().startsWith(needle) || item.label.toLowerCase().includes(needle),
      );
    }

    this.optionsBox.textContent = '';
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'Ничего не найдено';
      this.optionsBox.appendChild(empty);
      return;
    }

    const fragment = document.createDocumentFragment();
    for (const item of items.slice(0, 400)) {
      const option = document.createElement('div');
      option.className = 'option';
      option.dataset.code = item.code;
      if (this.selection.includes(item.code)) option.classList.add('selected');

      const code = document.createElement('span');
      code.className = 'code';
      code.textContent = item.code;
      const label = document.createElement('span');
      label.className = 'label';
      label.textContent = item.label;
      option.append(code, label);

      // У национальных тарифных линий отмечаем, чьи они.
      if (item.source === 'NTL' && item.countries && item.countries.length) {
        const note = document.createElement('span');
        note.className = 'span';
        note.textContent = `нац. · ${item.countries.join(', ')}`;
        option.appendChild(note);
      }

      // У стран показываем доступный диапазон для выбранной частоты.
      if (item.availability) {
        const span = item.availability[state.frequency];
        const note = document.createElement('span');
        note.className = 'span';
        if (span && span.available) {
          note.textContent = `${formatPeriod(span.first, state.frequency)} – ${formatPeriod(span.last, state.frequency)}`;
        } else {
          note.textContent = 'нет данных';
          option.classList.add('unavailable');
        }
        option.appendChild(note);
      }
      fragment.appendChild(option);
    }
    this.optionsBox.appendChild(fragment);
  }
}

const pickers = {};

// --- Продукты: ветка HS и быстрый выбор по глубине ---------------------------

function depthLabel(extra) {
  const root = state.productRoot;
  if (!root) return extra === 0 ? 'корень' : `+${extra}`;
  if (extra === 0) return `${root} целиком`;
  return `${root.length + extra} знаков`;
}

async function loadProducts() {
  const selected = state.selected.country.join(',');
  const payload = await api(`/api/ref/products?countries=${encodeURIComponent(selected)}`);

  state.productRoot = payload.root || '';
  state.productDepths = payload.depths || [];
  state.products = payload.items;
  pickers.product.setItems(payload.items);
  renderProductQuick();

  const hint = $('#product-hint');
  if (state.productRoot) {
    const national = payload.items.filter((item) => item.source === 'NTL').length;
    hint.textContent =
      `Ветка ${state.productRoot}: ${payload.total} кодов` +
      (national ? `, из них ${national} национальных` : '') +
      (state.selected.country.length
        ? '.'
        : ' — выберите страну, чтобы подтянулись её национальные коды.');
  } else {
    hint.textContent = `${payload.total} кодов HS.`;
  }

  for (const note of payload.notes || []) showMessage('warn', note);
}

function renderProductQuick() {
  const box = $('#product-quick');
  box.textContent = '';
  if (!state.productDepths.length) return;

  for (const extra of state.productDepths) {
    const codes = state.products.filter((item) => item.extra === extra).map((item) => item.code);
    if (!codes.length) continue;

    const selected = state.selected.product;
    const allChosen = codes.every((code) => selected.includes(code));

    const button = document.createElement('button');
    button.type = 'button';
    button.className = allChosen ? 'on' : '';
    button.textContent = `${depthLabel(extra)} (${codes.length})`;
    button.title = allChosen ? 'Снять выбор' : 'Выбрать все коды этой глубины';
    button.addEventListener('click', () => {
      if (allChosen) {
        state.selected.product = selected.filter((code) => !codes.includes(code));
      } else {
        for (const code of codes) if (!selected.includes(code)) selected.push(code);
      }
      pickers.product.renderChips();
      pickers.product.refresh();
      renderProductQuick();
      updateExportState();
    });
    box.appendChild(button);
  }

  // Национальные коды у разных стран разной длины (Индия — 8 знаков,
  // Австралия и США — 10), поэтому даём выбрать их одной кнопкой.
  const national = state.products.filter((item) => item.source === 'NTL').map((item) => item.code);
  if (national.length && state.productDepths.filter((d) => d >= 3).length > 1) {
    const selected = state.selected.product;
    const allChosen = national.every((code) => selected.includes(code));
    const button = document.createElement('button');
    button.type = 'button';
    button.className = allChosen ? 'on' : '';
    button.textContent = `все национальные (${national.length})`;
    button.addEventListener('click', () => {
      if (allChosen) {
        state.selected.product = selected.filter((code) => !national.includes(code));
      } else {
        for (const code of national) if (!selected.includes(code)) selected.push(code);
      }
      pickers.product.renderChips();
      pickers.product.refresh();
      renderProductQuick();
      updateExportState();
    });
    box.appendChild(button);
  }

  if (state.selected.product.length) {
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.textContent = 'Очистить';
    clear.addEventListener('click', () => {
      state.selected.product = [];
      pickers.product.renderChips();
      pickers.product.refresh();
      renderProductQuick();
      updateExportState();
    });
    box.appendChild(clear);
  }
}

// --- Периоды ----------------------------------------------------------------

function availabilitySpan() {
  const codes = state.selected.country.length ? state.selected.country : [WORLD];
  let first = 0;
  let last = 0;
  for (const code of codes) {
    const country = state.countryByCode.get(code);
    const span = country && country.availability[state.frequency];
    if (!span || !span.available) continue;
    first = first ? Math.min(first, span.first) : span.first;
    last = Math.max(last, span.last);
  }
  return { first, last };
}

function buildPeriodList(first, last, frequency) {
  if (!first || !last) return [];
  if (frequency === 'yearly') {
    const out = [];
    for (let year = first; year <= last; year += 1) out.push(year);
    return out;
  }
  const steps = frequency === 'quarterly' ? 4 : 12;
  const out = [];
  let year = Math.floor(first / 100);
  let unit = first % 100;
  const endYear = Math.floor(last / 100);
  const endUnit = last % 100;
  while (year < endYear || (year === endYear && unit <= endUnit)) {
    out.push(year * 100 + unit);
    unit += 1;
    if (unit > steps) { year += 1; unit = 1; }
  }
  return out;
}

function refreshPeriodRange() {
  const { first, last } = availabilitySpan();
  const periods = buildPeriodList(first, last, state.frequency);
  state.periods = periods;

  const fromSelect = $('#periodFrom');
  const toSelect = $('#periodTo');
  const previousFrom = Number(fromSelect.value);
  const previousTo = Number(toSelect.value);

  for (const select of [fromSelect, toSelect]) {
    select.textContent = '';
    for (const period of periods) {
      const option = document.createElement('option');
      option.value = String(period);
      option.textContent = formatPeriod(period, state.frequency);
      select.appendChild(option);
    }
    select.disabled = periods.length === 0;
  }

  if (!periods.length) {
    $('#period-hint').textContent =
      'Для выбранной страны и частоты у TradeMap нет данных — попробуйте другую частоту.';
    updateExportState();
    return;
  }

  // По умолчанию — последние 12 месяцев / 8 кварталов / 10 лет.
  const defaultCount = state.frequency === 'monthly' ? 12 : state.frequency === 'quarterly' ? 8 : 10;
  const defaultFrom = periods[Math.max(0, periods.length - defaultCount)];
  const defaultTo = periods[periods.length - 1];

  fromSelect.value = String(periods.includes(previousFrom) ? previousFrom : defaultFrom);
  toSelect.value = String(periods.includes(previousTo) ? previousTo : defaultTo);

  $('#period-hint').textContent =
    `Доступно у выбранных стран: ${formatPeriod(first, state.frequency)} – ${formatPeriod(last, state.frequency)}.`;
  updateExportState();
}

// --- Состояние формы --------------------------------------------------------

function currentIndicators() {
  const list = [];
  if ($('#ind-val').checked) list.push('VAL');
  if ($('#ind-qty').checked) list.push('QTY');
  return list;
}

function validationError() {
  const axis = AXIS_OF_OUTPUT[state.output];
  if (axis !== 'country' && !state.selected.country.length) return 'Выберите страну-репортёра.';
  if (axis !== 'product' && !state.selected.product.length) return 'Выберите продукт.';
  if (axis !== 'partner' && !state.selected.partner.length) return 'Выберите партнёра.';
  if (!currentIndicators().length) return 'Отметьте хотя бы один показатель.';
  if (!state.periods.length) return 'Нет доступных периодов для этой комбинации.';
  const from = Number($('#periodFrom').value);
  const to = Number($('#periodTo').value);
  if (from > to) return 'Начало периода позже его конца.';
  return null;
}

/** Сколько обращений к API потребует текущий выбор.
 *  Повторяет разворот на сервере: ось вывода схлопывается в одно значение. */
function plannedTasks() {
  const axis = AXIS_OF_OUTPUT[state.output];
  const countries = axis === 'country' ? 1 : Math.max(1, state.selected.country.length);
  const partners = axis === 'partner' ? 1 : Math.max(1, state.selected.partner.length);
  // Продукты не схлопываются даже в режиме byProduct: там каждый выбранный код
  // становится точкой входа для детализации, то есть отдельным запросом.
  const products = Math.max(1, state.selected.product.length);
  return countries * partners * products * Math.max(1, currentIndicators().length);
}

function renderPlan() {
  const tasks = plannedTasks();
  const hint = $('#plan-hint');
  if (tasks <= 2) { hint.textContent = ''; return; }

  // Темп на сервере — 2 параллельных запроса с паузой в секунду.
  const seconds = Math.ceil(tasks / 2);
  const duration = seconds < 90 ? `${seconds} с` : `${Math.ceil(seconds / 60)} мин`;
  const word = plural(tasks, 'запрос', 'запроса', 'запросов');
  let text =
    `Этот выбор — ${tasks} ${word} к TradeMap, ориентировочно ${duration}. ` +
    'Темп снижен намеренно: при частых обращениях API временно блокирует клиента.';

  // Много кодов в режиме «партнёры» — это запрос на каждый код. Если разбивка
  // по партнёрам не нужна, режим «продукты» отдаёт всё это одним обращением.
  const manyProducts = state.selected.product.length >= 8;
  if (manyProducts && state.output === 'byPartner') {
    text += ' Если разбивка по партнёрам не нужна, переключитесь на «Продукты»' +
      ' — там весь уровень приходит одним запросом.';
  }
  hint.textContent = text;
}

function updateExportState() {
  const problem = validationError();
  $('#btn-run').disabled = Boolean(problem);
  $('#btn-export').disabled = Boolean(problem);
  $('#hslevel-field').style.display = state.output === 'byProduct' ? '' : 'none';
  $('#output-hint').textContent = OUTPUT_HINTS[state.output] || '';
  renderPlan();
}

function buildRequest() {
  return {
    frequency: state.frequency,
    output: state.output,
    reporters: state.selected.country,
    products: state.selected.product,
    partners: state.selected.partner,
    tradeFlow: state.tradeFlow,
    indicators: currentIndicators(),
    periodFrom: Number($('#periodFrom').value),
    periodTo: Number($('#periodTo').value),
    currency: $('#currency').value,
    directMirror: $('#directMirror').value,
    hsLevel: Number($('#hsLevel').value),
  };
}

// --- Таблица результата -----------------------------------------------------

function renderResult(payload) {
  const table = $('#result-table');
  table.textContent = '';

  const head = document.createElement('thead');
  const headRow = document.createElement('tr');
  const titles = ['Строка', 'Показатель', 'Ед. изм.'];
  titles.forEach((title, index) => {
    const th = document.createElement('th');
    th.textContent = title;
    // Прилипает только первая колонка — иначе все три встанут в left: 0 внахлёст.
    th.className = index === 0 ? 'label-col sticky-left' : 'label-col';
    headRow.appendChild(th);
  });
  for (const period of payload.periods) {
    const th = document.createElement('th');
    th.textContent = period.label;
    headRow.appendChild(th);
  }
  head.appendChild(headRow);
  table.appendChild(head);

  const body = document.createElement('tbody');
  for (const row of payload.rows) {
    const tr = document.createElement('tr');
    if (row.isAggregate) tr.className = 'aggregate';

    const nameCell = document.createElement('td');
    nameCell.className = 'sticky-left';
    // В первой колонке показываем то, что перечисляется в строках.
    const primary =
      state.output === 'byProduct'
        ? `${row.product} · ${row.productName}`
        : state.output === 'byCountry'
        ? `${row.reporter} · ${row.reporterName}`
        : `${row.partner} · ${row.partnerName}`;
    nameCell.textContent = primary;
    const sub = document.createElement('span');
    sub.className = 'sub';
    sub.textContent =
      state.output === 'byProduct'
        ? `${row.reporterName} ← ${row.partnerName}`
        : state.output === 'byCountry'
        ? `${row.productName}`
        : `${row.reporterName} · ${row.product}`;
    nameCell.appendChild(sub);
    tr.appendChild(nameCell);

    const indicatorCell = document.createElement('td');
    indicatorCell.textContent = row.indicator === 'VAL' ? 'Value' : 'Quantity';
    tr.appendChild(indicatorCell);

    const unitCell = document.createElement('td');
    unitCell.textContent = row.unit || '';
    tr.appendChild(unitCell);

    for (const value of row.values) {
      const td = document.createElement('td');
      if (value === null || value === undefined) {
        td.className = 'empty-cell';
        td.textContent = '—';
      } else {
        td.className = 'num';
        td.textContent = formatNumber(value);
      }
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  table.appendChild(body);

  $('#result-panel').hidden = false;
  $('#result-meta').textContent =
    `${payload.totalRows} комбинаций, ${payload.cellCount} значений` +
    (payload.truncated ? ' — в превью показаны первые 200, в Excel попадут все' : '');
}

// --- Действия ---------------------------------------------------------------

async function runQuery() {
  clearMessages();
  const problem = validationError();
  if (problem) { showMessage('warn', problem); return; }

  const button = $('#btn-run');
  const original = button.textContent;
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span>Загружаю…';

  try {
    const payload = await api('/api/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildRequest()),
    });
    state.lastPayload = payload;
    renderResult(payload);
    if (payload.warnings && payload.warnings.length) {
      showMessage('warn', 'Часть запросов вернулась с замечаниями:', payload.warnings);
    }
    if (!payload.rows.length) {
      showMessage('info', 'TradeMap не вернул строк по этой комбинации.');
    }
  } catch (error) {
    showMessage('error', error.message);
    $('#result-panel').hidden = true;
  } finally {
    button.textContent = original;
    updateExportState();
  }
}

async function exportExcel() {
  clearMessages();
  const problem = validationError();
  if (problem) { showMessage('warn', problem); return; }

  const button = $('#btn-export');
  const original = button.textContent;
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span>Собираю файл…';

  try {
    const resp = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildRequest()),
    });
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try { detail = (await resp.json()).detail || detail; } catch (_) { /* не JSON */ }
      throw new Error(detail);
    }

    const disposition = resp.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = match ? match[1] : 'trademap.xlsx';
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showMessage('info', `Файл ${link.download} сохранён.`);
  } catch (error) {
    showMessage('error', error.message);
  } finally {
    button.textContent = original;
    updateExportState();
  }
}

// --- Вход -------------------------------------------------------------------

function renderAuth(status) {
  const badge = $('#auth-state');

  // Вход в приложение и есть вход в TradeMap, поэтому состояние одно.
  if (status.appUser) {
    $('#app-user').textContent = status.appUser;
    $('#logout-form').hidden = false;
  }

  if (status.authenticated) {
    badge.className = 'badge badge-ok';
    badge.textContent = 'TradeMap подключён';
  } else if (status.canRefresh) {
    // Токен доступа протух, но refresh ещё есть — обновится при первом запросе.
    badge.className = 'badge badge-ok';
    badge.textContent = 'TradeMap подключён';
  } else {
    badge.className = 'badge badge-warn';
    badge.textContent = 'сессия TradeMap истекла — войдите заново';
  }
}

async function refreshAuth() {
  try {
    renderAuth(await api('/api/auth/status'));
  } catch (_) {
    $('#auth-state').textContent = 'статус входа неизвестен';
  }
}

// --- Запуск -----------------------------------------------------------------

async function init() {
  for (const root of document.querySelectorAll('.picker')) {
    const picker = new Picker(root);
    pickers[picker.kind] = picker;
  }

  initSegmented('frequency', (value) => {
    state.frequency = value;
    pickers.country.refresh();
    pickers.partner.refresh();
    $('#frequency-hint').textContent =
      value === 'monthly' ? 'Месячные ряды доступны по вашей учётной записи.' : '';
    refreshPeriodRange();
  });
  initSegmented('tradeFlow', (value) => { state.tradeFlow = value; });
  initSegmented('output', (value) => { state.output = value; updateExportState(); });

  for (const id of ['#ind-val', '#ind-qty']) {
    $(id).addEventListener('change', updateExportState);
  }
  for (const id of ['#periodFrom', '#periodTo']) {
    $(id).addEventListener('change', updateExportState);
  }
  $('#btn-run').addEventListener('click', runQuery);
  $('#btn-export').addEventListener('click', exportExcel);
  $('#currency').value = 'USD';
  $('#frequency-hint').textContent = 'Месячные ряды доступны по вашей учётной записи.';

  refreshAuth();

  try {
    const countries = await api('/api/ref/countries');
    state.countries = countries;
    state.countryByCode = new Map(countries.map((c) => [c.code, c]));
    pickers.country.setItems(countries);
    pickers.partner.setItems(countries);

    await loadProducts();

    // Партнёр по умолчанию — World: он даёт итоговую строку и сведения об источниках.
    pickers.partner.renderChips();
    refreshPeriodRange();
    updateExportState();
  } catch (error) {
    showMessage('error', `Не удалось загрузить справочники TradeMap: ${error.message}`);
  }
}

init();
