# Pastoriza WhatsApp Bot

Servicio FastAPI + OpenAI Agents SDK que reemplaza el workflow de n8n completo.
WhatsApp (YCloud) → agente conversacional → Odoo (XML-RPC).

---

## ⚠️ Antes que nada: rotá las credenciales

El JSON de n8n tenía en texto plano la **OpenAI API key**, la **contraseña de
Odoo** y la **API key de YCloud**. Rotá las tres antes de poner esto en marcha.
En este proyecto ninguna credencial está en el código: todas salen de `.env`.

---

## Qué hace distinto que n8n

| Problema en n8n | Cómo se resuelve acá |
|---|---|
| El agente decía "tu pedido quedó registrado" sin haberlo creado | El `order_id` sólo lo escribe la tool. Si es `None`, el mensaje se reemplaza (`_sanear`) |
| Etiquetas `<IMG>` que el modelo debía copiar verbatim | El modelo devuelve **ids**; las URLs las arma el servicio. No puede inventar una URL |
| Reusaba la foto de un turno anterior | `_resolver_fotos` sólo acepta ids que una tool devolvió **en este turno** |
| `Wait 6s` dejaba una ejecución abierta por mensaje | Debounce en una task de asyncio; el webhook responde en milisegundos |
| Loops infinitos de tool-calling | `max_turns=12`, corta de raíz |
| ~100 líneas de regex para cazar respuestas falsas | 10 líneas de red de seguridad; el resto es imposible por construcción |
| Nada se podía testear | 149 aserciones en `pytest`, sin tocar Odoo ni OpenAI |

## Arquitectura

```
YCloud webhook
      │
      ▼
POST /webhook/ycloud ─── responde 200 en ~5 ms
      │
      ├─ .on / .off del encargado ──▶ pausar/reactivar bot
      │
      └─ mensaje del cliente
             │
             ▼
        acumular en Redis (buffer)
             │
             ▼  task asyncio, espera DEBOUNCE_SECONDS
        ¿soy el último mensaje? ── no ──▶ descartar
             │ sí
             ▼
        lock de conversación (Redis)
             │
             ▼
        drenar buffer → combinar (texto + audio transcrito + imagen analizada)
             │
             ├─ fast-path (horario, cuentas, saludo…) ──▶ enviar, fin
             │
             ▼
        Agente (Runner.run + RedisSession + 12 tools)
             │
             ▼
        RespuestaBot{mensaje, mostrar_productos[], escalar}
             │
             ├─ _sanear()        ← no confirmar pedidos inexistentes
             ├─ _resolver_fotos()← sólo productos de este turno
             ▼
        enviar por YCloud (chunking + delay de tipeo)
             │
             ▼
        efectos: adjuntar comprobante · avisar admin · cola de revisión
```

## Estructura

```
app/
  main.py            FastAPI: webhook, config, admin, health
  models.py          parseo del payload YCloud + referral de anuncios
  pipeline.py        orquestación de un turno completo
  debounce.py        buffer + ventana de espera
  agent.py           el agente Michelle (instructions dinámicas)
  context.py         estado del turno; las tools escriben acá
  session.py         historial en Redis (protocolo Session del SDK)
  router.py          fast-path determinista
  catalogo.py        productos desde Odoo, con caché
  matching.py        scoring de búsqueda (port fiel del JS)
  odoo.py            cliente XML-RPC
  ycloud.py          envío de texto/imágenes/plantillas
  media.py           descarga, transcripción, visión
  estado.py          pausa, ventana 24 h, cola de revisión
  business_config.py config editable por el cliente (Redis)
  tools/             las 12 tools del agente
scripts/
  indexar_fichas.py       genera el índice visual (requisito de buscar_por_foto)
  probar_conversacion.py  chatear con el bot desde la terminal
  mapear_anuncios.py      asignar ad_id de Facebook -> producto
tests/                    149 aserciones, corren en <4 s
```

## Puesta en marcha local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # completá las credenciales
pytest                    # todo verde antes de seguir
uvicorn app.main:app --reload
curl localhost:8000/health/deep     # redis + odoo + catálogo
```

Probar el bot sin WhatsApp:

```bash
python -m scripts.probar_conversacion --limpiar
python -m scripts.probar_conversacion --anuncio 52579732276546
```

Generar el índice de fotos (sin esto `buscar_por_foto` no devuelve nada):

```bash
python -m scripts.indexar_fichas
```

---

## Despliegue en Dokploy

Dokploy corre sobre Docker en tu VPS y trae Traefik con SSL automático.

> Si estás **mudando** el bot de Railway a un VPS, seguí **`DESPLIEGUE_VPS.md`**: tiene
> el orden de los pasos y las cuatro variables que cambian. Lo que más caro sale es
> olvidarse de copiar Redis (`scripts/migrar_redis.py`): ahí vive la config de negocio,
> los prompts editados y el CRM, así que sin eso el panel arranca vacío.

### Opción A — desde Git (recomendada)

1. Subí este proyecto a un repo privado (GitHub/Gitea). El `.gitignore` ya
   excluye `.env`.
2. En Dokploy: **Create Application** → *Provider: Git* → tu repo y rama.
3. **Build Type: Dockerfile**, path `./Dockerfile`.
4. **Environment**: pegá el contenido de tu `.env` (los valores reales).
5. **Domain**: `bot.tudominio.com`, puerto interno `8000`, HTTPS + Let's Encrypt.
6. **Deploy**. Verificá en `https://bot.tudominio.com/health`.
7. Activá **Auto Deploy** con el webhook del repo para que cada push despliegue.

### Opción B — Docker Compose

**Create Compose** → pegá `docker-compose.yml` → cargá las variables en
Environment → Deploy. Si usás esta vía, la red `dokploy-network` ya existe.

### Redis

Ya tenés uno para n8n: reusalo y evitá un segundo servicio.

- Si n8n corre en Dokploy, el hostname es el nombre del servicio:
  `REDIS_URL=redis://redis:6379/1`
- Distinto **número de base** (`/1`) y distinto `REDIS_PREFIX` para no pisar keys.
- Asegurate de que ambos contenedores estén en `dokploy-network`.

Si preferís uno dedicado, descomentá el bloque `redis` del compose.

### Cloudflare Tunnel

Podés usar cualquiera de las dos:

- **Traefik de Dokploy** (más simple): dominio real con SSL, sin tunnel.
- **Tu tunnel actual**: apuntá el ingress a `http://localhost:8000`. El
  contenedor ya publica sólo en `127.0.0.1`, así que no queda expuesto.

### Salud y reinicios

`restart: unless-stopped` + el `HEALTHCHECK` del Dockerfile hacen que Docker
reinicie el contenedor si deja de responder. Eso reemplaza el "n8n se reinicia
solo".

### Escalado

**No subas `--workers`**: el debounce usa tasks de asyncio.

**Por ahora corré 1 sola réplica** (ver ADR-010 en `ARCHITECTURE.md`). El debounce
sí es multi-réplica seguro (el `last_id` va contra Redis), pero las caches en
memoria de **prompts y conocimiento** (`app/panel/prompt_store.py`,
`app/panel/conocimiento.py`) NO se propagan entre procesos: con varias réplicas,
un cambio de prompt o una corrección aprobada desde el panel solo aplicaría a la
réplica que atendió esa request, y las demás servirían prompts viejos sin error
visible. Escalar a >1 réplica requiere antes implementar la propagación de caches
(Redis pub/sub), que está pendiente.

---

## Migrar sin apagar nada

1. **Desplegá el servicio** con `ALLOWLIST_NUMEROS=tu_numero_de_prueba`.
   Todo lo demás lo sigue atendiendo n8n.
2. **Apuntá el webhook de YCloud** al servicio nuevo. Los números fuera de la
   allowlist se descartan acá; agregá una copia del webhook hacia n8n durante
   la transición.
3. **Probá un pedido completo**: búsqueda → cotización → contacto →
   comprobante → pedido en Odoo con el comprobante adjunto.
4. **Sumá números** a la allowlist de a poco.
5. **Vaciá `ALLOWLIST_NUMEROS`** cuando confíes en el servicio.
6. **Desactivá el workflow de n8n.**

Antes del paso 5, corré `pytest` con casos reales de tu catálogo en
`tests/test_matching.py`: los que están son de ejemplo.

---

## Confirmar el referral de los anuncios

Todavía no está confirmado bajo qué nombre exacto manda YCloud el objeto
`referral`. El parseo es tolerante (busca `referral`, `sourceId`, `ctwaClid`,
etc. en cualquier nivel), pero conviene confirmarlo:

1. Apuntá temporalmente el webhook de YCloud a `POST /webhook/debug`.
2. Desde un teléfono de prueba, hacé clic en el anuncio y mandá un mensaje.
3. Buscá el payload en los logs: `docker logs pastoriza-bot | grep payload_crudo`.
4. Si el campo tiene otro nombre, agregalo a `CLAVES_REFERRAL` /
   `ALIAS_REFERRAL` en `app/models.py` y sumá el caso a `tests/test_models.py`.

### Mapa anuncio → producto

El referral trae el `ad_id` pero **no un SKU**. Hay que mapearlo:

```bash
curl -X POST https://bot.tudominio.com/admin/anuncios \
  -H 'Content-Type: application/json' \
  -d '{"ad_id":"52579732276546","product_tmpl_id":42}'

curl https://bot.tudominio.com/admin/catalogo   # ver ids disponibles
curl https://bot.tudominio.com/admin/anuncios   # ver el mapa actual
```

O desde la terminal, en modo interactivo (te muestra además qué anuncios ya
recibieron mensajes y siguen sin mapear):

```bash
python -m scripts.mapear_anuncios
```

Los anuncios sin mapear entran solos a la cola de revisión.

---

## Revisión por excepción

En vez de leer todas las conversaciones, mirá sólo lo que el bot marcó:

```bash
curl https://bot.tudominio.com/admin/revision | jq
```

Entran a la cola: búsqueda ambigua, foto sin match claro, cantidad rara,
comprobante que no terminó en pedido, pedido sin líneas, handoff a humano,
anuncio sin mapear, `max_turns` excedido.

## Endpoints

| Método | Ruta | Para qué |
|---|---|---|
| POST | `/webhook/ycloud` | webhook de YCloud (header `X-Webhook-Token`) |
| POST | `/webhook/debug` | loguea el payload crudo |
| GET | `/pastoriza-config-load` | compatible con el panel actual |
| POST | `/pastoriza-config-save` | compatible con el panel actual |
| GET/DELETE | `/admin/revision` | cola de revisión |
| GET/POST | `/admin/anuncios` | mapa ad_id → producto |
| GET | `/admin/catalogo` | productos e ids |
| POST | `/admin/pausar/{chat_id}` | pausar el bot en una conversación |
| POST | `/admin/reactivar/{chat_id}` | reactivar |
| GET | `/health`, `/health/deep` | salud |

Los endpoints `/admin/*` no tienen auth: **poneles Basic Auth en Traefik** o
dejalos accesibles sólo por el tunnel.

## Notas de implementación

- **`RedisSession`** está implementada a mano (protocolo `Session` del SDK) para
  no depender de la versión de `openai-agents`. Si tu versión trae
  `agents.extensions.memory.RedisSession`, podés cambiarla; la interfaz es la misma.
- **XML-RPC** requiere plan Custom en Odoo Online. En self-hosted
  (`pastorizaplastic.net`) viene habilitado. Si `authenticate()` falla, cae al
  `ODOO_UID_FALLBACK`.
- **Precio blindado**: `agregar_linea_pedido` compara el precio contra el del
  catálogo y lo corrige si no coincide. El modelo no puede cambiar precios.
- **Welcome message de CTWA**: Meta ya mandó un saludo antes de que llegue el
  primer mensaje. Por eso, cuando hay `ad_id`, el fast-path se desactiva y el
  agente arranca sabiendo de qué anuncio viene el cliente.
