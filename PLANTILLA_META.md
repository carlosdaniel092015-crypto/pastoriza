# Plantillas de WhatsApp: aprobación del pedido

**Ningún** pedido le llega al cliente con su número hasta que el **supervisor
(+1 829 471-6701)** lo aprueba, y aprueba desde WhatsApp con dos botones, sin entrar al
panel. Para eso hacen falta **DOS plantillas**, porque los dos casos no son iguales:

| Plantilla | Cuándo | Encabezado |
|---|---|---|
| **`aprobacion_pago`** | ENVÍO: el cliente transfirió y mandó el comprobante | **Imagen** (el comprobante) |
| **`aprobacion_retiro1`** | RETIRO en tienda: paga al retirar, no hay comprobante | **Ninguno** |

**¿Por qué dos y no una?** Porque una plantilla con encabezado de imagen **exige** una
imagen en cada envío: si se manda sin foto, Meta rechaza el mensaje entero y el
supervisor se queda sin aviso y sin botones. Y en retiro no hay foto que mandar. El
cuerpo y los botones son los mismos en las dos, así que es copiar y pegar.

Hay que darlas de alta **una sola vez** y esperar que Meta las apruebe (suele tardar de
minutos a unas horas). Mientras no estén aprobadas el bot no se rompe: cae al aviso de
siempre y el pedido queda pendiente en el panel (módulo de conversaciones), que es la
otra puerta para aprobarlo.

---

## 1. Dónde se crea

En **YCloud → WhatsApp → Templates → Create template** (o en Meta Business Manager →
Administrador de WhatsApp → Plantillas de mensajes, si preferís hacerlo del lado de
Meta; es la misma plantilla).

## 2. Datos de la plantilla `aprobacion_pago` (ENVÍO, con comprobante)

| Campo | Valor |
|---|---|
| **Nombre** | `aprobacion_pago` |
| **Categoría** | **Utility / Utilidad** (NO Marketing: es un aviso operativo) |
| **Idioma** | el **mismo** que ya usan tus plantillas actuales (`notificar_pedido_creado`, `alerta_supervisor_cliente`). Ver la nota de idioma más abajo. |

### Encabezado (Header)

- Tipo: **Imagen (Media → Image)** ← es lo más fácil de dejar en "Ninguno", y es
  justamente donde va el comprobante.
- De ejemplo, para que Meta lo revise, subí **cualquier foto de un comprobante de
  transferencia** (podés tachar los datos). Esa foto es sólo la muestra: en cada envío
  real va el comprobante que mandó el cliente.

**Si la plantilla queda sin encabezado** no se rompe nada, pero se pierde lo mejor:
mandarle a una plantilla un encabezado que no declara hace que Meta rechace el mensaje
ENTERO, así que el bot lo detecta, reintenta **sin** la foto (el aviso y los botones
salen igual) y le manda al supervisor **el comprobante aparte**, como imagen suelta.
Ese envío suelto depende de la ventana de 24 h de WhatsApp, así que es un parche: lo
que corresponde es agregarle el encabezado de imagen cuando la plantilla esté activa.

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

## 3b. Datos de la plantilla `aprobacion_retiro1` (RETIRO, sin comprobante)

Es la misma, con tres diferencias. **Nombre:** `aprobacion_retiro1` (con el `1`
al final: así se dio de alta en Meta, y el bot la busca con ese nombre exacto). Categoría, idioma,
variables de ejemplo y pie: **idénticos** a la de arriba.

- **Encabezado: Ninguno.** Acá sí va en Ninguno, y es a propósito: no hay comprobante.
- **Cuerpo** (mismas 9 variables en el mismo orden, sólo cambia la primera línea y la
  última):

```
Nuevo pedido para aprobar.

Pedido: {{1}}
Entrega: {{2}}
Cliente: {{3}}
Direccion: {{4}}
Productos: {{5}}
Subtotal: RD$ {{6}}
ITBIS: RD$ {{7}}
Envio: RD$ {{8}}
TOTAL: RD$ {{9}}

Es retiro en tienda y paga al retirar, no hay comprobante. Si esta todo bien toca Aprobar pedido y el cliente recibe su numero.
```

- **Botones** (respuesta rápida, en este orden): `Aprobar pedido` · `No aprobar`.

Los valores de ejemplo pueden ser los mismos, cambiando `{{2}}` por `RETIRO EN TIENDA`,
`{{4}}` por `Retiro en tienda` y `{{8}}` por `0.00`.

> Las 9 variables se mantienen aunque en retiro el envío sea siempre `0.00`: el bot arma
> el cuerpo una sola vez para las dos plantillas. Un `Envio: RD$ 0.00` de más es más
> barato que dos formas distintas de armar el mismo mensaje.

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

## 5. Después de que Meta las apruebe

No hay que tocar nada: los nombres `aprobacion_pago` y `aprobacion_retiro1` ya están
configurados. Si les pusiste otros, ponelos en el `.env`:

```
TEMPLATE_APROBACION_PAGO=el_nombre_que_le_pusiste
TEMPLATE_APROBACION_RETIRO=el_otro_nombre
```

Para que la **foto del comprobante** llegue, el servidor tiene que ser alcanzable desde
internet: WhatsApp la descarga de `https://TU_DOMINIO/panel/media/...`. Eso sale de
`PUBLIC_BASE_URL` (o de `RAILWAY_PUBLIC_DOMAIN`). Si esa variable está vacía, la
plantilla sale igual pero **sin la foto**.

---

## Cómo funciona en vivo

### Envío (hubo transferencia)

1. El cliente manda el comprobante → el bot verifica que el monto cubra el total, crea el
   pedido en Odoo, le adjunta el comprobante y le contesta **"estamos verificando tu
   pago"** (texto editable en el panel, *Cuando llega el comprobante*). **Nunca le da el
   número de pedido.**
2. Al 6701 le llega **`aprobacion_pago`** con la foto y el detalle.
3. El supervisor toca:
   - **Aprobar pago** → el cliente recibe que su pago fue **verificado y aceptado**, con
     el **número** (*Cuando TÚ apruebas el pago*).
   - **No aprobar** → **al cliente no se le dice nada** (a propósito: eso lo habla una
     persona, con el motivo real) y el caso entra en la cola de revisión del panel.

### Retiro en tienda (paga al retirar)

1. El cliente confirma → el bot crea el pedido en Odoo, **sin pedir comprobante**, y le
   dice que quedó **tomado y en revisión** (*Retiro: cuando toma el pedido*). Tampoco le
   da el número.
2. Al 6701 le llega **`aprobacion_retiro1`**: mismo detalle, mismos botones, sin foto.
3. **Aprobar pedido** → el cliente recibe el **número** y que lo esperan en la tienda,
   donde paga al retirar (*Cuando TÚ apruebas un retiro*). Acá no se le dice "pago
   verificado", porque todavía no pagó.

En los dos casos, el sistema le responde al supervisor por WhatsApp confirmándole qué
pasó.

Si el botón no funcionara (plantilla vieja, cliente de WhatsApp raro), el supervisor
también puede escribir **`aprobar 160`** o **`rechazar 160`** al mismo número y hace
exactamente lo mismo. Y el panel sigue teniendo los botones de siempre.

Sólo el número configurado en `ADMIN_PHONE` puede aprobar: si otro número manda
`aprobar 160`, se trata como un mensaje de cliente cualquiera.
