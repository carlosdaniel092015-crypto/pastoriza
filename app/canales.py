"""CANAL = el número NUESTRO de YCloud por el que entra una conversación.

El bot atiende DOS números (uno COEXISTENTE con la app de WhatsApp) y la operación
de cada uno es INDEPENDIENTE: sus conversaciones, su configuración de negocio, sus
prompts, sus reglas aprendidas y sus agentes. Este módulo es la pieza mínima
compartida: cómo se identifica un canal y cómo se nombra su key en Redis.

Modelo de datos, idéntico para config, prompts, conocimiento y agentes:

    pastoriza:algo             -> valor COMÚN (la base que heredan los dos números)
    pastoriza:algo:c:8092221092 -> valor PROPIO de ese canal (gana sobre el común)

De ahí salen las dos garantías que pidió la operación:
  - un cambio hecho dentro de un canal NO toca al otro (se guarda en su key propia);
  - "aplicar a ambos" escribe el común y borra los propios, así los dos quedan igual.

A propósito SIN dependencias (ni settings ni redis): lo importan business_config,
prompt_store, conocimiento, agentes_custom, el pipeline y el panel.
"""
from __future__ import annotations

import re

COMUN = ""  # canal vacío = configuración común a los dos números


def canal_id(numero: str | None) -> str:
    """Identificador estable de un canal: los últimos 10 dígitos del número.

    YCloud/Meta entregan el mismo número como "+1 809…", "1809…" u "809…": comparar
    el string crudo partiría un solo canal en tres.
    """
    digitos = re.sub(r"\D", "", str(numero or ""))
    return digitos[-10:] if len(digitos) >= 10 else digitos


def key_canal(base: str, canal: str | None = COMUN) -> str:
    """Key de Redis del valor PROPIO de `canal`; la key común si no hay canal."""
    c = canal_id(canal)
    return f"{base}:c:{c}" if c else base


def formatear(canal: str | None) -> str:
    """809-922-1092 (para mostrar). Devuelve el número tal cual si no son 10 dígitos."""
    c = canal_id(canal)
    return f"{c[0:3]}-{c[3:6]}-{c[6:]}" if len(c) == 10 else c


__all__ = ["COMUN", "canal_id", "key_canal", "formatear"]
