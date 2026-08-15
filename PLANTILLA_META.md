# Plantilla de WhatsApp: aprobación del pago

Esta es la plantilla que le llega al **supervisor (+1 829 471-6701)** cada vez que un
cliente manda un comprobante y el bot crea el pedido. Trae la **foto del comprobante**,
el cliente con su dirección, los productos con cantidades, subtotal, ITBIS, envío y
total, y **dos botones**: el supervisor aprueba desde ahí, sin entrar al panel.

Hay que darla de alta **una sola vez** y esperar que Meta la apruebe (suele tardar de
minutos a unas horas). Mientras no esté aprobada el bot no se rompe: cae al aviso de
siempre y el pago queda pendiente en el panel (módulo de conversaciones), que es la
otra puerta para aprobarlo.

---

## 1. Dónde se crea

En **YCloud → WhatsApp → Templates → Create template** (o en Meta Business Manager →
Administrador de WhatsApp → Plantillas de mensajes, si preferís hacerlo del lado de
Meta; es la misma plantilla).

## 2. Datos de la plantilla

| Campo | Valor |
|---|---|
| **Nombre** | `aprobacion_pago` |
| **Categoría** | **Utility / Utilidad** (NO Marketing: es un aviso operativo) |
| **Idioma** | el **mismo** que ya usan tus plantillas actuales (`notificar_pedido_creado`, `alerta_supervisor_cliente`). Ver la nota de idioma más abajo. |

### Encabezado (Header)

- Tipo: **Imagen (Media → Image)**
- De ejemplo, para que Meta lo revise, subí **cualquier foto de un comprobante de
  transferencia** (podés tachar los datos). Esa foto es sólo la muestra: en cada envío
  real va el comprobante que mandó el cliente.

### Cuerpo (Body)

Copiá y pegá **exactamente** esto (respetá los saltos de línea):

```
Nuevo pago para aprobar.

Pedido: {{1}}
Entrega: {{2}}
Cliente: {{3}}
Direccion: {{4}}
Productos: {{5}}
Subtotal: RD$ {{6}}
ITBIS: RD$ {{7}}
Envio: RD$ {{8}}
TOTAL: RD$ {{9}}

Revisa el comprobante de arriba. Si el pago esta correcto toca Aprobar pago y el cliente recibe su numero de pedido.
```

### Pie (Footer) — opcional

```
Pastoriza Plastics
```

### Botones (Buttons)

Tipo **Quick Reply / Respuesta rápida**, en **este orden** (el orden importa: el
sistema manda el "aprobar" en el botón 1 y el "rechazar" en el botón 2):

| # | Texto del botón |
|---|---|
| 1 | `Aprobar pago` |
| 2 | `No aprobar` |

## 3. Valores de ejemplo (los pide Meta para revisar)

| Variable | Ejemplo |
|---|---|
| `{{1}}` | `160` |
| `{{2}}` | `ENVIO A DOMICILIO` |
| `{{3}}` | `Clarys Rey (18091112222)` |
| `{{4}}` | `Calle 5 #12, Los Alcarrizos, Santo Domingo` |
| `{{5}}` | `300 x BOTELLA LISA 8 OZ · 300 x TAPA 28MM` |
| `{{6}}` | `4,500.00` |
| `{{7}}` | `810.00` |
| `{{8}}` | `550.00` |
| `{{9}}` | `5,860.00` |

## 4. Nota sobre el idioma

El bot manda la plantilla en el idioma que diga `TEMPLATE_LANG` (por defecto `es_DO`),
y **tiene que coincidir exactamente** con el idioma con el que la creaste, o el envío
falla. Si al crearla Meta sólo te ofrece **Spanish (es)**, creála en `es` y poné en el
`.env`:

```
TEMPLATE_LANG=es
```

Ojo: eso cambia el idioma de **todas** las plantillas, así que las otras dos
(`notificar_pedido_creado`, `alerta_supervisor_cliente`) tienen que estar en ese mismo
idioma. Lo más simple es crear ésta con el mismo idioma que ya tienen las otras.

## 5. Después de que Meta la apruebe

No hay que tocar nada: el nombre `aprobacion_pago` ya está configurado. Si la creaste
con otro nombre, ponelo en el `.env`:

```
TEMPLATE_APROBACION_PAGO=el_nombre_que_le_pusiste
```

Para que la **foto del comprobante** llegue, el servidor tiene que ser alcanzable desde
internet: WhatsApp la descarga de `https://TU_DOMINIO/panel/media/...`. Eso sale de
`PUBLIC_BASE_URL` (o de `RAILWAY_PUBLIC_DOMAIN`). Si esa variable está vacía, la
plantilla sale igual pero **sin la foto**.

---

## Cómo funciona en vivo

1. El cliente manda el comprobante → el bot crea el pedido en Odoo, le adjunta el
   comprobante y le contesta al cliente **"estamos verificando tu pago"** (ese texto se
   edita en el panel, campo *Mensaje al recibir comprobante*). **El bot nunca da el
   pago por bueno ni suelta el número de pedido.**
2. Al 6701 le llega esta plantilla con la foto y el detalle.
3. El supervisor toca:
   - **Aprobar pago** → el bot le escribe al cliente que su pago fue **verificado y
     aceptado**, con el **número de pedido** (texto editable en el panel, campo
     *Mensaje al aprobar el pago*), y la conversación queda marcada como aprobada.
   - **No aprobar** → **al cliente no se le dice nada** (a propósito: eso lo habla una
     persona, con el motivo real) y el caso entra en la cola de revisión del panel.
4. El sistema le responde al supervisor por WhatsApp confirmándole qué pasó.

Si el botón no funcionara (plantilla vieja, cliente de WhatsApp raro), el supervisor
también puede escribir **`aprobar 160`** o **`rechazar 160`** al mismo número y hace
exactamente lo mismo. Y el panel sigue teniendo los botones de siempre.

Sólo el número configurado en `ADMIN_PHONE` puede aprobar: si otro número manda
`aprobar 160`, se trata como un mensaje de cliente cualquiera.
