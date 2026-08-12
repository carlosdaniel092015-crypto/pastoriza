# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Bot de ventas de WhatsApp ("Michelle") para Pastoriza Plastics. FastAPI + openai-agents +
Redis + Odoo (XML-RPC) + YCloud. La arquitectura completa (capas, ADR, diagrama) está en
**`ARCHITECTURE.md`** — léelo antes de un cambio estructural. El `README.md` explica el porqué
vs el n8n original.

## Comandos

Entorno **Windows**; hay `Makefile` pero en Windows conviene invocar el venv directo y forzar UTF-8:

```bash
# venv (Python 3.12)
./.venv/Scripts/python.exe -m pytest -q                          # toda la suite (149 tests)
./.venv/Scripts/python.exe -m pytest tests/test_enrutador.py -q  # un archivo
./.venv/Scripts/python.exe -m pytest tests/test_seguridad.py::TestNoInventarFotos -q  # una clase/test

# servidor (SIEMPRE con UTF-8 en Windows, ver Gotchas)
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# conversación por terminal SIN mandar WhatsApp (pega contra Odoo/OpenAI reales)
PYTHONUTF8=1 ./.venv/Scripts/python.exe -m scripts.probar_conversacion --limpiar --chat-id 18090000000

# índice de fotos para buscar_por_foto (1 llamada de visión por producto)
PYTHONUTF8=1 ./.venv/Scripts/python.exe -m scripts.indexar_fichas
```

Los tests corren **sin `.env`, sin Redis, sin Odoo** (`tests/conftest.py` fija claves dummy y
solo cubren lógica pura: enrutado, matching, cotización, saneo, entrega, repetición, prompts).

## Arquitectura en 30 segundos

Flujo de un turno (todo en `app/pipeline.py:procesar_turno`):
```
webhook (main.py) → debounce → combinar media (media.py) → anti-repetición (repeticion.py)
→ fast-path FAQ (router.py, 0 tokens)  ó  enrutador (agents/enrutador.py) → UN especialista
→ saneo de salida (_sanear) → envío (ycloud.py) → efectos (_efectos: Odoo, aviso, revisión, panel)
```

**Multi-agente** (`app/agents/`): `enrutador.py` decide determinista-first (regex/estado, 0
tokens; `gpt-4o-mini` solo ante duda) y corre **un solo** especialista por turno —
`ventas.py`/`pedido.py`/`soporte.py`, cada uno su módulo, su prompt y sus tools. `especialistas.py`
es el registro. Modelos: mini salvo `pedido` (gpt-4o). Añadir un agente = nuevo módulo + entrada
en `ESPECIALISTAS` + `prompts/<nombre>.md` + `AGENTES` en `prompt_store.py`.

**Invariante central:** los efectos (`order_id`, `partner_id`, productos mostrados) los escriben
las **tools** en `ConversationContext`, nunca el modelo. Por eso el bot no puede confirmar un
pedido inexistente ni mostrar una foto que una tool no devolvió este turno. No rompas esto.

**Prompts** = `prompts/base_comun.md` (identidad + seguridad + reglas duras, compartido por todos)
+ `prompts/<agente>.md` + conocimiento inyectado + config dinámica. Editables/subibles desde el
panel (override en Redis). Ver `app/agents/base.py:armar_instrucciones`.

**Datos:** Odoo = durable (clientes, pedidos, catálogo); Redis (prefijo `pastoriza:`) = efímero
(sesión 24h, pausas, debounce, eventos, conocimiento); código/`.env` = secretos y reglas duras.
Config de **negocio** editable en Redis (`app/business_config.py`) ≠ config de **entorno**
(`app/settings.py`).

**Panel de operación** (`app/panel/`, servido en `/panel`, protegido con `PANEL_TOKEN`): CRM en
vivo, alertas, config, prompts por-agente, aprendizaje, kill-switch global. La UI es una SPA
vanilla en `ui.py` (`PANEL_HTML`), **responsive** (nav lateral en escritorio → barra inferior +
hilo a pantalla completa en móvil) e **instalable como PWA**: `MANIFEST` + `SERVICE_WORKER` en
`ui.py`, iconos en `app/panel/static/` (los sirve `router.py` en `/panel/manifest.webmanifest`,
`/panel/sw.js`, `/panel/static/*`; el SW usa scope `/panel` vía cabecera `Service-Worker-Allowed`).
**Notificaciones** de cada chat entrante/acción: el poll de `/api/events` dispara notificación del
SO (Web Notifications) + toast in-app; clic → abre el chat. Los assets viven bajo `app/` así que el
`COPY app ./app` del Dockerfile ya los incluye (no toca `.dockerignore`).

## Gotchas (esto rompe si no lo sabes)

- **Windows/UTF-8:** corre uvicorn y scripts con `PYTHONUTF8=1` o los emojis/acentos revientan la
  consola cp1252. Y **no confíes en `curl | python -m json.tool`** para ver acentos: mojibakea al
  leer; los datos reales están bien. Lee JSON con `open(f, encoding='utf-8')`.
- **`probar_conversacion` y el webhook golpean Odoo/OpenAI REALES** (gastan tokens, crean
  registros). La `YCLOUD_API_KEY` del `.env` es real → un webhook inbound con número real MANDA
  WhatsApp. Para probar sin efectos usa `probar_conversacion` (no envía) y limpia en Odoo los
  registros de prueba (nómbralos con "PRUEBA"). No hay staging del Odoo del cliente (sin admin).
- **Redis:** escrituras NO idempotentes → `run_write` (1 intento, NO reintenta, evita duplicar
  historial/eventos); lecturas → `with_reconnect`. Ver `app/redis_client.py`. Corre en **1 worker**
  (caches en memoria por-proceso: `prompt_store`, `conocimiento`).
- **Odoo de este cliente NO tiene los campos `website_slug` (product.template) ni `price`
  (product.product)** — se quitaron de `catalogo.py`. No los reintroduzcas.
- **`OPENAI_API_KEY` se propaga a `os.environ` en `settings.py`** porque el SDK y `openai` la leen
  del entorno, no de `settings`.
- **Docker:** el `Dockerfile` DEBE copiar `prompts/` (y `.dockerignore` tiene excepción para
  `prompts/*.md`), o los agentes quedan sin prompt en producción.
