"""Catálogo: lectura de product.template desde Odoo + búsqueda.

Cambio de diseño respecto de n8n: acá NO se devuelven strings `<IMG>...</IMG>`
para que el modelo los copie verbatim. Se devuelven objetos `Producto`, y las
URLs de imagen las construye el servicio al enviar. El modelo pierde la
posibilidad de inventar una URL o reusar la de un turno anterior.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.logging_conf import get_logger
from app.matching import es_busqueda_tipo_envase, score
from app.odoo import odoo
from app.settings import settings

log = get_logger(__name__)

EXCLUIR_NOMBRES = {"BIENES", "SERVICIO"}
IMG_BASE = f"{settings.odoo_url.rstrip('/')}/web/image/product.template"
SHOP_BASE = f"{settings.odoo_url.rstrip('/')}/shop"


@dataclass(frozen=True)
class Producto:
    tmpl_id: int
    variant_id: int
    nombre: str
    precio_con_itbis: float
    website_slug: str = ""

    @property
    def image_url(self) -> str:
        return f"{IMG_BASE}/{self.tmpl_id}/image_1024"

    @property
    def shop_url(self) -> str:
        return f"{SHOP_BASE}/{self.website_slug}" if self.website_slug else ""

    def resumen(self) -> str:
        return f"{self.nombre} - RD${self.precio_con_itbis:.2f}"


def con_itbis(precio: float | int | None) -> float:
    v = float(precio or 0)
    if not settings.precios_guardados_con_itbis:
        v = v * (1 + settings.itbis_rate)
    return round(v, 2)


def sin_itbis(precio: float | int | None) -> float:
    v = float(precio or 0)
    if settings.precios_guardados_con_itbis:
        v = v / (1 + settings.itbis_rate)
    return round(v, 2)


class Catalogo:
    def __init__(self) -> None:
        self._cache: list[Producto] = []
        self._cache_ts: float = 0.0
        self._lock = asyncio.Lock()

    async def _cargar(self) -> list[Producto]:
        ctx = (
            {"pricelist": settings.website_pricelist_id}
            if settings.website_pricelist_id > 0
            else {}
        )
        tmpls = await odoo.search_read(
            "product.template",
            [["active", "=", True]],
            ["id", "name", "list_price", "is_published", "image_128", "qty_available"],
            limit=500,
            order="name asc",
            context=ctx or None,
        )

        # Excluir nombres administrativos.
        tmpls = [
            t for t in tmpls
            if (t.get("name") or "").strip().upper() not in EXCLUIR_NOMBRES
        ]
        # Regla de negocio: SOLO productos PUBLICADOS y CON IMAGEN. Si el admin no
        # lo publicó (no lo quiso) o no tiene foto, no se le muestra al cliente:
        # casi siempre pide la imagen y no habría forma de dársela.
        # (image_128 se computa desde la imagen subida; vacío = sin foto real.)
        tmpls = [
            t for t in tmpls
            if t.get("is_published") is True and t.get("image_128")
        ]
        # Filtro por stock (SOLO_CON_STOCK): oculta agotados. FAIL-SAFE: si dejaría
        # el catálogo VACÍO (qty_available no es confiable en este Odoo, llega 0/None),
        # NO se aplica, para no dejar al bot sin productos. Apagado por defecto.
        if settings.solo_con_stock:
            con_stock = [t for t in tmpls if float(t.get("qty_available") or 0) > 0]
            if con_stock:
                log.info(
                    "catalogo_filtro_stock",
                    con_stock=len(con_stock), sin_stock=len(tmpls) - len(con_stock),
                )
                tmpls = con_stock
            else:
                log.warning("catalogo_filtro_stock_vacio_ignorado", total=len(tmpls))

        if not tmpls:
            return []

        ids = [t["id"] for t in tmpls]

        # Variantes: necesitamos el product.product id real para sale.order.line.
        variants = await odoo.search_read(
            "product.product",
            [["product_tmpl_id", "in", ids], ["active", "=", True]],
            ["id", "product_tmpl_id", "lst_price"],
            limit=1000,
            context=ctx or None,
        )
        variant_por_tmpl: dict[int, int] = {}
        precio_por_tmpl: dict[int, float] = {}
        for v in variants:
            tid = v["product_tmpl_id"]
            tid = tid[0] if isinstance(tid, list) else tid
            variant_por_tmpl.setdefault(tid, v["id"])
            if settings.website_pricelist_id > 0 and tid not in precio_por_tmpl:
                p = v.get("price")
                if p is None:
                    p = v.get("lst_price")
                if p is not None:
                    precio_por_tmpl[tid] = float(p)

        productos = [
            Producto(
                tmpl_id=t["id"],
                variant_id=variant_por_tmpl.get(t["id"], t["id"]),
                nombre=t["name"],
                precio_con_itbis=con_itbis(
                    precio_por_tmpl.get(t["id"], t.get("list_price"))
                ),
                website_slug=t.get("website_slug") or "",
            )
            for t in tmpls
        ]
        log.info("catalogo_cargado", productos=len(productos))
        return productos

    def _fresco(self, now: float) -> bool:
        return bool(self._cache) and (now - self._cache_ts) <= settings.catalogo_cache_seconds

    async def todos(self, force: bool = False) -> list[Producto]:
        if not force and self._fresco(time.monotonic()):
            return self._cache
        # Single-flight: un solo turno recarga; el resto espera y reutiliza. Antes,
        # al expirar la caché bajo carga, N turnos disparaban N recargas simultáneas
        # contra Odoo (estampida).
        async with self._lock:
            now = time.monotonic()
            if not force and self._fresco(now):
                return self._cache
            try:
                self._cache = await self._cargar()
                self._cache_ts = now
            except Exception as exc:  # noqa: BLE001
                log.error("catalogo_carga_fallo", error=str(exc))
                if not self._cache:
                    raise
        return self._cache

    async def por_tmpl_id(self, tmpl_id: int) -> Producto | None:
        for p in await self.todos():
            if p.tmpl_id == int(tmpl_id):
                return p
        return None

    async def buscar(self, texto: str, limite: int = 5) -> tuple[str, list[Producto]]:
        """Devuelve (veredicto, productos).

        veredicto ∈ {"match_fuerte", "candidatos", "muy_general", "vacio"}
        Misma lógica de umbrales que el nodo original.
        """
        productos = await self.todos()
        if not productos:
            return ("vacio", [])
        if not texto.strip():
            return ("candidatos", productos)

        ranked = sorted(
            ((p, score(texto, p.nombre)) for p in productos),
            key=lambda x: x[1],
            reverse=True,
        )
        ranked = [(p, s) for p, s in ranked if s > 0]
        if not ranked:
            return ("muy_general", [])

        mejor = ranked[0][1]
        segundo = ranked[1][1] if len(ranked) > 1 else 0
        es_tipo = es_busqueda_tipo_envase(texto)

        if mejor >= 8 and mejor >= segundo + 4:
            return ("match_fuerte", [ranked[0][0]])
        if mejor <= 1 and not es_tipo:
            return ("muy_general", [])

        min_sc = 1 if es_tipo else 2
        top = [p for p, s in ranked if s >= min_sc][:limite]
        return ("candidatos", top) if top else ("muy_general", [])

    async def por_nombre(self, nombre: str) -> Producto | None:
        veredicto, res = await self.buscar(nombre, limite=1)
        return res[0] if res else None


catalogo = Catalogo()
