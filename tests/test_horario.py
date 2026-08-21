"""Ventana horaria en la que el bot responde, por canal (app/horario.py).

Pura: sin Redis. El caso real que motivó esto: "quiero que el bot 8294716701
solamente se active automáticamente de 7 pm a 5 am todos los días" — una ventana que
CRUZA la medianoche.
"""
from __future__ import annotations

from datetime import datetime

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
