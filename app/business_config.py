"""Configuración de NEGOCIO: la que edita el cliente desde el panel.

Vive en Redis bajo la misma key que ya usaba n8n (`pastoriza:config`), así que
el panel actual sigue funcionando sin cambios. Los defaults son idénticos a los
del nodo `Load Config3`.

POR CANAL: cada número de YCloud puede tener su propia configuración
(`pastoriza:config:c:<canal>`), que se superpone sobre la común. Lo que se cambia
dentro de un canal NO afecta al otro; para que aplique a los dos hay que pedirlo
explícitamente (`ambos=True`). Ver `app/canales.py`.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, fields

from app.canales import COMUN, canal_id, formatear, key_canal
from app.logging_conf import get_logger
from app.settings import settings

log = get_logger(__name__)

def get_redis():
    """Import perezoso: así `BusinessConfig` y el router se pueden testear
    sin tener redis instalado ni levantado."""
    from app.redis_client import get_redis as _r

    return _r()


CONFIG_KEY = "pastoriza:config"  # key literal, compartida con el panel/n8n

# Campos que NUNCA se separan por canal: definen QUÉ canales existen. Si cada canal
# guardara su propia lista, editar la lista desde un canal podría hacer desaparecer
# al otro del panel.
CAMPOS_COMUNES = ("canales",)


# ------------------------------------------------------------------ canales ---
# El bot atiende DOS números (canales de YCloud) y el panel se divide por canal.
# El canal de una conversación es el número NUESTRO por el que entró (el `emisor`).
# `norm_num` se mantiene como alias histórico de `canales.canal_id`.
norm_num = canal_id


def parsear_canales(texto: str) -> dict[str, str]:
    """"18099221092 = Ventas" (uno por línea o separados por coma) -> {num: nombre}."""
    out: dict[str, str] = {}
    for linea in re.split(r"[\n,;]+", str(texto or "")):
        if "=" not in linea:
            continue
        num, nombre = linea.split("=", 1)
        num, nombre = norm_num(num), nombre.strip()
        if num and nombre:
            out[num] = nombre[:40]
    return out


def nombre_canal(emisor: str, mapa: dict[str, str] | None = None) -> str:
    """Nombre para mostrar: el que puso el operador, o el número formateado."""
    n = norm_num(emisor)
    if not n:
        return "Sin canal"
    if mapa and n in mapa:
        return mapa[n]
    return formatear(n)
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
    # El bot NO da un pago por bueno: avisa que se está verificando. La confirmación
    # con el número de pedido la manda el panel cuando el supervisor aprueba.
    msg_comprobante: str = (
        "Recibi tu comprobante. Estamos verificando el pago y en un momento nuestro "
        "supervisor se comunica contigo para confirmarte. Gracias por tu paciencia."
    )
    # Cuando el comprobante es por MENOS que el total. {falta} = lo que falta, en RD$.
    # No se crea el pedido: se le dice cuánto falta, sin acusarlo de nada.
    msg_monto_corto: str = (
        "Recibi tu comprobante, pero el monto no cubre el total del pedido: faltan "
        "RD${falta}. Puedes enviarme el comprobante por el monto completo (o de la "
        "diferencia) y seguimos?"
    )
    # Lo que recibe el cliente CUANDO EL SUPERVISOR APRUEBA. {numero} = nº de pedido.
    msg_pago_aprobado: str = (
        "Tu pago fue verificado y aceptado. Tu pedido quedo registrado con el numero "
        "{numero}. Cualquier cosa, aqui estoy para ayudarte."
    )
    nota_envio: str = "Pago antes de las 2PM = entrega el mismo dia"
    info_envio: str = (
        "El pago es por adelantado (no es contra entrega). Enviamos con TRANSPORTE "
        "BLANCO. Costo de envio: RD$550. Gran Santo Domingo aprox 48 horas habiles; "
        "Interior minimo 3 dias habiles."
    )
    # Pago y mínimos
    monto_minimo: str = "1000"  # pedido mínimo en RD$
    # Mínimo de UNIDADES para envío a domicilio, por tamaño (editable en el panel).
    minimo_envio: str = (
        "4, 8 y 12 oz: desde 300 unidades. 16 oz: desde 200 unidades. Fardos: desde 1."
    )
    formas_pago: str = "Tarjetas de credito, transferencia, depositos y pago en efectivo"
    contra_entrega: str = "No"  # ¿se acepta pago contra entrega? No
    # Nombres de los canales (números de YCloud) para el panel. Formato:
    # "18099221092 = Tienda" (uno por línea o separados por coma). Sin nombre se
    # muestra el número formateado. Sólo afecta cómo se VE el panel.
    canales: str = "18099221092 = 809-922-1092\n18294716701 = 829-471-6701"
    # Venta por fardo (OPCIONAL: dejar vacío hasta que el cliente confirme los datos)
    fardo_cantidad: str = ""  # unidades por fardo
    fardo_envio_minimo: str = ""  # envío mínimo por fardo

    @property
    def precio_envio_num(self) -> float:
        try:
            return float(str(self.precio_envio).replace(",", ""))
        except ValueError:
            return 550.0


_cache: dict[str, tuple[float, BusinessConfig]] = {}
_ultima_buena: dict[str, BusinessConfig] = {}
_CACHE_TTL = 30.0  # segundos


def invalidar() -> None:
    """Tira el cache de TODOS los canales (un cambio en el común los afecta a todos)."""
    _cache.clear()


async def _leer_doc(key: str) -> dict | None:
    """Documento JSON de Redis. `{}` si no existe, None si Redis falló."""
    try:
        # with_reconnect: un blip de Redis reintenta en vez de degradar de una.
        from app.redis_client import with_reconnect

        raw = await with_reconnect(lambda r: r.get(key))
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("config_load_failed", key=key, error=str(exc))
        return None


async def _guardar_doc(key: str, data: dict) -> None:
    await get_redis().set(key, json.dumps(data, ensure_ascii=False))


async def load_config(canal: str = COMUN, force: bool = False) -> BusinessConfig:
    """Config EFECTIVA de un canal: la común + lo propio del canal encima.

    Sin `canal` devuelve la común (la que heredan los dos números).
    """
    c = canal_id(canal)
    now = time.monotonic()
    hit = _cache.get(c)
    if not force and hit and (now - hit[0]) < _CACHE_TTL:
        return hit[1]

    comun = await _leer_doc(CONFIG_KEY)
    propio: dict | None = await _leer_doc(key_canal(CONFIG_KEY, c)) if c else {}

    if comun is None or propio is None:
        # NO caer a los defaults hardcodeados: si el panel cambió precios, envío o
        # cuentas, cotizaríamos con valores VIEJOS y el cliente pagaría distinto.
        # Preferimos la última config buena; si no hay ninguna, ahí sí defaults.
        previa = _ultima_buena.get(c) or _ultima_buena.get(COMUN)
        if previa is not None:
            log.warning("config_usando_cache_previa", canal=c or "comun")
            return previa
        log.error("config_sin_redis_usando_defaults", canal=c or "comun")
        comun, propio = {}, {}

    data = dict(comun)
    # Los campos comunes (la lista de canales) no se pisan desde un canal.
    data.update({k: v for k, v in propio.items() if k not in CAMPOS_COMUNES})

    valid = {f.name for f in fields(BusinessConfig)}
    cfg = BusinessConfig(**{k: str(v) for k, v in data.items() if k in valid})
    _cache[c] = (now, cfg)
    _ultima_buena[c] = cfg
    return cfg


async def overrides_de_canal(canal: str) -> dict:
    """Qué campos tiene PROPIOS este canal (para que el panel lo muestre)."""
    c = canal_id(canal)
    if not c:
        return {}
    return await _leer_doc(key_canal(CONFIG_KEY, c)) or {}


async def overrides_por_canal() -> dict[str, list[str]]:
    """{canal: [campos propios]} de los canales que NO siguen del todo a la común.

    El panel lo usa para avisar, al editar la común, qué número no va a ver el cambio
    (porque tiene ese campo personalizado).
    """
    out: dict[str, list[str]] = {}
    for c in await canales_configurados():
        propios = await overrides_de_canal(c)
        if propios:
            out[c] = sorted(propios.keys())
    return out


async def canales_configurados() -> tuple[str, ...]:
    """Canales declarados en la config común (los números de YCloud del negocio)."""
    try:
        cfg = await load_config(COMUN)
        return tuple(parsear_canales(cfg.canales).keys())
    except Exception as exc:  # noqa: BLE001
        log.warning("canales_configurados_fallo", error=str(exc))
        return ()


async def save_config(
    data: dict, canal: str = COMUN, ambos: bool = False
) -> BusinessConfig:
    """Guarda la config. Por defecto (sin canal, o con `ambos`) aplica a los DOS.

    Con `canal` y sin `ambos` sólo se toca ese número: el otro sigue igual. Es lo
    que pidió la operación: "si realizo un cambio en el 6701 no se debe aplicar al
    1092 a menos que yo lo coloque en ambos".
    """
    c = canal_id(canal)
    if ambos or not c:
        # La lista de canales se PRESERVA si el que guarda no la manda (p.ej. el
        # endpoint viejo de n8n): perderla borraría las pestañas del panel.
        faltantes = [k for k in CAMPOS_COMUNES if k not in data]
        if faltantes:
            previo = await _leer_doc(CONFIG_KEY) or {}
            data = {**data, **{k: previo[k] for k in faltantes if k in previo}}
        await _guardar_doc(CONFIG_KEY, data)
        if ambos:
            # "Aplicar a los dos" es explícito: se borra lo propio de cada canal, si no
            # el que tuviera valores propios seguiría ignorando el cambio y el operador
            # creería que aplicó a ambos.
            for otro in await canales_configurados():
                try:
                    await get_redis().delete(key_canal(CONFIG_KEY, otro))
                except Exception as exc:  # noqa: BLE001
                    log.warning("config_canal_no_borrada", canal=otro, error=str(exc))
        # Sin `ambos` (guardar desde "Todos") se toca SÓLO la común: lo que un número
        # tenga personalizado se respeta. Borrarlo sería una pérdida silenciosa.
        invalidar()
        return await load_config(COMUN, force=True)

    # Sólo se guarda como PROPIO lo que de verdad difiere de la común. El panel manda
    # el formulario completo: si guardáramos los 26 campos, el canal dejaría de heredar
    # TODO (un cambio futuro en la común no le llegaría) y la marca de "campo propio"
    # no diría nada porque estarían todos marcados.
    comun = await _leer_doc(CONFIG_KEY)
    valid = {f.name for f in fields(BusinessConfig)}
    efectiva = asdict(
        BusinessConfig(**{k: str(v) for k, v in (comun or {}).items() if k in valid})
    )
    propio = {
        k: v for k, v in data.items()
        if k not in CAMPOS_COMUNES and str(v) != str(efectiva.get(k, ""))
    }
    await _guardar_doc(key_canal(CONFIG_KEY, c), propio)
    # La lista de canales es común: si vino en el formulario, va a la key común.
    comunes = {k: v for k, v in data.items() if k in CAMPOS_COMUNES}
    if comunes:
        base = await _leer_doc(CONFIG_KEY)
        base = dict(base or {})
        base.update(comunes)
        await _guardar_doc(CONFIG_KEY, base)
    invalidar()
    log.info("config_guardada_por_canal", canal=c, campos=len(propio))
    return await load_config(c, force=True)


async def resetear_canal(canal: str) -> BusinessConfig:
    """Vuelve un canal a heredar la config común (borra lo propio)."""
    c = canal_id(canal)
    if not c:
        raise ValueError("hay que indicar el canal")
    await get_redis().delete(key_canal(CONFIG_KEY, c))
    invalidar()
    log.info("config_canal_reseteada", canal=c)
    return await load_config(c, force=True)


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
    "overrides_de_canal",
    "overrides_por_canal",
    "canales_configurados",
    "resetear_canal",
    "invalidar",
    "norm_num",
    "parsear_canales",
    "nombre_canal",
    "get_producto_de_anuncio",
    "set_producto_de_anuncio",
    "listar_anuncios",
    "config_as_dict",
    "settings",
]
