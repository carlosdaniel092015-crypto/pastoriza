"""Aviso de APROBACIÓN al supervisor: el resumen del pedido que él aprueba por WhatsApp.

El bot no da un pago por bueno. Cuando llega un comprobante crea el pedido y le manda
al supervisor una PLANTILLA con la foto del comprobante, el cliente, la dirección, los
productos y los montos, con dos botones: aprobar o no. Recién cuando toca "aprobar",
el cliente recibe la confirmación con el número de pedido.

Este módulo es la parte PURA (armar el texto y los montos) más el parseo de la
respuesta del botón. Sin Redis, sin HTTP: por eso se puede testear entera.

Cuidado con los límites de WhatsApp, que son los que mandan en el diseño:
  - una variable de plantilla NO puede tener saltos de línea ni tabs, así que la
    lista de productos va en UNA línea separada por " · ";
  - el cuerpo de la plantilla tiene tope de 1024 caracteres;
  - el payload de un botón tiene tope de 128.
"""
from __future__ import annotations

import re

ITBIS = 1.18  # los precios del catálogo YA vienen con ITBIS incluido

# Prefijos del payload de los botones (lo que vuelve cuando el supervisor toca).
ACCION_APROBAR = "aprobar"
ACCION_RECHAZAR = "rechazar"

# También se acepta que el supervisor escriba la orden a mano, por si la plantilla
# todavía no está aprobada por Meta o el botón no devuelve payload.
RE_TEXTO = re.compile(
    r"^\s*(aprobar|aprobado|apruebo|ok|si|s[ií]|rechazar|rechazado|no)\b[^\d]*(\d{1,9})?",
    re.IGNORECASE,
)
_APRUEBAN = {"aprobar", "aprobado", "apruebo", "ok", "si", "sí"}

# Topes de cada variable. El CUERPO ENTERO de la plantilla (texto fijo + variables)
# no puede pasar de 1024 caracteres o WhatsApp rechaza el envío — y ahí el supervisor
# no se entera de un pago. El texto fijo de PLANTILLA_META.md son 247 caracteres, así
# que estos topes dejan el peor caso en ~890. Si cambiás el texto de la plantilla,
# rehacé esta cuenta (hay un test que la vigila).
MAX_CLIENTE = 100
MAX_DIRECCION = 200
MAX_PRODUCTOS = 260


def una_linea(texto: str, limite: int = 300) -> str:
    """Una variable de plantilla no admite saltos de línea ni tabs (Meta la rechaza)."""
    limpio = re.sub(r"[\r\n\t]+", " · ", str(texto or "")).strip(" ·")
    limpio = re.sub(r"\s{2,}", " ", limpio)
    return (limpio[: limite - 1] + "…") if len(limpio) > limite else (limpio or "-")


def montos(lineas: list[dict] | None, envio: float = 0.0) -> dict:
    """Subtotal, ITBIS, envío y total a partir de las líneas REALES del pedido.

    Los precios del catálogo ya incluyen ITBIS, así que el subtotal se desagrega
    hacia atrás — igual que `cotizar_tools.calcular`, para que al supervisor le dé
    exactamente lo mismo que se le cotizó al cliente.
    """
    productos = round(sum(float(x.get("total") or 0) for x in (lineas or [])), 2)
    subtotal = round(productos / ITBIS + 1e-9, 2)
    return {
        "productos": productos,
        "subtotal": subtotal,
        "itbis": round(productos - subtotal, 2),
        "envio": round(float(envio or 0), 2),
        "total": round(productos + float(envio or 0), 2),
    }


def texto_productos(lineas: list[dict] | None) -> str:
    """'300 x BOTELLA LISA 8 OZ · 100 x TAPA 28MM' — en una sola línea."""
    partes = [
        f"{int(x.get('cantidad') or 0)} x {str(x.get('nombre') or '').strip()}"
        for x in (lineas or [])
        if x.get("nombre")
    ]
    return una_linea(" · ".join(partes), MAX_PRODUCTOS) if partes else (
        "(sin líneas cargadas)"
    )


def rd(monto: float) -> str:
    return f"{monto:,.2f}"


def parametros(
    *,
    order_id: int,
    modalidad: str,
    cliente: str,
    telefono: str,
    direccion: str,
    lineas: list[dict] | None,
    envio: float = 0.0,
) -> list[str]:
    """Las 9 variables de la plantilla, en orden. Ver PLANTILLA_META.md."""
    es_envio = str(modalidad).lower().startswith("env")
    # En RETIRO no se cobra envío, aunque el cliente haya cotizado con envío antes:
    # si el total lo incluyera, el supervisor aprobaría un monto que nadie pagó.
    m = montos(lineas, envio if es_envio else 0.0)
    return [
        str(order_id),                                    # {{1}} número de pedido
        "ENVÍO A DOMICILIO" if es_envio else "RETIRO EN TIENDA",  # {{2}}
        una_linea(f"{cliente or 'Sin nombre'} ({telefono or '-'})", MAX_CLIENTE),  # {{3}}
        una_linea(
            direccion or ("Retiro en tienda" if not es_envio else "-"), MAX_DIRECCION
        ),  # {{4}}
        texto_productos(lineas),                          # {{5}} productos
        rd(m["subtotal"]),                                # {{6}} subtotal sin ITBIS
        rd(m["itbis"]),                                   # {{7}} ITBIS
        rd(m["envio"]),                                   # {{8}} envío
        rd(m["total"]),                                   # {{9}} TOTAL
    ]


def payload(accion: str, chat_id: str, order_id: int) -> str:
    """Lo que viaja en el botón y vuelve cuando el supervisor lo toca (tope 128)."""
    return f"{accion}:{chat_id}:{order_id}"[:128]


def parsear_respuesta(texto: str) -> tuple[str, str, int] | None:
    """(accion, chat_id, order_id) de la respuesta del supervisor.

    Acepta el payload del botón ("aprobar:1809...:160") y también que lo escriba a
    mano ("aprobar 160" / "ok 160"), por si el botón no devuelve payload.
    Devuelve None si no es una respuesta de aprobación.
    """
    t = str(texto or "").strip()
    if not t:
        return None

    partes = t.split(":")
    if len(partes) == 3 and partes[0].lower() in (ACCION_APROBAR, ACCION_RECHAZAR):
        try:
            return (partes[0].lower(), partes[1].strip(), int(partes[2]))
        except ValueError:
            return None

    m = RE_TEXTO.match(t)
    if m and m.group(2):
        palabra = m.group(1).lower()
        accion = ACCION_APROBAR if palabra in _APRUEBAN else ACCION_RECHAZAR
        return (accion, "", int(m.group(2)))
    return None


__all__ = [
    "ACCION_APROBAR",
    "ACCION_RECHAZAR",
    "montos",
    "parametros",
    "parsear_respuesta",
    "payload",
    "texto_productos",
    "una_linea",
]
