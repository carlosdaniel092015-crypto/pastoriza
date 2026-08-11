"""Logging estructurado en JSON.

Esto reemplaza el panel de ejecuciones de n8n: si no lo mirás, quedás ciego.
Cada línea trae chat_id para poder filtrar una conversación completa:

    docker logs -f pastoriza-bot | grep '"chat_id":"18091234567"'
"""
from __future__ import annotations

import logging
import sys

import structlog

from app.settings import settings


def setup_logging() -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "pastoriza"):
    return structlog.get_logger(name)
