"""Configuración compartida de pytest.

Los tests no deben depender de un .env real ni de secretos: app/settings.py
instancia Settings() al importarse y exige OPENAI_API_KEY e YCLOUD_API_KEY.
Fijamos valores dummy ANTES de que se importe cualquier módulo de `app`, para
que la suite corra sin .env, sin Redis, sin Odoo y sin gastar tokens.
"""
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("YCLOUD_API_KEY", "test-key")
