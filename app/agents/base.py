"""Base común de los agentes especialistas.

Cada especialista se construye igual: mismas garantías (structured output
`RespuestaBot`, temperatura, sin tool-calls en paralelo) y el mismo armado de
instrucciones = base_comun + prompt del agente + conocimiento + datos dinámicos.
Lo único que cambia entre agentes es su prompt (.md) y sus herramientas.
"""
from __future__ import annotations

from agents import Agent, ModelSettings, RunContextWrapper
from pydantic import BaseModel, Field

from app.context import ConversationContext


class RespuestaBot(BaseModel):
    """Lo único que el modelo puede producir."""

    mensaje: str = Field(
        description="Texto para el cliente, en español dominicano, 1-3 líneas por párrafo."
    )
    mostrar_productos: list[int] = Field(
        default_factory=list,
        description=(
            "ids de productos cuya foto hay que enviar. SOLO ids que devolvió una "
            "tool en este turno. Máximo 5. Lista vacía si no hay que mostrar fotos."
        ),
    )
    escalar: bool = Field(
        default=False,
        description="true si esta conversación necesita que la siga una persona.",
    )


def _bloque_dinamico(c: ConversationContext) -> str:
    """Datos que dependen de la config/estado del turno (no van en los .md)."""
    cfg = c.cfg
    partes = [
        f"""# DATOS DE LA TIENDA
Dirección: {cfg.direccion}
Horario: {cfg.horario_tienda}
Teléfono: {cfg.telefono}
Tienda online: https://{cfg.website}
Mapa (sólo si lo piden): {cfg.maps_url}
Envío: {cfg.info_envio}
Nota de envío: {cfg.nota_envio}
{cfg.nota_botellon}.
Si no tienes un dato: "Para esa información contáctanos al +1 829 471-6701."

# CUENTAS PARA TRANSFERENCIA (mostrarlas sólo tras confirmar el pedido)
{cfg.banco1_nombre} - Cta {cfg.banco1_cuenta}
{cfg.banco2_nombre} - Cta {cfg.banco2_cuenta}
({cfg.titular}, {cfg.cedula})"""
    ]

    if c.ad_id:
        bloque = [
            "# EL CLIENTE VIENE DE UN ANUNCIO",
            "Ya sabes de dónde viene: NO le preguntes '¿en qué te puedo ayudar?' "
            "como si no supieras nada.",
        ]
        if c.ad_headline:
            bloque.append(f'Titular del anuncio: "{c.ad_headline}"')
        if c.ad_producto_nombre:
            bloque.append(
                f"Producto del anuncio: {c.ad_producto_nombre} "
                f"(id={c.ad_producto_tmpl_id}). Arranca por ahí: confírmalo por su "
                "nombre y muestra su foto."
            )
        else:
            bloque.append(
                "No hay producto mapeado a este anuncio. Pregunta de forma natural "
                "qué envase vio en el anuncio."
            )
        partes.append("\n".join(bloque))

    if not c.telefono:
        partes.append(
            "# SIN TELÉFONO\nEste cliente escribió desde un anuncio y no tenemos su "
            "número real. Antes de crear el pedido, pídele el teléfono para coordinar "
            "la entrega."
        )

    if c.es_comprobante:
        partes.append(
            "# COMPROBANTE DETECTADO\nEl sistema validó que la imagen de este turno "
            "es un comprobante de pago. Ejecuta EN ORDEN: verificar_contacto -> "
            "(crear_contacto si no existe) -> crear_pedido -> agregar_linea_pedido. "
            "Sólo después confírmale al cliente con el número real de pedido."
        )

    partes.append(
        f"# CLIENTE\nNombre en WhatsApp: {c.user_name or 'desconocido'}\n"
        f"Teléfono: {c.telefono or 'no disponible (vino de anuncio)'}"
    )
    return "\n\n".join(partes)


def armar_instrucciones(nombre: str):
    """Devuelve el callable de instrucciones para el agente `nombre` (síncrono)."""

    def _instr(ctx: RunContextWrapper[ConversationContext], agent: Agent) -> str:
        # Import perezoso para evitar ciclos con el panel.
        from app.panel.conocimiento import get_bloque_inyeccion
        from app.panel.prompt_store import get_prompt

        partes = [get_prompt("base_comun"), get_prompt(nombre)]
        conocimiento = get_bloque_inyeccion()
        if conocimiento:
            partes.append(conocimiento)
        partes.append(_bloque_dinamico(ctx.context))
        return "\n\n".join(p for p in partes if p)

    return _instr


def crear_especialista(nombre: str, tools: list, model: str) -> Agent[ConversationContext]:
    return Agent[ConversationContext](
        name=f"pastoriza-{nombre}",
        instructions=armar_instrucciones(nombre),
        tools=tools,
        model=model,
        output_type=RespuestaBot,
        model_settings=ModelSettings(temperature=0.4, parallel_tool_calls=False),
    )
