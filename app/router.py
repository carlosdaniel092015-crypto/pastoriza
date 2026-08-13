"""Fast-path determinista: preguntas frecuentes que NO necesitan el modelo.

Port del nodo `Router4`. Ahora es una función pura: entra texto + config, sale
una respuesta o None. Se testea con pytest en dos milisegundos, sin gastar un
token (ver tests/test_router.py).
"""
from __future__ import annotations

import random
import re

from app.business_config import BusinessConfig
from app.matching import quitar_tildes

SALUDOS = [
    "Hola! Soy Michelle de Pastoriza Plastics. En que te puedo ayudar hoy?",
    "Hola! Soy Michelle, de Pastoriza Plastics. Que envase o producto andas buscando?",
    "Hola! Soy Michelle de Pastoriza Plastics. Con gusto te ayudo, dime que necesitas.",
]
DESPEDIDAS = [
    "A ti! Cualquier cosa por aqui estoy.",
    "Con gusto! Que tengas un buen dia.",
    "Perfecto! Aqui andamos para lo que necesites.",
]

_R = lambda p: re.compile(p)  # noqa: E731

RE_DIRECCION_CLIENTE = [
    _R(r"\b(calle|sector|carretera|autopista|avenida|residencial|apartamento|apto|"
       r"edificio|manzana|ensanche|barrio|reparto|callejon|kilometro|urbanizacion|villa)\b"),
    _R(r"\b(av|mz|km)\b"),
    _R(r"(proximo a|cerca de|frente a|al lado de|detras de|entrando por)"),
    _R(r"#\s*\d"),
    _R(r"\b(santo domingo|distrito nacional|santiago|los alcarrizos|herrera|los mina|"
       r"boca chica|san cristobal|villa mella|los guaricanos|sabana perdida)\b"),
]
RE_QUEJA_A = _R(r"(ya (se la |le )?(he )?(di|dado|envie|mande|pase)|muchas veces|"
                r"otra vez|de nuevo|te la (di|envie|mande))")
RE_QUEJA_B = _R(r"(direccion|ubicacion|maps|localizacion)")
RE_ENVIO_UBIC_A = _R(r"\b(envie|mande|pase|te envie|le envie|le mande|le puse)\b")
RE_ENVIO_UBIC_B = _R(r"(ubicacion|direccion|maps|localizacion|pin)")
RE_YA_PAGO = _R(r"\bya (te |le )?(pague|transferi|deposite|hice la transferencia|"
                r"envie el (pago|comprobante)|mande el (pago|comprobante))\b")
RE_UBIC_WA = _R(r"ubicacion_whatsapp|ubicacion de whatsapp|compartio su ubicacion|maps\.google")

RE_ESTADO_PEDIDO = _R(
    r"\b(estado (de mi|del) pedido|donde (esta|va) mi pedido|cuando (llega|me llega|"
    r"entregan) mi pedido|ya (salio|despacharon|enviaron) mi pedido|pedido pendiente|"
    r"estado de (mi )?orden|donde (esta|va) mi orden|rastrear mi (pedido|orden)|"
    r"seguimiento de mi (pedido|orden)|tracking)\b"
)
RE_ENVIO_RETIRO = _R(r"^(envio o retiro|envio/retiro|envio y retiro)$")
RE_COTIZAR = _R(r"^(cotizar pedido|cotizar|quiero cotizar|hacer un pedido|hacer pedido)$")
RE_DIRECCION = _R(r"(donde (estan|estamos|queda|quedan|esta|es la tienda|es el negocio|"
                  r"los encuentro|recojo|retiro)|"
                  r"donde[^\n]{0,25}ubicad|(estan|estamos|esta) ubicad|como llego|"
                  r"ubicacion( de (la tienda|el negocio|ustedes))?|"
                  r"direccion de (la tienda|el negocio|ustedes)|cual es (su|la) direccion|"
                  r"donde (los )?ubic)")
RE_HORARIO = _R(r"\b(horario|a que hora (abren|cierran)|hasta que hora|"
                r"que hora (abren|cierran)|estan abiertos|dias que abren)\b")
RE_TELEFONO = _R(r"\b(telefono|numero de contacto|como los llamo|numero de telefono)\b")
RE_CUENTAS = _R(r"\b(cuenta bancaria|numero de cuenta|numero de cuentas|a que cuenta|"
                r"a cual cuenta|donde (deposito|transfiero|pago)|a que banco|"
                r"a cual banco|datos de pago|para (transferir|pagar))\b")
RE_ENVIO = _R(r"\b(cuanto (cuesta|es|vale) el envio|precio del envio|costo del envio|"
              r"hacen (envios|delivery)|como (funciona|es) el envio)\b")
RE_SALUDO = _R(r"^(hola+|hey|buenas|buenos dias|buenas tardes|buenas noches|que tal|"
               r"saludos|ola|klk|qlq|ke lo ke|epa|epale|dime|dimelo)[\s!.,]*$")

# Piezas de cortesía: si al quitarlas TODAS no queda nada con contenido, el mensaje
# es sólo un saludo. Necesario porque la ráfaga se combina en un solo texto
# ("Hola" + "Buenas tardes cómo estás" -> "hola\nbuenas tardes como estas") y el
# regex de arriba, anclado con ^...$, ya no matcheaba: el turno se iba al modelo,
# que llegó a escalar un SALUDO al supervisor.
_PIEZAS_SALUDO = _R(
    r"\b(hola+|hey+|hi|hello|buenas?|buenos?|dia|dias|tarde|tardes|noche|noches|"
    r"que|tal|saludos|ola|klk|qlq|ke|lo|epa|epale|como|estas|esta|andas|anda|"
    r"todo|bien|dime|dimelo|senorita|senora|senor|amigo|amiga|joven|disculpe|"
    r"por|favor|usted|ustedes|dios|bendiciones|feliz)\b"
)


def es_solo_saludo(norm: str) -> bool:
    """True si el texto es SÓLO saludo/cortesía (sin ninguna intención de compra)."""
    if not norm or len(norm) > 120:  # un saludo no es un párrafo
        return False
    resto = _PIEZAS_SALUDO.sub(" ", norm)
    resto = re.sub(r"[^a-z0-9]+", " ", resto)
    return not resto.strip()
RE_CIERRE = _R(r"^(gracias|muchas gracias|mil gracias|ok gracias|okay gracias|"
               r"listo gracias|perfecto gracias|hasta luego|nos vemos|chao|adios|bye|"
               r"eso es todo|nada mas)[\s!.,]*$")


def normalizar(texto: str) -> str:
    return quitar_tildes(str(texto or "")).strip().lower()


# Sustantivos de producto: si aparecen, la pregunta necesita el CATÁLOGO y no la
# puede contestar una respuesta enlatada. Ojo: "precio"/"costo" NO van acá, porque
# "cuanto cuesta el envio" sí lo resuelve el fast-path sin gastar tokens.
RE_PRODUCTO = _R(
    r"\b(botella|botellas|botellon|botellones|galon|galones|tarro|tarros|frasco|"
    r"frascos|pote|potes|pomo|pomos|envase|envases|tapa|tapas|atomizador|jarra|"
    r"vaso|vasos|onza|onzas|oz|catalogo|producto|productos)\b"
)

# Grupos de FAQ: una misma respuesta cubre varias preguntas (la de dirección ya trae
# horario y teléfono), así que se cuentan por GRUPO y no por regex.
def _grupos_faq(norm: str) -> set[str]:
    g: set[str] = set()
    # Exclusiva: "donde esta mi pedido" es estado del pedido, NO la dirección de la
    # tienda (aunque "donde esta" también matchee la regex de dirección).
    if RE_ESTADO_PEDIDO.search(norm):
        return {"estado"}
    if RE_DIRECCION.search(norm) or RE_HORARIO.search(norm) or RE_TELEFONO.search(norm):
        g.add("tienda")
    if RE_ENVIO_RETIRO.match(norm) or RE_ENVIO.search(norm):
        g.add("envio")
    if RE_CUENTAS.search(norm):
        g.add("pago")
    return g


def _multi_intencion(norm: str) -> bool:
    """True si el mensaje trae más de una cosa que responder."""
    grupos = _grupos_faq(norm)
    if not grupos:
        return False
    if len(grupos) > 1:
        return True
    # Una FAQ + una pregunta de producto ("precio botellas") -> hace falta el catálogo.
    return bool(RE_PRODUCTO.search(norm))


def respuesta_directa(
    texto: str,
    cfg: BusinessConfig,
    content_type: str = "text",
    viene_de_anuncio: bool = False,
) -> str | None:
    """Devuelve la respuesta si es una consulta resoluble sin el modelo, o None.

    None significa: "esto va al agente".
    """
    if content_type != "text":
        return None

    norm = normalizar(texto)
    if not norm:
        return None

    # Nota: si viene de un anuncio, el SALUDO genérico lo maneja el agente (más abajo),
    # pero las FAQ de negocio (dirección, horario, cuentas, envío…) SÍ se responden acá.

    # Cualquier cosa que huela a dirección DEL CLIENTE, queja, ubicación o pago -> agente.
    if any(r.search(norm) for r in RE_DIRECCION_CLIENTE):
        return None
    if RE_QUEJA_A.search(norm) and RE_QUEJA_B.search(norm):
        return None
    if RE_ENVIO_UBIC_A.search(norm) and RE_ENVIO_UBIC_B.search(norm):
        return None
    if RE_YA_PAGO.search(norm) or RE_UBIC_WA.search(norm):
        return None

    # MULTI-INTENCIÓN: el fast-path devuelve UNA sola respuesta enlatada, así que si
    # el cliente preguntó DOS cosas en la misma ráfaga sólo contestaba la primera que
    # matcheara y la otra se perdía en silencio (caso real: "¿Precio botellas? / Donde
    # están ubicado" -> respondió sólo la dirección). Si hay más de una intención, o
    # una FAQ + una pregunta de producto (que exige el catálogo), lo atiende el agente,
    # que tiene TODOS estos datos en su prompt y responde las dos cosas en UN mensaje.
    if _multi_intencion(norm):
        return None

    if RE_ESTADO_PEDIDO.search(norm):
        return (
            "Hola, veo que tienes un pedido con nosotros. Para darte el estado exacto "
            "y coordinar la entrega, comunicate con nuestro equipo al +1 (829) 471-6701; "
            "ellos manejan esa parte y te ayudan enseguida."
        )

    if RE_ENVIO_RETIRO.match(norm) or RE_ENVIO.search(norm):
        envio = (
            f"Envio a domicilio\n"
            f"Costo: RD${cfg.precio_envio}. Dias: {cfg.dias_envio}. {cfg.nota_envio}."
        )
        if getattr(cfg, "minimo_envio", ""):
            envio += f"\nMinimo para envio: {cfg.minimo_envio}"
        return (
            f"{envio}\n\n"
            f"Retiro en tienda (gratis)\n{cfg.direccion}\n"
            f"Horario: {cfg.horario_tienda}\n\nCual prefieres?"
        )

    if RE_CUENTAS.search(norm):
        return (
            f"Formas de pago: {cfg.formas_pago}.\n"
            f"Pedido minimo: RD${cfg.monto_minimo}.\n\n"
            f"Para transferencia o deposito:\n"
            f"{cfg.banco1_nombre} - Cta {cfg.banco1_cuenta}\n"
            f"{cfg.banco2_nombre} - Cta {cfg.banco2_cuenta}\n"
            f"({cfg.titular}, {cfg.cedula})\n\n"
            "Cuando pagues, enviame la foto del comprobante y te confirmo el pedido."
        )

    if RE_DIRECCION.search(norm):
        base = (
            f"Estamos en {cfg.direccion}. Horario: {cfg.horario_tienda}. "
            f"Tel: {cfg.telefono}."
        )
        if cfg.maps_url:
            return f"{base}\n\nAqui te llega directo por Google Maps:\n{cfg.maps_url}"
        return f"{base} Si buscas en Google Maps te lleva directo."

    if RE_HORARIO.search(norm):
        return f"Nuestro horario es {cfg.horario_tienda}. Estamos en {cfg.direccion}."

    if RE_TELEFONO.search(norm):
        return f"Nuestro telefono es {cfg.telefono}. Tambien puedes escribirnos por aqui."

    if RE_COTIZAR.match(norm):
        return (
            "Con gusto! Que envase o producto te interesa? Dime el nombre y te muestro "
            "las opciones. Si prefieres, te muestro todo el catalogo."
        )

    if RE_SALUDO.match(norm) or es_solo_saludo(norm):
        # Desde un anuncio, el saludo lo da el agente (sabe de qué producto viene).
        return None if viene_de_anuncio else random.choice(SALUDOS)

    if RE_CIERRE.match(norm):
        return random.choice(DESPEDIDAS)

    return None
