FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Santo_Domingo

WORKDIR /srv

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY prompts ./prompts

RUN useradd -m -u 10001 bot && chown -R bot:bot /srv
USER bot

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# UN SOLO WORKER a propósito: el debounce usa tasks de asyncio en memoria.
# Y por ahora UNA SOLA RÉPLICA (ver ADR-010): las caches en memoria de prompts y
# conocimiento no se propagan entre procesos; con >1 réplica los cambios del panel
# solo aplicarían a una. Escalar requiere primero la propagación vía Redis pub/sub.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
