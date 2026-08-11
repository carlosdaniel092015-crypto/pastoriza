"""Port línea por línea del scoring de `GestionarProductos` (JS -> Python).

Esta es la pieza MÁS delicada de la migración: está afinada contra el catálogo
real de Pastoriza. No la "mejores" hasta tener los tests en verde con casos
reales (ver tests/test_matching.py).
"""
from __future__ import annotations

import re
import unicodedata

STOP = {
    "de", "con", "la", "el", "los", "las", "un", "una", "y", "o", "a", "en",
    "del", "para", "que", "economica", "economico", "barata", "barato", "sea",
    "no", "mas", "menos", "liquido", "liquidos", "plastico", "plasticos",
    "envase", "tienen", "tiene",
}
TIPOS = [
    "botella", "botellas", "envase", "envases", "galon", "galones", "botellon",
    "botellones", "tarro", "tarros", "frasco", "frascos", "pomo", "pomos",
    "tapa", "tapas", "dispensador", "jarra", "vaso",
]
MODELO = [
    "mabi", "eco", "cilindrica", "cilindrico", "cuadrada", "cuadrado", "lisa",
    "liso", "natural", "pet", "primera", "segunda",
]
DIST = ["asa", "atomizador", "spray", "gotero", "dosificador", "pico", "rosca"]

_RE_UNIDAD = re.compile(
    r"(\d)\s*(oz|onza|onzas|galon|galones|gal|ml|litro|litros|l)\b"
)
_RE_NO_ALNUM = re.compile(r"[^a-z0-9/ ]")
_RE_ESPACIOS = re.compile(r"\s+")
_RE_CAPS = re.compile(
    r"(\d+(?:/\d+)?)\s*(oz|onza|onzas|galon|galones|gal|litro|litros|ml|cc|l)\b"
)
_RE_EXCLUIDAS = re.compile(
    r"\b(?:que\s+)?no\s+(?:sea\s+|es\s+)?(\w+)|(?:\bsin\s+)(\w+)"
)


def quitar_tildes(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s or "")
        if unicodedata.category(c) != "Mn"
    )


def norm(s: str) -> str:
    s = quitar_tildes(str(s or "")).lower()
    s = s.replace("rd$", " ")
    s = _RE_UNIDAD.sub(r"\1 \2", s)
    s = _RE_NO_ALNUM.sub(" ", s)
    s = re.sub(r"\bmedio\b", "1/2", s)
    s = re.sub(r"\bmedia\b", "1/2", s)
    return _RE_ESPACIOS.sub(" ", s).strip()


def caps(s: str) -> list[str]:
    """Extrae capacidades normalizadas: '8 oz', '1 galon', '1/2 galon'."""
    out: list[str] = []
    for num, uni in _RE_CAPS.findall(norm(s)):
        u = uni
        if u.startswith("galon") or u == "gal":
            u = "galon"
        if u in ("onza", "onzas"):
            u = "oz"
        out.append(f"{num} {u}")
    return out


def toks(s: str) -> list[str]:
    return [t for t in norm(s).split(" ") if t and t not in STOP]


def excluidas(busqueda: str) -> set[str]:
    """Detecta negaciones: 'que no sea cuadrada', 'sin tapa'."""
    b = quitar_tildes(str(busqueda or "")).lower()
    out: set[str] = set()
    for m1, m2 in _RE_EXCLUIDAS.findall(b):
        w = m1 or m2
        if w:
            out.add(w)
    return out


def score(busqueda: str, nombre: str) -> int:
    bt = toks(busqueda)
    nt = set(toks(nombre))
    bc = caps(busqueda)
    nc = set(caps(nombre))
    sc = 0

    for t in bt:
        if t in nt:
            if t in MODELO:
                sc += 5
            elif t in DIST:
                sc += 4
            elif t in TIPOS:
                sc += 1
            else:
                sc += 2

    for c in bc:
        if c in nc:
            sc += 8

    # Misma unidad pero distinta cantidad: pista débil de que es la familia correcta.
    for c in bc:
        partes = c.split(" ")
        if len(partes) < 2:
            continue
        u = partes[1]
        if any(d.endswith(" " + u) and d != c for d in nc):
            sc += 1

    for ex in excluidas(busqueda):
        if ex in nt:
            sc -= 10

    return sc


def es_busqueda_tipo_envase(busqueda: str) -> bool:
    """'botellas' o 'galones' a secas: búsqueda por tipo, no por producto concreto."""
    t = toks(busqueda)
    if not t:
        return False
    return any(x in TIPOS for x in t) and len(t) <= 2


# ------------------------------------------------- matching por foto ---------
def norm_capacidad(c: str) -> str:
    c = _RE_ESPACIOS.sub(" ", str(c or "").lower()).strip()
    if not c:
        return ""
    if "gal" in c:
        return "galon"
    m = re.search(r"(\d+(?:/\d+)?)\s*(oz|onza)", c)
    return f"{m.group(1)} oz" if m else ""


def _eq(a, b) -> bool:
    return bool(a) and bool(b) and str(a).lower() == str(b).lower()


def score_ficha(cliente: dict, producto: dict) -> int:
    """Compara la ficha visual de la foto del cliente contra la del catálogo."""
    s = 0
    if _eq(cliente.get("tipo"), producto.get("tipo")):
        s += 5
    if _eq(cliente.get("forma"), producto.get("forma")):
        s += 4
    if _eq(cliente.get("proporcion"), producto.get("proporcion")):
        s += 2
    if _eq(cliente.get("transparencia"), producto.get("transparencia")):
        s += 2
    if _eq(cliente.get("tapa"), producto.get("tapa")):
        s += 2
    if _eq(cliente.get("tapa_color"), producto.get("tapa_color")):
        s += 1
    cc = norm_capacidad(cliente.get("capacidad", ""))
    pc = norm_capacidad(producto.get("capacidad", ""))
    if cc and pc:
        s += 6 if cc == pc else -2
    return s
