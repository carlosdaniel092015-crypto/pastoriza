"""El aviso de "asistencia humana requerida" tiene que decir lo que dijo EL CLIENTE.

El bug real: la plantilla mandaba `trigger.content or mensaje`. Una nota de voz o una
foto no traen `content`, así que caía al fallback — y el fallback era la respuesta del
BOT. Al supervisor le llegaba "Ya le avisé al supervisor y deberían contactarte pronto",
que es justo lo que el bot acababa de contestar: cero información sobre qué necesita el
cliente.
"""
from __future__ import annotations

from app.models import InboundMessage
from app.pipeline import _texto_del_cliente

RESPUESTA_DEL_BOT = (
    "Entiendo tu preocupación. Ya le avisé al supervisor y deberían contactarte "
    "pronto. Agradezco tu paciencia mientras tanto. 😊"
)


def _msg(content: str = "", tipo: str = "text") -> InboundMessage:
    return InboundMessage(chat_id="18091112222", content=content, content_type=tipo)


class TestTextoEscrito:
    def test_lo_que_escribio_pasa_tal_cual(self):
        assert _texto_del_cliente("quiero hablar con una persona", _msg("quiero hablar con una persona")) == (
            "quiero hablar con una persona"
        )

    def test_sin_saltos_de_linea(self):
        """Una variable de plantilla de Meta no admite \\n: el envío se rechaza."""
        out = _texto_del_cliente("necesito\nayuda\ncon un pedido", _msg())
        assert "\n" not in out and "\t" not in out
        assert "necesito" in out and "pedido" in out

    def test_se_acota(self):
        out = _texto_del_cliente("a" * 900, _msg())
        assert len(out) <= 260


class TestNotaDeVoz:
    def test_manda_la_transcripcion_no_la_respuesta_del_bot(self):
        """El caso del reporte: audio → content vacío → antes caía al mensaje del bot."""
        texto = "<audio>\nEs que necesito hablar con alguien, tengo un problema\n</audio>"
        out = _texto_del_cliente(texto, _msg("", "audio"))
        assert "necesito hablar con alguien" in out
        assert "nota de voz" in out.lower()  # se avisa que habló, no escribió
        assert "avisé al supervisor" not in out

    def test_no_quedan_las_etiquetas(self):
        out = _texto_del_cliente("<audio>\nhola\n</audio>", _msg("", "audio"))
        assert "<audio>" not in out and "</audio>" not in out

    def test_audio_mas_texto_van_los_dos(self):
        out = _texto_del_cliente("quiero 5 fardos\n<audio>\ny me los mandas hoy\n</audio>", _msg())
        assert "5 fardos" in out and "mandas hoy" in out


class TestFoto:
    def test_no_manda_el_andamiaje_del_prompt(self):
        texto = (
            "# EL CLIENTE ENVIO UNA IMAGEN\n"
            "## LO QUE ESCRIBIO CON LA IMAGEN: necesito esta botella urgente\n"
            "## ANALISIS VISUAL:\n"
            "TIPO_ENVASE: Botella / CAPACIDAD: 12 oz / COLOR: Transparente"
        )
        out = _texto_del_cliente(texto, _msg("necesito esta botella urgente", "image"))
        assert "necesito esta botella urgente" in out
        assert "mandó una foto" in out
        # El análisis visual es la lectura del MODELO y es largo: no va al supervisor.
        assert "TIPO_ENVASE" not in out
        assert "#" not in out

    def test_foto_sin_caption_igual_se_avisa(self):
        texto = "# EL CLIENTE ENVIO UNA IMAGEN\n## ANALISIS VISUAL:\nTIPO_ENVASE: Botella"
        out = _texto_del_cliente(texto, _msg("", "image"))
        assert "mandó una foto" in out
        assert "TIPO_ENVASE" not in out


class TestNuncaLaRespuestaDelBot:
    def test_sin_texto_ni_content_da_algo_neutro(self):
        """El peor caso: no se pudo reconstruir nada. Antes acá entraba el mensaje del
        bot; ahora entra un texto neutro que al menos no engaña al supervisor."""
        out = _texto_del_cliente("", _msg())
        assert out and "avisé al supervisor" not in out
        assert "una persona" in out

    def test_la_respuesta_del_bot_no_aparece_por_ningun_camino(self):
        for texto, trigger in [
            ("", _msg()),
            ("", _msg("", "audio")),
            ("<audio>\n\n</audio>", _msg("", "audio")),
            ("# EL CLIENTE ENVIO UNA IMAGEN\n## ANALISIS VISUAL:\nx", _msg("", "image")),
        ]:
            out = _texto_del_cliente(texto, trigger)
            assert RESPUESTA_DEL_BOT not in out
            assert "avisé al supervisor" not in out
