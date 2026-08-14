"""Semáforo de cierre: qué tan cerca está una conversación de convertirse en pedido.

PARA QUÉ SIRVE: ordenar a quién atiende primero una PERSONA del equipo.
PARA QUÉ NO SIRVE, y es una decisión de diseño, no un olvido:

  - El bot atiende IGUAL a todos. Esto no se inyecta en ningún prompt (si el modelo
    no lo ve, no se lo puede filtrar a un cliente) y no cambia ni una respuesta.
  - No existe puntaje NEGATIVO ni etiqueta de "no compra". Pedir la lista completa,
    preguntar por millar, regatear o no dar cantidad es la apertura del MAYORISTA:
    penalizarlo sería castigar al cliente más valioso.
  - No se mide NUNCA cómo escribe el cliente (ortografía, faltas, tildes, largo del
    mensaje, audio vs texto, cuántas preguntas hace). En RD eso es un proxy de clase
    social, y `prompts/base_comun.md` ya ordena lo contrario de forma explícita.
  - "Sin señales" (gris) NO es "malo": es que todavía no pasó nada medible. Un chat
    nuevo arranca en gris.

Sólo suma HECHOS que escribió el código o una tool (mismo invariante que el resto del
proyecto: los efectos nunca los declara el modelo). Es una función PURA: no toca
Redis, ni Odoo, ni OpenAI, y por eso se puede testear entera.

Los hitos son ACUMULATIVOS por conversación: si el cliente pidió las cuentas el
martes, eso sigue siendo cierto el viernes.
"""
from __future__ import annotations

from app.router import (
    RE_CUENTAS,
    RE_DIRECCION_CLIENTE,
    RE_UBIC_WA,
    RE_YA_PAGO,
    normalizar,
)

# hito -> (peso, etiqueta que ve el operador). La etiqueta habla de lo que HIZO el
# cliente, nunca de cómo es: cualquiera de estos textos tiene que poder aparecer en
# una captura de pantalla sin que dé vergüenza.
PESOS: dict[str, tuple[int, str]] = {
    "comprobante": (50, "Mandó comprobante"),
    "pedido": (50, "Pedido creado"),
    "dijo_pago": (45, "Dice que ya pagó"),
    "pidio_cuentas": (30, "Pidió las cuentas"),
    "cotizo": (15, "Cotizó"),
    "sobre_minimo": (10, "Cotizó sobre el pedido mínimo"),
    "eligio_entrega": (8, "Eligió envío o retiro"),
    "dio_direccion": (12, "Dio dirección"),
    "dio_ubicacion": (12, "Compartió su ubicación"),
    "lineas": (10, "Productos cargados al pedido"),
    "contacto": (10, "Registrado en Odoo"),
}

UMBRAL_VERDE = 40
UMBRAL_AMARILLO = 15

# Orden de atención (mayor = llamar antes). El pedido ya CERRADO no necesita llamada,
# así que baja: lo urgente es el que está a punto de cerrar y todavía no cerró.
PRIORIDAD = {"verde": 3, "amarillo": 2, "gris": 1, "cerrado": 0}


def detectar(
    texto: str,
    *,
    es_comprobante: bool = False,
    order_id: int | None = None,
    partner_id: int | None = None,
    lineas_creadas: int = 0,
    cotizado_unidades: int = 0,
    cotizado_total: float = 0.0,
    cotizado_modalidad: str = "",
    monto_minimo: float = 0.0,
) -> set[str]:
    """Hitos alcanzados EN ESTE TURNO. Todo dato viene de una tool o del código."""
    n = normalizar(texto or "")
    hitos: set[str] = set()

    if n:
        if RE_CUENTAS.search(n):
            hitos.add("pidio_cuentas")
        if RE_YA_PAGO.search(n):
            hitos.add("dijo_pago")
        if RE_UBIC_WA.search(n):
            hitos.add("dio_ubicacion")
        # Dirección: las mismas regex con las que el fast-path decide desviar el turno
        # al agente (no se duplica criterio).
        if any(r.search(n) for r in RE_DIRECCION_CLIENTE):
            hitos.add("dio_direccion")

    if es_comprobante:
        hitos.add("comprobante")
    if order_id:
        hitos.add("pedido")
    if lineas_creadas:
        hitos.add("lineas")
    if partner_id:
        hitos.add("contacto")
    if cotizado_unidades > 0:
        hitos.add("cotizo")
        if monto_minimo > 0 and cotizado_total >= monto_minimo:
            hitos.add("sobre_minimo")
    if cotizado_modalidad in ("envio", "retiro"):
        hitos.add("eligio_entrega")
    return hitos


# --------------------------------------------------- desde el historial ---
# Las conversaciones que ya existían no tienen semáforo: se calculó siempre al cerrar
# un turno. Esto lo reconstruye leyendo el historial que ya está en Redis, para no
# tener que esperar a que cada cliente vuelva a escribir. Se hace UNA vez por chat (el
# resultado se guarda en el chatmeta), a pedido del operador desde el panel.
#
# Se leen SOLO salidas de tools (texto que escribió el código, no el modelo) y los
# mensajes del cliente. Lo que el modelo redactó se ignora a propósito: si dijo "tu
# pedido quedó registrado" sin que existiera, no debe contar como hito.
_MARCAS_TOOL = {
    "contacto": ("EXISTE: partner_id=", "OK: contacto creado, partner_id=", "OK: contacto "),
    "pedido": ("OK: pedido creado con número",),
    "lineas": ("agregado al pedido",),
    "cotizo": ("COTIZACION (para mostrar al cliente):",),
}


def _texto_de(item: dict) -> str:
    contenido = item.get("content") if isinstance(item, dict) else None
    if isinstance(contenido, list):
        contenido = " ".join(
            str((x or {}).get("text") or (x or {}).get("content") or "") for x in contenido
        )
    if contenido is None:
        # Salida de una tool (function_call_output): es lo más confiable que hay.
        contenido = item.get("output") if isinstance(item, dict) else None
    return str(contenido or "")


def desde_historial(items: list[dict] | None) -> tuple[set[str], float]:
    """(hitos, total cotizado) deducibles del historial de una conversación."""
    from app.media import es_comprobante_de

    hitos: set[str] = set()
    total_cotizado = 0.0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        texto = _texto_de(item)
        if not texto:
            continue
        rol = str(item.get("role") or "")
        tipo = str(item.get("type") or "")

        # 1. Lo que dijo el CLIENTE (y el bloque de análisis de imagen del turno).
        if rol == "user":
            hitos |= detectar(texto)
            if es_comprobante_de(texto):
                hitos.add("comprobante")
            continue

        # 2. Salidas de TOOLS. Sólo estas: lo que redactó el modelo no cuenta.
        if tipo.startswith("function_call") or rol == "tool":
            for hito, marcas in _MARCAS_TOOL.items():
                if any(m in texto for m in marcas):
                    hitos.add(hito)
            if "COTIZACION (para mostrar al cliente):" in texto:
                for linea in texto.splitlines():
                    if linea.startswith("TOTAL"):
                        try:
                            total_cotizado = max(
                                total_cotizado,
                                float(linea.split("RD$")[1].replace(",", "").strip()),
                            )
                        except (IndexError, ValueError):
                            pass
                if "Retiro en tienda" in texto or "Envio: RD$" in texto:
                    hitos.add("eligio_entrega")
    return hitos, total_cotizado


def reconstruir(items: list[dict] | None, monto_minimo: float = 0.0) -> dict:
    """Semáforo de una conversación ya existente, a partir de su historial."""
    hitos, total = desde_historial(items)
    if total and monto_minimo > 0 and total >= monto_minimo:
        hitos.add("sobre_minimo")
    return puntuar([], hitos)


def puntuar(previos: list[str] | None, nuevos: set[str] | None = None) -> dict:
    """Score acumulado de la conversación. Devuelve lo que se guarda en el chatmeta.

    `previos`: hitos que ya venían de turnos anteriores (se conservan).
    """
    validos = set(PESOS)
    hitos = sorted(
        (set(previos or []) | set(nuevos or [])) & validos,
        key=lambda h: (-PESOS[h][0], h),
    )
    score = min(100, sum(PESOS[h][0] for h in hitos))

    if "pedido" in hitos and ("comprobante" in hitos or "dijo_pago" in hitos):
        sem = "cerrado"  # ya pagó y el pedido existe: no hay a quién llamar
    elif score >= UMBRAL_VERDE:
        sem = "verde"
    elif score >= UMBRAL_AMARILLO:
        sem = "amarillo"
    else:
        sem = "gris"
    return {"score": score, "sem": sem, "hitos": hitos}


def etiquetas(hitos: list[str] | None) -> list[str]:
    """Los hitos en texto, para mostrar el POR QUÉ del semáforo (nunca sólo el número:
    un número sin explicación se convierte en un juicio que nadie puede discutir)."""
    return [PESOS[h][1] for h in (hitos or []) if h in PESOS]


__all__ = [
    "PESOS",
    "desde_historial",
    "reconstruir",
    "PRIORIDAD",
    "UMBRAL_AMARILLO",
    "UMBRAL_VERDE",
    "detectar",
    "etiquetas",
    "puntuar",
]
