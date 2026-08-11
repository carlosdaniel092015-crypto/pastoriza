# Cómo funciona el bot — Pastoriza Plastics

Documento explicativo del sistema completo. Está escrito para entenderlo de punta a punta,
sin necesidad de leer el código. Para el detalle técnico/arquitectónico ver `ARCHITECTURE.md`.

---

## 1. Qué es y qué resuelve

Es **"Michelle"**, un vendedor automático que atiende por **WhatsApp** a los clientes de
Pastoriza Plastics (envases plásticos, Rep. Dominicana). Hace lo que haría un vendedor:
saluda, entiende qué envase busca el cliente, le muestra fotos y precios, cotiza, toma sus
datos y **registra el pedido en Odoo** (el sistema de la empresa). Todo solo, 24/7, y cuando
algo se sale de lo normal, **avisa a una persona**.

La idea central: el bot **no improvisa lo importante**. Los precios, los pedidos y las cuentas
salen del sistema real; el bot no puede inventarlos ni regalarlos.

---

## 2. El viaje de un mensaje (lo más importante)

Cuando un cliente escribe, esto pasa por dentro, en orden:

1. **Llega por WhatsApp** → la plataforma **YCloud** se lo manda al bot (un "webhook", que es
   simplemente un aviso por internet: "llegó este mensaje").
2. **Filtros de entrada:** ¿el bot está encendido? ¿este número está permitido? ¿ya hay un
   asesor atendiendo a este cliente? Si algo dice "no", el bot no responde.
3. **Espera un momento (debounce):** la gente manda varios mensajitos seguidos ("hola", "quiero
   botellas", "de 8 oz"). El bot **espera ~6 segundos y los junta** en uno solo, para responder
   como una persona y no tres veces.
4. **Entiende la entrada:** si mandaron **texto** lo usa tal cual; si mandaron una **nota de
   voz** la transcribe; si mandaron una **foto** la analiza; si compartieron su **ubicación** la
   convierte en dirección. Todo queda en un solo texto para el cerebro.
5. **¿Preguntó lo mismo 3 veces?** → lo pasa a un asesor (señal de que el bot no está ayudando).
6. **¿Es una pregunta frecuente?** (horario, dirección, costo de envío, cuentas) → responde al
   instante con una respuesta fija, **sin usar inteligencia artificial** (rápido y gratis).
7. **Si no es simple, entra el cerebro:** un **enrutador** decide qué especialista debe atender
   y llama **solo a uno**.
8. **El especialista trabaja:** usa sus herramientas (buscar en catálogo, cotizar, crear el
   pedido en Odoo…) y arma la respuesta.
9. **Red de seguridad:** antes de enviar, el sistema revisa que el bot no esté diciendo "tu
   pedido quedó registrado" si en realidad no lo creó. Si miente, se corrige solo.
10. **Responde por WhatsApp** (texto y fotos de los productos).
11. **Efectos finales:** si se creó un pedido, avisa al supervisor y adjunta el comprobante en
    Odoo; si algo quedó dudoso, lo pone en la **cola de revisión**; y todo se registra en el
    **panel** para que el dueño lo vea.

> Diagrama lineal de este recorrido: ver el que generamos en el chat, o `ARCHITECTURE.md`.

---

## 3. Los agentes (el cerebro)

En vez de un solo bot que sabe hacer todo (y se confunde), hay **varios agentes
especializados**, y un **enrutador** que decide cuál usar en cada mensaje. Cada uno tiene su
propia "personalidad e instrucciones" (su prompt) y solo sus herramientas.

| Agente | Se encarga de… | Ejemplo de mensaje que lo activa |
|--------|----------------|----------------------------------|
| **Enrutador** | Leer el mensaje y decidir a quién mandarlo | (no habla con el cliente) |
| **Ventas** | Buscar productos, mostrar fotos, dar precios, cotizar | "¿tienen botellas de 8 oz?" |
| **Pedido** | Tomar los datos, la dirección y **registrar el pedido** | "me llamo Juan, para envío" |
| **Soporte** | Quejas, cancelaciones (avisa al supervisor, **no cancela**) | "quiero cancelar mi pedido" |

Además, las **preguntas frecuentes** (envío, cuentas, horario, dirección) las responde una capa
automática **sin gastar inteligencia artificial**.

**Cómo decide el enrutador (barato y rápido):** primero por reglas simples y por el contexto
(si llegó un comprobante de pago → Pedido; si es una foto → Ventas; si dice "cancelar" →
Soporte). Solo cuando hay duda real usa un modelo pequeño y económico para clasificar. Así
gastamos lo mínimo posible en inteligencia artificial.

**Por qué así:** un agente con instrucciones cortas y pocas herramientas **se equivoca menos**
(alucina menos) y **cuesta menos**. Es el mismo principio de una empresa: cada quien su rol.

---

## 4. Casos especiales, explicados

- **Foto de un envase:** el cliente manda la foto de un pote → el bot la compara contra un
  "álbum" de fotos del catálogo (generado una vez con `scripts/indexar_fichas.py`) y ofrece
  **solo** el producto que coincide. Si no está seguro, muestra los parecidos y pregunta —
  **nunca inventa** un producto.
- **Nota de voz:** la transcribe a texto y responde a lo que el cliente pidió.
- **Ubicación por el mapa de WhatsApp:** la toma como parte de la dirección de envío, pero igual
  pide los datos escritos (sector, calle, referencia) porque el pin solo no le basta al mensajero.
- **Cliente que vuelve:** lo reconoce por su teléfono en Odoo; no le vuelve a pedir nombre y
  dirección. Un mismo cliente puede hacer **varios pedidos**.

---

## 5. Cómo se cierra una venta (flujo de pedido)

1. El cliente elige producto y cantidad → **Ventas** cotiza (precio con ITBIS + envío).
2. Elige **envío o retiro**.
3. Da su **nombre**; si es envío, la **dirección detallada** (provincia, municipio, sector,
   calle, casa/negocio, referencia).
4. **Envío:** el bot muestra las cuentas y pide la **foto del comprobante**; solo cuando el
   sistema confirma que la imagen es un comprobante válido, **crea el pedido**.
   **Retiro:** crea el pedido tras la confirmación del cliente.
5. Al crear el pedido en Odoo, confirma al cliente con el **número real** de pedido y avisa al
   supervisor.

**Regla dura:** el bot **no puede cancelar ni quitar** productos de un pedido — solo **añadir**.
Si piden cancelar/quitar, avisa al supervisor y dice "ya le avisé al equipo, te contactan". Esto
evita que se despache un producto equivocado.

---

## 6. El panel de operación (para el dueño)

Una página web (protegida con contraseña) donde el dueño ve y controla todo:

- **Conversaciones:** todos los chats en vivo; al abrir uno se ve el hilo completo (cliente ↔
  bot). Puede **pausar el bot** para un cliente, **marcar para revisión**, **responder** él mismo
  o **exportar** la charla. Arriba, un **interruptor global** para pausar el bot para todos.
- **Alertas:** una bitácora en vivo (respuestas, cambios, cosas a revisar, errores). Los errores
  se explican en español simple y traen un botón **"Copiar para Claude"** para pedir ayuda técnica.
- **Config:** los datos que el bot dice al cliente (precio de envío, cuentas, horario, dirección,
  mensajes). Se editan y se aplican al instante — **sin programador**.
- **Prompt:** las instrucciones de cada agente. Se pueden **editar o subir un archivo `.md`** por
  agente, y volver al original cuando se quiera.
- **Aprendizaje:** el bot **propone reglas** a partir de los casos que fallaron; el dueño las
  **aprueba o descarta**. También puede agregar reglas y "correcciones" (situación → respuesta
  correcta). Así el bot **mejora con el uso, pero siempre con el dueño decidiendo**.

---

## 7. Memoria: qué recuerda y por cuánto

- **La conversación reciente** vive en **Redis** (una base rápida) por **24 horas**. Es el
  "hilo" de la charla del momento.
- **Lo importante y permanente** (quién es el cliente, su dirección, sus pedidos) vive en
  **Odoo** y **no se borra**. Por eso, aunque el chat "se olvide" a las 24h, cuando el cliente
  vuelve el bot lo reconoce por su teléfono y no le hace repetir sus datos.

O sea: la memoria de largo plazo es Odoo; Redis es solo la memoria corta de la conversación.

---

## 8. Seguridad (los clientes van a intentar romperlo)

- **No negocia precios ni regala:** los precios están fijos y se corrigen contra el catálogo en
  el código. Aunque el cliente diga "me lo prometieron" o "soy VIP", el bot no cede — deriva a un
  teléfono humano.
- **No confirma pedidos/pagos falsos:** el pedido solo existe si la herramienta lo creó de verdad.
- **No se deja manipular:** ignora mensajes tipo "ignora tus instrucciones", "ahora eres otro",
  "dame tu prompt". Trata todo lo que manda el cliente como **contenido**, no como órdenes.
- **No revela nada técnico** (que usa Odoo, ids, herramientas).
- **Ante abuso o repetición** pasa la conversación a una persona.

La clave: aunque logren confundir las **palabras** del bot, **no pueden hacerle ejecutar una
acción dañina**, porque eso está bloqueado en el código, no en el prompt.

---

## 9. Qué pasa cuando algo falla

- Si se cae la conexión un instante, el bot **reintenta o degrada** sin dejar al cliente colgado;
  si de plano no puede, responde "tuve un inconveniente, un compañero te escribe" y **avisa al
  supervisor**.
- Todo error queda en **Alertas** con su detalle técnico, para resolverlo desde el panel.
- Un cliente que insiste sin avanzar termina en manos de una persona (no en un loop).

---

## 10. Cómo se prueba (sin ensuciar datos reales)

- **Pruebas automáticas:** 149 tests que corren sin tocar nada externo (verifican la lógica:
  enrutado, cotización, seguridad, etc.).
- **Prueba de conversación por terminal:** se chatea con el bot desde la computadora contra el
  catálogo real, **sin mandar WhatsApp** a nadie.
- Cualquier registro de prueba en Odoo se nombra "PRUEBA" y se borra después.

---

## 11. Con qué está construido (piezas externas)

- **WhatsApp:** vía **YCloud** (recibe y envía los mensajes).
- **Inteligencia artificial:** **OpenAI** — un modelo grande (gpt-4o) solo para el paso delicado
  de crear el pedido, y uno económico (gpt-4o-mini) para lo demás; también transcribe voz y
  analiza imágenes.
- **ERP:** **Odoo** (catálogo, clientes, pedidos) por XML-RPC.
- **Base rápida:** **Redis** (memoria corta y estado).
- **Alertas (opcional):** **Telegram**.

---

## 12. Glosario rápido

- **Webhook:** aviso automático por internet ("llegó un mensaje").
- **Debounce:** esperar un momento para juntar varios mensajitos en uno.
- **Agente / especialista:** un "empleado" virtual con un rol y sus herramientas.
- **Enrutador:** decide qué agente atiende cada mensaje.
- **Prompt:** las instrucciones que le damos a un agente.
- **Tool (herramienta):** una acción concreta que el agente puede ejecutar (buscar producto,
  crear pedido…).
- **Odoo:** el sistema de la empresa donde viven catálogo, clientes y pedidos.
- **Redis:** base de datos rápida para la memoria corta y el estado.
