# Revisión de Arquitectura — Pastoriza Bot · 2026-08-11

Revisión multi-perspectiva (5 revisores en paralelo: seguridad, correctitud/concurrencia,
arquitectura, rendimiento, testing). Este documento consolida hallazgos, lo **aplicado y
verificado** en esta sesión, y lo **pendiente** (que requiere decisión o acción del usuario).

**Valoración global: sólido con brechas graves en el perímetro.** El núcleo de negocio
("garantías en código, no en el prompt": efectos escritos por tools, precio blindado, sin tool
de cancelar) es un diseño excelente y difícil de romper. El riesgo real estaba en (1) un bug de
concurrencia que rompía el bot en producción, (2) el perímetro HTTP sin auth, y (3) el manejo de
secretos. Suite: **127 → 149 tests, todos verdes.**

---

## Tema transversal #1 — Bug crítico del lock (lo señalaron los 5 revisores)

`app/redis_client.py` · `conversation_lock`: el `finally` usaba una variable `r` **inexistente**
→ `NameError` tragado por `contextlib.suppress` → **el lock nunca se liberaba**, solo expiraba por
TTL (120 s). Efecto en producción: tras cada respuesta, si el cliente volvía a escribir dentro de
~2 min (lo normal en ventas), su mensaje se **descartaba en silencio**. Ningún test lo cubría
porque la suite no toca Redis. **✅ Corregido + test de regresión añadido.**

---

## ✅ Aplicado y verificado en esta sesión

| # | Cambio | Archivo | Severidad |
|---|--------|---------|-----------|
| 1 | Lock se libera de verdad (`get_redis().eval`) + TTL 120→180 s + guarda del caso degradado | `redis_client.py` | 🔴 Crítica |
| 2 | Auth (`PANEL_TOKEN`) en `/admin/*`, `/pastoriza-config-save/load`, `/webhook/debug` | `main.py` | 🔴 Crítica |
| 3 | Comparación de tokens con `hmac.compare_digest` (timing-safe) | `main.py`, `panel/router.py` | 🟠 Media |
| 4 | Timeout en OpenAI (`openai_timeout=30s`) + `Runner.run` acotado (`agente_timeout=90s`) | `settings.py`, `pipeline.py`, `media.py` | 🟠 Alta |
| 5 | Reintento del lock ya no descarta en silencio: hace fallback | `pipeline.py` | 🟠 Alta |
| 6 | Single-flight en la caché de catálogo (evita estampida contra Odoo) | `catalogo.py` | 🟠 Alta |
| 7 | Reuso de cliente httpx en descargas de media (antes uno nuevo por llamada) | `media.py`, `main.py` | 🟠 Media |
| 8 | Blindaje de precio extraído a función pura `precio_blindado` (ADR-006) + tests | `tools/odoo_tools.py` | 🟠 Media |
| 9 | `openai-agents` pineado a `==0.19.4`; `pytest-cov` añadido | `requirements.txt` | 🟠 Media |
| 10 | Docs: 149 tests; contradicción réplicas vs 1 worker alineada a ADR-010 | README, ARCHITECTURE, CLAUDE, COMO_FUNCIONA, Dockerfile | 🟡 Baja |

**Tests nuevos (22):** `test_redis_client.py` (lock adquirir/liberar/re-adquirir, degradado,
`run_write`/`with_reconnect`), `test_efectos.py` (invariante `order_id` productivo:
`comprobante_sin_pedido`, `pedido_sin_lineas`, adjuntar comprobante, handoff), `test_pedido_tools.py`
(blindaje de precio, datos de envío, nota de entrega), `test_message_updated.py` (takeover ADR-009).

---

## ⚠️ Pendiente — requiere tu acción o decisión (NO aplicado)

1. 🔴 **ROTAR las 5 credenciales** de `.env` (OpenAI, YCloud, Odoo, Redis Cloud, Telegram). Son
   claves reales de producción y el `.env` vive en una carpeta **OneDrive corporativa** → se
   sincroniza a la nube de Microsoft y a todo equipo vinculado. Asúmelas comprometidas.
2. 🔴 **Sacar el proyecto/`.env` de OneDrive** o excluir `.env` de la sincronización. En Dokploy,
   inyectar secretos por variables de entorno, no por archivo sincronizado.
3. 🟠 **SSRF + fuga de API key en `media.descargar`**: adjunta `X-API-Key` a cualquier URL que
   contenga la subcadena `"ycloud"`, con `follow_redirects=True` y sin validar host. No lo corregí
   porque necesito confirmar el **host real** de la media de YCloud para no romper descargas
   legítimas. Decisión tuya → lo aplico en cuanto lo confirmes.
4. 🟠 **`odoo_uid_fallback=2`** = típicamente admin: el bot opera con privilegios totales si
   `authenticate()` falla. Crear un usuario Odoo de mínimo privilegio (res.partner, sale.order,
   product.*, ir.attachment). Requiere admin de Odoo.
5. 🟡 **Versión vieja + zips en la carpeta padre** (`../agent.py`, `../pipeline.py`, `*.zip`):
   son el monolito anterior, confunden cuál es la fuente de verdad. Recomiendo moverlos a
   `legacy/`. No los toqué porque no los creé yo — dime y los archivo.
6. 🟡 **`git init` + CI**: no hay repo git. Sin historial ni una red que corra los 149 tests en
   cada cambio.

---

## Backlog priorizado (mejoras mayores, esfuerzo estimado)

**Corto plazo:** divergencia historial-vs-enviado tras `_sanear` (el SDK guarda la salida original
del modelo, no la saneada) [M]; dedup por `message_id` en `acumular` para reintentos del webhook
[M]; re-chequear pausa/kill-switch tras tomar el lock [S]; socket timeout en `ServerProxy` de Odoo
+ reconciliación de `create` ante timeout (evita pedidos duplicados) [M]; registro declarativo
único de agentes (hoy en 4 sitios) [M].

**Largo plazo (habilitadores de escala):** propagación de caches vía Redis pub/sub (precondición
dura para >1 réplica) [L]; migrar Odoo XML-RPC bloqueante a cliente async [L]; deploy Dokploy con
Redis co-ubicado [M].
