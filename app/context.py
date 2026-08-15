"""Contexto que viaja con cada turno del agente.

CLAVE DEL DISEÑO: los campos de "efecto" (order_id, partner_id, productos
mostrados) los escriben LAS TOOLS, nunca el modelo. El modelo no puede
declarar que creó un pedido: si `order_id` es None, no hubo pedido y punto.
Esto es lo que hace innecesario el nodo `Clasificar Respuesta1` de n8n, que
cazaba con regex las confirmaciones falsas *después* de que ocurrían.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.business_config import BusinessConfig
from app.catalogo import Producto


@dataclass
class ConversationContext:
    # --- identidad ---
    chat_id: str
    telefono: str | None
    user_name: str
    emisor: str  # número/WABA desde el que respondemos
    destino: dict  # {"to": ...} o {"recipient": ...}
    cfg: BusinessConfig

    # --- contexto del anuncio (Click to WhatsApp) ---
    ad_id: str = ""
    ad_headline: str = ""
    ad_producto_tmpl_id: int | None = None
    ad_producto_nombre: str = ""
    ad_descripcion: str = ""  # qué muestra la IMAGEN del anuncio (visión), si no está mapeado

    # --- media del turno ---
    imagen_url: str = ""
    es_comprobante: bool = False

    # --- enrutado multi-agente (qué especialista atendió este turno) ---
    agente: str = ""
    # Visto bueno del DETERMINADOR (app/agents/enrutador.py) para pasar a un humano.
    # False = el bot debe resolverlo: `escalar_a_humano` queda bloqueada y también se
    # ignora un `escalar=True` que venga del modelo. Evita escalar saludos, "ok",
    # precios o envíos, que el bot sí puede responder.
    permite_escalar: bool = False
    motivo_determinador: str = ""

    # --- efectos escritos por las tools (fuente de verdad) ---
    order_id: int | None = None
    partner_id: int | None = None
    lineas_creadas: int = 0
    productos_ofrecidos: dict[int, Producto] = field(default_factory=dict)
    escalar: bool = False
    motivo_revision: list[str] = field(default_factory=list)
    # Lo que se le cotizó (lo escribe la tool `cotizar`, nunca el modelo). Alimenta el
    # semáforo de cierre del panel (app/score.py): una cotización por encima del
    # pedido mínimo es la señal de compra más honesta que hay.
    cotizado_unidades: int = 0
    cotizado_total: float = 0.0
    cotizado_modalidad: str = ""
    # Modalidad con la que se CREÓ el pedido ("envio" | "retiro"). Manda sobre la de la
    # cotización: el cliente puede cotizar envío y terminar retirando en tienda.
    pedido_modalidad: str = ""
    # Detalle del pedido, para el aviso que aprueba el supervisor. Lo escriben las
    # tools con lo que REALMENTE quedó en Odoo (nombre del producto del catálogo,
    # cantidad y precio ya corregido), no lo que el modelo haya dicho.
    lineas: list[dict] = field(default_factory=list)
    direccion_entrega: str = ""

    def marcar_revision(self, motivo: str) -> None:
        if motivo not in self.motivo_revision:
            self.motivo_revision.append(motivo)

    def ofrecer(self, productos: list[Producto]) -> None:
        for p in productos:
            self.productos_ofrecidos[p.tmpl_id] = p
