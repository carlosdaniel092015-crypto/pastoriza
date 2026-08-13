"""Qué versión está corriendo AHORA en producción.

Existe porque no había forma de saberlo: cuando un cambio "no aparecía", era
imposible distinguir "el deploy no salió" de "el código no funciona", y se perdía
tiempo buscando el bug en el lugar equivocado. Railway inyecta el commit en el
entorno; lo exponemos en /health y en el panel.
"""
from __future__ import annotations

import os

# Railway inyecta estas variables en cada deploy. En local quedan vacías.
_SHA = (
    os.environ.get("RAILWAY_GIT_COMMIT_SHA")
    or os.environ.get("GIT_COMMIT_SHA")
    or os.environ.get("SOURCE_COMMIT")
    or ""
)
_RAMA = os.environ.get("RAILWAY_GIT_BRANCH", "")
_DEPLOY = os.environ.get("RAILWAY_DEPLOYMENT_ID", "")


def commit() -> str:
    return _SHA[:7] if _SHA else "local"


def info() -> dict:
    """Datos de la versión desplegada, para /health y el panel."""
    return {
        "commit": commit(),
        "rama": _RAMA or "-",
        "deploy_id": _DEPLOY[:8] if _DEPLOY else "-",
    }
