FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Santo_Domingo

WORKDIR /srv

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# ffmpeg (OPCIONAL): convierte las notas de voz del panel (webm/opus del navegador)
# a ogg/opus, el formato que acepta WhatsApp/YCloud. Va en su propia capa y NO es
# fatal a propósito: si el paquete falla, el bot igual despliega y atiende clientes
# (solo la nota de voz responde "no se pudo convertir el audio"). Una feature
# opcional no debe bloquear el deploy de la venta.
RUN apt-get update \
    && (apt-get install -y --no-install-recommends ffmpeg || echo "AVISO: ffmpeg no instalado; las notas de voz del panel quedan deshabilitadas") \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY prompts ./prompts

# /srv/media: copia de las fotos/audios de los clientes para el panel. Tiene que
# existir y ser del usuario `bot` YA EN LA IMAGEN: Docker hereda esos permisos al
# montar el volumen, y si no, el volumen queda de root y el bot no puede escribir.
RUN useradd -m -u 10001 bot && mkdir -p /srv/media && chown -R bot:bot /srv
USER bot

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8000}/health" || exit 1

# UN SOLO WORKER a propósito: el debounce usa tasks de asyncio en memoria.
# Y por ahora UNA SOLA RÉPLICA (ver ADR-010): las caches en memoria de prompts y
# conocimiento no se propagan entre procesos; con >1 réplica los cambios del panel
# solo aplicarían a una. Escalar requiere primero la propagación vía Redis pub/sub.
#
# Puerto: escuchamos en $PORT si el host lo inyecta (Railway/Render/Fly) y en 8000
# si no (local/Docker). `exec` para que uvicorn sea PID 1 y reciba SIGTERM.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
