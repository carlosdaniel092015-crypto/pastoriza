"""Configuración de NEGOCIO: la que edita el cliente desde el panel.

Vive en Redis bajo la misma key que ya usaba n8n (`pastoriza:config`), así que
el panel actual sigue funcionando sin cambios. Los defaults son idénticos a los
del nodo `Load Config3`.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, fields

from app.logging_conf import get_logger
from app.settings import settings

log = get_logger(__name__)

def get_redis():
    """Import perezoso: así `BusinessConfig` y el router se pueden testear
    sin tener redis instalado ni levantado."""
    from app.redis_client import get_redis as _r

    return _r()


CONFIG_KEY = "pastoriza:config"  # key literal, compartida con el panel/n8n
ADS_MAP_KEY = "pastoriza:ads_map"  # hash: ad_id -> JSON {product_tmpl_id, nombre}


@dataclass
class BusinessConfig:
    precio_envio: str = "550"
    dias_envio: str = "Lunes a Viernes"
    hora_corte: str = "2PM"
    banco1_nombre: str = "Banco Popular"
    banco1_cuenta: str = "787919679"
    banco2_nombre: str = "Banco Reservas"
    banco2_cuenta: str = "9602738582"
    titular: str = "PASTORIZA PLASTICS SRL"
    cedula: str = "402-2107637-1"
    direccion: str = "Isabel Aguiar #240, Nave #1, Herrera"
    horario_tienda: str = "L-V 8AM-5PM, S 8AM-12PM"
    telefono: str = "809-922-1092"
    website: str = "pastorizaplastic.net/shop"
    maps_url: str = "https://maps.app.goo.gl/6eJidgQKXkvdqFDW6"
    nota_botellon: str = "Los botellones NO incluyen tapa"
    nota_stock: str = "Tenemos disponibilidad para entrega inmediata."
    msg_escalar: str = "Enseguida uno de nuestros asesores se comunicara contigo."
    msg_comprobante: str = (
        "Recibi tu comprobante. Tu pedido fue registrado y esta siendo procesado."
    )
    nota_envio: str = "Pago antes de las 2PM = entrega el mismo dia"
    info_envio: str = (
        "El pago es por adelantado (no es contra entrega). Enviamos con TRANSPORTE "
        "BLANCO. Costo de envio: RD$550. Gran Santo Domingo aprox 48 horas habiles; "
        "Interior minimo 3 dias habiles."
    )
    # Pago y mínimos
    monto_minimo: str = "1000"  # pedido mínimo en RD$
    formas_pago: str = "Tarjetas de credito, transferencia, depositos y pago en efectivo"
    contra_entrega: str = "No"  # ¿se acepta pago contra entrega? No
    # Venta por fardo (OPCIONAL: dejar vacío hasta que el cliente confirme los datos)
    fardo_cantidad: str = ""  # unidades por fardo
    fardo_envio_minimo: str = ""  # envío mínimo por fardo

    @property
    def precio_envio_num(self) -> float:
        try:
            return float(str(self.precio_envio).replace(",", ""))
        except ValueError:
            return 550.0


_cache: tuple[float, BusinessConfig] | None = None
_CACHE_TTL = 30.0  # segundos


async def load_config(force: bool = False) -> BusinessConfig:
    global _cache
    now = time.monotonic()
    if not force and _cache and (now - _cache[0]) < _CACHE_TTL:
        return _cache[1]

    data: dict = {}
    try:
        raw = await get_redis().get(CONFIG_KEY)
        if raw:
            data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("config_load_failed", error=str(exc))

    valid = {f.name for f in fields(BusinessConfig)}
    cfg = BusinessConfig(**{k: str(v) for k, v in data.items() if k in valid})
    _cache = (now, cfg)
    return cfg


async def save_config(data: dict) -> BusinessConfig:
    global _cache
    await get_redis().set(CONFIG_KEY, json.dumps(data, ensure_ascii=False))
    _cache = None
    return await load_config(force=True)


# ---------------------------------------------------------------- anuncios ---
async def get_producto_de_anuncio(ad_id: str) -> dict | None:
    """Mapa ad_id -> producto. El referral de Meta NO trae SKU: este mapa lo suple.

    Se llena desde el panel (o con scripts/mapear_anuncios.py).
    """
    if not ad_id:
        return None
    try:
        raw = await get_redis().hget(ADS_MAP_KEY, str(ad_id))
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001
        log.warning("ads_map_read_failed", ad_id=ad_id, error=str(exc))
        return None


async def set_producto_de_anuncio(ad_id: str, product_tmpl_id: int, nombre: str) -> None:
    await get_redis().hset(
        ADS_MAP_KEY,
        str(ad_id),
        json.dumps({"product_tmpl_id": product_tmpl_id, "nombre": nombre}),
    )


async def listar_anuncios() -> dict[str, dict]:
    raw = await get_redis().hgetall(ADS_MAP_KEY)
    out: dict[str, dict] = {}
    for k, v in (raw or {}).items():
        try:
            out[k] = json.loads(v)
        except Exception:  # noqa: BLE001
            continue
    return out


def config_as_dict(cfg: BusinessConfig) -> dict:
    return asdict(cfg)


__all__ = [
    "BusinessConfig",
    "load_config",
    "save_config",
    "get_producto_de_anuncio",
    "set_producto_de_anuncio",
    "listar_anuncios",
    "config_as_dict",
    "settings",
]
