# Mudanza al VPS (Contabo + Dokploy)

Guía de la mudanza real, en orden, con lo que ya está roto si te lo salteás. Los pasos
de Dokploy en sí están en el `README.md`; acá está **lo específico de esta mudanza**.

> **Lo más importante de todo:** en Redis no vive sólo cache. Vive la **config de
> negocio** (precios, mínimos, mensajes, los dos canales), los **prompts** que editaste
> desde el panel, las **reglas aprendidas**, la cola de revisión y el **índice de
> conversaciones del CRM**. Si levantás el bot apuntando a un Redis vacío, no explota
> nada: arranca con los defaults del código y el panel aparece sin conversaciones, como
> si el negocio empezara de cero. Por eso el paso 4 no es opcional.

---

## 1. Entrar por SSH

Desde tu PC (PowerShell en Windows 10/11 ya trae `ssh`):

```powershell
ssh root@TU_IP
```

La primera vez pregunta si confiás en la huella del servidor: escribí `yes`. Después
pide la contraseña que te mandó Contabo por correo (al escribirla no se ve nada, es
normal: escribí y Enter).

Si te dice `Connection refused` o queda colgado, entrá al panel de Contabo y usá la
**consola VNC**: desde ahí ves si el server arrancó.

**Lo primero que conviene hacer, ya adentro:**

```bash
passwd                      # cambiá la contraseña que vino por correo
apt update && apt upgrade -y
```

Y para no depender de una contraseña, copiá tu llave (desde tu PC, no desde el server):

```powershell
ssh-keygen -t ed25519          # si no tenés una todavía; Enter a todo
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@TU_IP "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

Probá entrar de nuevo: ya no debería pedir contraseña.

## 2. Dokploy

Si el VPS no lo trae instalado:

```bash
curl -sSL https://dokploy.com/install.sh | sh
```

Después entrás a `http://TU_IP:3000` y creás el usuario admin. **Hacelo enseguida**:
mientras no exista, cualquiera que sepa la IP puede crearlo.

## 3. Dominio

El bot **necesita un dominio público con HTTPS**, no alcanza la IP: WhatsApp descarga
de ahí la foto del comprobante (ver `PLANTILLA_META.md`) y no acepta HTTP ni
certificados inválidos.

1. En tu DNS, un registro **A**: `bot.tudominio.com` → `TU_IP`.
2. En Dokploy, al crear la app: **Domain** `bot.tudominio.com`, puerto `8000`, HTTPS
   con Let's Encrypt. Traefik ya viene con Dokploy.

## 4. Redis: copiar los datos ANTES de cambiar el webhook

Creá el Redis nuevo (en Dokploy: *Create Database → Redis*, o descomentá el bloque del
`docker-compose.yml`). Que tenga **estas tres cosas**:

```
--appendonly yes                 # persistencia: sobrevive a un reinicio
--maxmemory-policy noeviction    # al llenarse NO borra config ni sesiones en silencio
--requirepass UNA_CLAVE_LARGA    # si queda sin clave y el puerto se expone, es de todos
```

`noeviction` es el que más caro sale si falta: con la política por defecto, cuando
Redis se llena empieza a borrar keys al azar — y algunas de esas keys son tus precios.

Después copiá los datos del Redis actual al nuevo, **desde tu PC**, con los dos
accesibles:

```bash
# primero en seco: muestra qué copiaría y no toca nada
python -m scripts.migrar_redis --origen "redis://...redis-cloud...:6379/1" --destino "redis://:CLAVE@TU_IP:6379/1"

# y cuando el listado te cierre:
python -m scripts.migrar_redis --origen "..." --destino "..." --si
```

El origen sólo se lee. Se puede correr dos veces sin problema. Las keys efímeras
(debounce, locks) no se copian a propósito.

> Si el Redis nuevo no está expuesto a internet (bien hecho), corré el script **en el
> VPS**: clonás el repo, `pip install redis` y usás `--destino redis://:CLAVE@localhost:6379/1`.

## 5. Variables de entorno

Copiá tu `.env` actual y **revisá estas cuatro**, que son las que cambian al mudarse:

| Variable | Qué poner | Si falla |
|---|---|---|
| `PUBLIC_BASE_URL` | `https://bot.tudominio.com` | **La foto del comprobante no llega** al supervisor y las fotos de producto salen por un proxy externo |
| `REDIS_URL` | el Redis nuevo, con su clave | El bot no arranca |
| `YCLOUD_FROM` | **vacía** | Con dos números, todo sale por uno solo y las conversaciones se registran en el canal equivocado |
| `PANEL_TOKEN` | uno largo (`openssl rand -hex 32`) | El panel y `/admin/*` quedan **sin auth** en internet |

El resto (`OPENAI_API_KEY`, `YCLOUD_API_KEY`, Odoo, plantillas) va igual que hoy. Ver
`.env.example`, que las tiene todas comentadas.

## 6. Desplegar y verificar ANTES de mover el webhook

```bash
curl https://bot.tudominio.com/health
curl -H "X-Panel-Token: TU_TOKEN" https://bot.tudominio.com/health/deep
```

`/health/deep` tiene que decir `"status": "ok"` con `redis: true`, `odoo: true` y
`catalogo` > 0. Si el catálogo da 0, el bot le va a decir a todos los clientes que no
hay productos: **no muevas el webhook hasta que eso esté en verde.**

Después abrí `https://bot.tudominio.com/panel` y confirmá que **están tus
conversaciones y tu config**. Si el panel aparece vacío, la copia del paso 4 no salió:
paralo acá, no sigas.

## 7. Mover el webhook de YCloud (el interruptor)

Este es el único paso que corta el servicio, y dura segundos. En YCloud → Webhooks,
cambiá la URL a:

```
https://bot.tudominio.com/webhook/ycloud?token=TU_WEBHOOK_TOKEN
```

(YCloud no permite headers propios, por eso el token va en la URL.)

Mandate un mensaje de prueba desde tu celular a cada uno de los dos números y mirá los
logs en Dokploy. Los eventos que tienen que aparecer: `entrante`, `agente_elegido`.

**Vuelta atrás:** si algo sale mal, volvés la URL del webhook a la de Railway y todo
sigue como antes — Railway queda andando hasta que decidas apagarlo. Por eso conviene
no apagarlo el mismo día.

## 8. Después de que ande

- **Backups de Redis.** El volumen persiste, pero un volumen no es un backup. Un cron
  diario que copie el `dump.rdb` afuera del VPS alcanza. Sin eso, perder el disco es
  perder la config, los prompts y el CRM.
- **Apagá Railway** cuando lleves un par de días estable.
- `REDIS_MAX_CONEXIONES=40`: con Redis local ya no estás contra el límite de 30
  clientes del plan gratis, y el panel carga más rápido.
- **Una sola réplica.** El bot corre con 1 worker a propósito (ADR-010): las caches de
  prompts y conocimiento son por proceso, así que con 2 réplicas un cambio del panel
  aplicaría sólo a una. No escales horizontalmente sin leer ese ADR.
- **No abras el puerto de Redis** al mundo. Si está en la misma red de Docker, se
  alcanza por nombre de servicio y no necesita salir a internet.
