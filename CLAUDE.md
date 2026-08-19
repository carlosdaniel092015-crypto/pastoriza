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
./.venv/Scripts/python.exe -m pytest -q                          # toda la suite (332 tests)
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
solo cubren lógica pura: enrutado, matching, cotización, saneo, entrega, repetición, prompts,
separación por canal; el panel se prueba con TestClient + `tests/fake_redis.py`).

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

**DOS CANALES (`app/canales.py`, ADR-011):** el bot atiende dos números de YCloud (uno
coexistente con la app de WhatsApp) y **cada uno es individual**. Canal = número NUESTRO por el
que entró el mensaje (`ctx.emisor`), normalizado a sus últimos 10 dígitos. Todo lo configurable
vive en dos niveles: `pastoriza:algo` (COMÚN, heredado) y `pastoriza:algo:c:<canal>` (PROPIO,
gana). Aplica a config, prompts (`prompt_store`), reglas/correcciones (`conocimiento`) y agentes
personalizados (`agentes_custom`); todas esas funciones toman `canal=` y las de escritura,
`ambos=True` (única forma de tocar el otro número). `armar_instrucciones` lee `ctx.emisor`, así
que un mismo Agent sirve a los dos. Si agregás algo configurable, seguí ese patrón o el panel
mentirá. `YCLOUD_FROM` **debe quedar vacía** con dos números (si no, todo sale por uno solo; se
avisa al arrancar). Sigue compartido por chat_id: sesión/historial, pausa 30 min y debounce.

**Semáforo de cierre (`app/score.py`, ADR-012):** función PURA que puntúa qué tan cerca está una
conversación de convertirse en pedido, con HECHOS que ya escriben las tools (pidió las cuentas,
comprobante, cotizó, pedido…). Sirve para **ordenar a quién atiende primero una persona** y nada
más: NO se inyecta en ningún prompt, NO cambia una sola respuesta del bot, NO tiene puntaje
negativo y NUNCA mide cómo escribe el cliente (ortografía/audio/largo = proxy de clase, y
`base_comun.md` lo prohíbe). Gris = sin señales todavía ≠ malo. Se calcula en `_puntuar`
(pipeline) y viaja en el chatmeta; el panel tiene un módulo propio (**06 Semáforo**, columnas por
color) y lo muestra siempre con su desglose en texto. Para las conversaciones que ya existían,
`score.reconstruir` lo deduce del historial (sólo mensajes del cliente y SALIDAS DE TOOLS: lo que
redactó el modelo no cuenta) desde `POST /api/chats/calcular-semaforo`, una vez por chat. Si
agregás hitos, agregalos a `PESOS` y NO agregues nada que castigue al mayorista (pedir la lista,
regatear, preguntar mucho). El supervisor puede **moverlo a mano** (`sem_manual`: gana sobre el
cálculo sin pisarlo; vacío vuelve al automático).

**El bot NO da las cuentas (ADR-006):** el pago lo coordina el supervisor por WhatsApp
(`business_config.derivar_pago`, número y mensaje editables por canal). La garantía no es el
prompt: las cuentas NO están en el bloque dinámico de `agents/base.py`, así que el modelo no
las ve y no las puede filtrar. El fast-path de "¿a qué cuenta transfiero?" tampoco las manda.
Siguen en la config para tu referencia, pero ningún camino las emite (`tests/test_seguridad.py`).

**El pago lo aprueba una PERSONA (`app/pagos.py` + `app/aprobacion.py`, ADR-013):** en ENVÍO
`crear_pedido` NO crea nada sin comprobante y exige que cubra el total cotizado (lo compara
`app/comprobante.py` contra `estado.leer_cotizacion`; si no se puede leer el monto NO se
bloquea); en RETIRO no se pide comprobante. **Un pago NO crea otro pedido:** el pedido abierto
del chat vive 7 días (`estado.leer_pedido_abierto`) y el comprobante que llega días después se
aplica a ÉSE, leyendo sus líneas de Odoo; se cierra al aprobar/rechazar. Con el pago en regla el bot
crea el pedido y adjunta el comprobante, pero al cliente le dice sólo "estamos verificando"
(lo fuerza `_sanear`, no el prompt) y el pago queda `pendiente`. Al supervisor (`ADMIN_PHONE`)
le llega una plantilla con cliente, dirección, productos, subtotal/ITBIS/envío/total y dos
botones; el número de pedido sale **sólo** de aprobar (botón o panel). Al rechazar, el cliente
recibe el MOTIVO que escribió el supervisor, tal cual (`msg_rechazado`): desde el panel viene
en el clic, desde WhatsApp el bot se lo PIDE (`estado.motivo_pendiente`) y avisa al contestar. **TODO pedido espera aprobación** (`ctx.espera_aprobacion`),
también el de RETIRO, que no lleva comprobante pero sí decisión — y por eso son DOS plantillas:
`aprobacion_pago` (cabecera de imagen, envío) y `aprobacion_retiro1` (sin cabecera), porque una
plantilla con cabecera de imagen EXIGE imagen en cada envío. Lo que se le dice al cliente nunca
habla de un pago que no existió (4 mensajes editables; la marca guarda `con_pago`). Hay que dar
de alta las plantillas en Meta una vez: **`PLANTILLA_META.md`** tiene el texto exacto; si ese
texto cambia, rehacé los topes de `MAX_*` en `aprobacion.py` (el cuerpo no pasa de 1024). El comprobante se republica en `/panel/media/...`
(`app/media_publica.py`) porque las URLs de YCloud exigen `X-API-Key` y Meta no puede bajarlas.

**Al supervisor (`app/panel/supervisor_log.py`, módulo **08 Al supervisor**):** el panel muestra
conversaciones con CLIENTES, así que lo que el bot le manda al `ADMIN_PHONE` (la plantilla de
aprobación con los botones, los avisos de escalamiento) no se veía en ninguna parte — y si Meta
rechaza la plantilla, el síntoma es justamente que NO llega nada y no hay dónde mirar. Se anota
en una lista capada (`pastoriza:panel:supervisor`, 400 entradas, TTL 30 días) desde
`pagos.avisar_supervisor` y `ycloud.avisar_admin`, con `enviado: bool`; el contador del header
("al supervisor") y el badge del módulo muestran los que NO se entregaron, porque un aviso que no
llegó es un pedido esperando aprobación que el supervisor no sabe que existe. La plantilla vive
en Meta (el bot sólo manda las 9 variables), así que `aprobacion.resumen_legible` las etiqueta
para que en el panel no se vean nueve valores sueltos. Registrar NUNCA puede impedir el envío.

**Media del cliente (`app/panel/media_chat.py`):** el hilo del panel guarda el TEXTO que
se le pasó al modelo (transcripción del audio, análisis visual de la foto), así que una foto
se veía como `## ANALISIS VISUAL: TIPO_ENVASE: Botella...` y una nota de voz sólo como su
transcripción — imposible juzgar si el bot entendió bien. Ahora se guarda una copia del
archivo **a DISCO** (volumen `media-data:/srv/media`), NO a Redis: ahí vive la config del
negocio con `noeviction` y llenarla de fotos haría FALLAR escrituras reales. El índice
(`pastoriza:panel:media:<chat_id>`, TTL 7 días como la sesión) sí va a Redis. Se sirve por
`GET /api/media/{token}` **con** `PANEL_TOKEN` (a diferencia de `/panel/media/...`, que es sin
auth porque ahí entra Meta): son fotos de clientes y comprobantes. La UI lo baja por `fetch`
+ blob porque un `<img src>` no puede mandar el header. `limpiar_viejos()` corre al arrancar:
el TTL de Redis vence la ENTRADA, no el ARCHIVO.

**Uso / observabilidad (`app/panel/uso.py`, módulo **07 Uso**):** tokens gastados y latencia por
día y por agente. Se acumula con `HINCRBY` en una key por día (`pastoriza:panel:uso:<fecha>`, TTL
45 días) en vez de un evento por turno: con cientos de turnos diarios eso sería una lista sin
techo, y el panel sólo necesita totales. Lo registra `_registrar_uso` (pipeline) leyendo
`result.context_wrapper.usage` del SDK, y **nunca puede tumbar el turno**: un fallo ahí sólo
pierde la métrica. NO va por canal (una sola cuenta de OpenAI, un solo servidor). El detalle
por turno igual queda en los logs (`turno_uso`). También **por conversación**
(`pastoriza:panel:uso:chat:<chat_id>`, TTL 7 días como la sesión) con el desglose
entrada/salida por agente, más un ZSET de ranking (`uso:chats`, tope 300) para no escanear una
key por cliente: así se puede ver CUÁL conversación se come los tokens y con qué agente
(no es lo mismo `ventas` en mini que `pedido` en gpt-4o).

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
- **VPS:** `PUBLIC_BASE_URL` es OBLIGATORIA fuera de Railway (sin ella no sale la foto del
  comprobante). Y al mudar Redis hay que COPIAR las keys (`scripts/migrar_redis.py`): ahí
  vive la config de negocio, los prompts del panel y el CRM. Ver **`DESPLIEGUE_VPS.md`**.
- **Docker:** el `Dockerfile` DEBE copiar `prompts/` (y `.dockerignore` tiene excepción para
  `prompts/*.md`), o los agentes quedan sin prompt en producción.
