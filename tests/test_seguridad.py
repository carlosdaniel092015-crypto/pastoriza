"""Tests de las dos garantías que justifican migrar de n8n a código.

1. El bot no puede confirmar un pedido que no existe.
2. El bot no puede enviar la foto de un producto que no salió de una tool
   en ESTE turno (adiós al "no reutilices la imagen de un turno anterior"
   escrito en el prompt y cruzando los dedos).
"""
from __future__ import annotations

from app.business_config import BusinessConfig
from app.catalogo import Producto
from app.context import ConversationContext
from app.pipeline import _resolver_fotos, _sanear
from app.ycloud import YCloud


def ctx_nuevo(**kw) -> ConversationContext:
    base = dict(
        chat_id="18090000000",
        telefono="18090000000",
        user_name="Test",
        emisor="test",
        destino={"to": "18090000000"},
        cfg=BusinessConfig(),
    )
    base.update(kw)
    return ConversationContext(**base)


PRODUCTO = Producto(
    tmpl_id=42,
    variant_id=99,
    nombre="BOTELLA LISA ECO 8 OZ",
    precio_con_itbis=5.87,
)


class TestBloqueDinamico:
    def test_incluye_minimo_de_envio_por_tamano(self):
        from app.agents.base import _bloque_dinamico

        c = ctx_nuevo()
        bloque = _bloque_dinamico(c)
        assert "CANTIDAD MÍNIMA POR TAMAÑO" in bloque
        assert c.cfg.envio_minimo_por_tamano in bloque

    def test_omite_bloque_si_no_hay_minimo(self):
        from app.agents.base import _bloque_dinamico

        c = ctx_nuevo(cfg=BusinessConfig(envio_minimo_por_tamano=""))
        bloque = _bloque_dinamico(c)
        assert "CANTIDAD MÍNIMA POR TAMAÑO" not in bloque


class TestNoConfirmarPedidosFalsos:
    def test_bloquea_pedido_registrado_sin_order_id(self):
        c = ctx_nuevo()
        out = _sanear("Listo! Tu pedido quedó registrado, gracias.", c)
        assert "registrado" not in out.lower()
        assert "comprobante" in out.lower()
        assert "claim_pedido_sin_order_id" in c.motivo_revision

    def test_bloquea_recibi_tu_comprobante_sin_pedido(self):
        c = ctx_nuevo()
        out = _sanear("Recibí tu comprobante, ya lo procesamos.", c)
        assert "Recibí tu comprobante" not in out

    def test_permite_confirmacion_con_order_id_real(self):
        c = ctx_nuevo(order_id=1234)
        texto = "Listo! Tu pedido quedó registrado con el número 1234."
        assert _sanear(texto, c) == texto
        assert c.motivo_revision == []

    def test_mensaje_normal_pasa_intacto(self):
        c = ctx_nuevo()
        texto = "Tenemos la botella lisa de 8 oz a RD$5.87. Cuántas necesitas?"
        assert _sanear(texto, c) == texto

    def test_vacio(self):
        assert _sanear("", ctx_nuevo()) == ""


class TestNoInventarFotos:
    def test_solo_productos_ofrecidos_este_turno(self):
        c = ctx_nuevo()
        c.ofrecer([PRODUCTO])
        fotos = _resolver_fotos([42], c)
        assert len(fotos) == 1
        assert fotos[0][0].endswith("/product.template/42/image_1024")
        assert "BOTELLA LISA ECO 8 OZ" in fotos[0][1]

    def test_id_no_ofrecido_se_descarta(self):
        c = ctx_nuevo()
        c.ofrecer([PRODUCTO])
        assert _resolver_fotos([999], c) == []

    def test_mezcla_valido_e_invalido(self):
        c = ctx_nuevo()
        c.ofrecer([PRODUCTO])
        assert len(_resolver_fotos([42, 777, 888], c)) == 1

    def test_tope_de_imagenes(self):
        c = ctx_nuevo()
        productos = [
            Producto(tmpl_id=i, variant_id=i, nombre=f"P{i}", precio_con_itbis=1.0)
            for i in range(20)
        ]
        c.ofrecer(productos)
        assert len(_resolver_fotos(list(range(20)), c)) == 5

    def test_lista_vacia(self):
        assert _resolver_fotos([], ctx_nuevo()) == []


class TestTroceo:
    def test_texto_corto_es_un_solo_mensaje(self):
        assert YCloud.trocear("Hola, en qué te ayudo?") == ["Hola, en qué te ayudo?"]

    def test_parrafos_se_separan(self):
        assert len(YCloud.trocear("Primero.\n\nSegundo.\n\nTercero.")) == 3

    def test_lista_numerada_no_se_fragmenta(self):
        lista = "Catálogo:\n" + "\n".join(f"{i}. Producto {i}" for i in range(1, 11))
        chunks = YCloud.trocear(lista)
        assert len(chunks) == 1
        assert "10. Producto 10" in chunks[0]

    def test_parrafo_largo_se_corta_por_oracion(self):
        largo = " ".join(["Esta es una oración de prueba."] * 30)
        chunks = YCloud.trocear(largo)
        assert len(chunks) > 1
        assert all(len(c) <= 400 for c in chunks)

    def test_vacio(self):
        assert YCloud.trocear("   ") == []
