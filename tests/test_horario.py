"""Ventana horaria en la que el bot responde, por canal (app/horario.py).

Pura: sin Redis. El caso real que motivó esto: "quiero que el bot 8294716701
solamente se active automáticamente de 7 pm a 5 am todos los días" — una ventana que
CRUZA la medianoche.
"""
from __future__ import annotations

import os
import time as time_mod
from datetime import datetime

import pytest

from app.horario import dentro_de_horario


def _a_las(hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 1, 15, hh, mm)


class TestSinRestriccion:
    def test_vacio_los_dos_siempre_activo(self):
        assert dentro_de_horario("", "", _a_las(12)) is True
        assert dentro_de_horario("", "", _a_las(3)) is True

    def test_uno_solo_vacio_tambien_sin_restriccion(self):
        """Config a medio llenar no puede dejar al bot mudo sin querer."""
        assert dentro_de_horario("19:00", "", _a_las(10)) is True
        assert dentro_de_horario("", "05:00", _a_las(10)) is True


class TestVentanaQueCruzaLaMedianoche:
    """El caso pedido: 19:00 a 05:00, todos los días."""

    def test_activo_de_noche(self):
        assert dentro_de_horario("19:00", "05:00", _a_las(19, 0)) is True  # arranca
        assert dentro_de_horario("19:00", "05:00", _a_las(23, 30)) is True
        assert dentro_de_horario("19:00", "05:00", _a_las(0, 1)) is True

    def test_activo_de_madrugada(self):
        assert dentro_de_horario("19:00", "05:00", _a_las(4, 59)) is True

    def test_inactivo_de_dia(self):
        assert dentro_de_horario("19:00", "05:00", _a_las(5, 0)) is False  # termina
        assert dentro_de_horario("19:00", "05:00", _a_las(12, 0)) is False
        assert dentro_de_horario("19:00", "05:00", _a_las(18, 59)) is False


class TestVentanaNormal:
    """Sin cruzar medianoche, por si alguien la usa así (horario de oficina, etc.)."""

    def test_dentro(self):
        assert dentro_de_horario("08:00", "17:00", _a_las(12)) is True
        assert dentro_de_horario("08:00", "17:00", _a_las(8, 0)) is True  # arranca

    def test_fuera(self):
        assert dentro_de_horario("08:00", "17:00", _a_las(17, 0)) is False  # termina
        assert dentro_de_horario("08:00", "17:00", _a_las(3)) is False


class TestConfigRota:
    """Falla ABIERTO: un typo no puede dejar al bot mudo todo el día sin explicación."""

    def test_texto_invalido_no_explota_y_no_restringe(self):
        assert dentro_de_horario("no es una hora", "05:00", _a_las(12)) is True
        assert dentro_de_horario("19:00", "tampoco", _a_las(12)) is True
        assert dentro_de_horario("25:99", "05:00", _a_las(12)) is True

    def test_usa_ahora_real_si_no_se_pasa(self):
        # No debe levantar al usar datetime.now() internamente.
        assert isinstance(dentro_de_horario("", ""), bool)


class TestSinBaseDeZonasHorarias:
    """Si falta `tzdata` (Windows sin el paquete, un venv no actualizado) el bot
    entero no se puede caer al arrancar: mejor perder precisión de zona que perder
    el proceso completo (ver el comentario de ZONA_RD en app/horario.py)."""

    def test_import_no_explota_si_zoneinfo_falla(self):
        import importlib
        import zoneinfo

        import app.horario as horario_mod

        class _ZoneInfoRota:
            def __init__(self, *a, **kw):
                raise Exception("No time zone found (simulado)")

        # Se parcha en el módulo `zoneinfo`, no en `horario_mod`: `reload` vuelve a
        # ejecutar `from zoneinfo import ZoneInfo`, así que el parche tiene que estar
        # en el origen para sobrevivir esa reimportación. Manual (no `monkeypatch`)
        # porque el `undo` de `monkeypatch` corre DESPUÉS de este `finally`, y el
        # reload de limpieza necesita el ZoneInfo real ya restaurado o deja el módulo
        # roto para el resto de la suite (`reload` muta el módulo en `sys.modules`).
        original = zoneinfo.ZoneInfo
        zoneinfo.ZoneInfo = _ZoneInfoRota
        try:
            importlib.reload(horario_mod)
            assert horario_mod.ZONA_RD is None
            assert isinstance(horario_mod.dentro_de_horario("19:00", "05:00"), bool)
        finally:
            zoneinfo.ZoneInfo = original
            importlib.reload(horario_mod)  # deja el módulo real para el resto de tests

    def test_zona_en_none_cae_a_la_hora_del_sistema(self, monkeypatch):
        import app.horario as horario_mod

        monkeypatch.setattr(horario_mod, "ZONA_RD", None)
        # datetime.now(None) es exactamente datetime.now(): no debe levantar.
        assert isinstance(dentro_de_horario("19:00", "05:00"), bool)


@pytest.mark.skipif(not hasattr(time_mod, "tzset"), reason="tzset es POSIX, no Windows")
class TestUsaHoraDeRepublicaDominicanaSinImportarElServidor:
    """El 829-471-6701 no se puede activar antes de tiempo por un servidor con otra
    zona horaria (TZ del sistema/contenedor, una migración, un redeploy que pierda la
    variable): la hora se calcula EXPLÍCITA en código (America/Santo_Domingo, sin
    horario de verano), no según cómo esté configurado el reloj del proceso."""

    def test_ignora_la_variable_tz_del_proceso(self, monkeypatch):
        original = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "UTC"
            time_mod.tzset()
            # Si el código usara datetime.now() sin zona (hora del SISTEMA), este
            # resultado dependería de que el proceso esté en UTC. Con la zona RD
            # explícita, da la MISMA respuesta que si el sistema estuviera bien puesto.
            con_tz_utc = dentro_de_horario("19:00", "05:00")

            os.environ["TZ"] = "America/Santo_Domingo"
            time_mod.tzset()
            con_tz_rd = dentro_de_horario("19:00", "05:00")

            assert con_tz_utc == con_tz_rd
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time_mod.tzset()
