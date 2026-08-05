# TradeMap Explorer.
#
# Playwright и Chromium в образ НЕ кладутся: вход выполняется напрямую по
# логину и паролю (grant_type=password), а окно браузера на headless-сервере
# всё равно не открыть. Это экономит около 400 МБ.

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Кэш токена и справочников — в томе, чтобы переживал пересоздание контейнера.
    TRADEMAP_CACHE_DIR=/data \
    # На сервере нет графической среды: запасной вход через браузер отключён.
    TRADEMAP_ALLOW_BROWSER_LOGIN=0

WORKDIR /srv

# Зависимости ставим отдельным слоем — он переиспользуется при правках кода.
# Playwright исключаем: он в requirements.txt помечен как опциональный.
COPY requirements.txt ./
RUN grep -vE '^\s*(#|$|playwright)' requirements.txt > /tmp/req.txt \
    && pip install --no-cache-dir -r /tmp/req.txt \
    && rm /tmp/req.txt

COPY app ./app

# Работаем не от root: у процесса нет причин иметь права на весь контейнер.
RUN useradd --uid 10001 --no-create-home --home-dir /srv \
        --shell /usr/sbin/nologin trademap \
    && mkdir -p /data \
    && chown -R trademap:trademap /srv /data
USER trademap

EXPOSE 8765

# Лёгкая проверка живости — без обращений к TradeMap.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8765", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
