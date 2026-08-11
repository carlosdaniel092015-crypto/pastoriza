"""Cliente Odoo por XML-RPC.

Reemplaza los 8 nodos `odooTool` y las llamadas JSON-RPC embebidas en los Code
nodes. xmlrpc.client es bloqueante, así que todo se despacha con
`asyncio.to_thread` para no frenar el event loop de FastAPI.

REQUISITO: XML-RPC debe estar habilitado. En Odoo Online (odoo.com) sólo está
disponible en plan Custom; en self-hosted (tu caso, pastorizaplastic.net) viene
habilitado por defecto en /xmlrpc/2/.
"""
from __future__ import annotations

import asyncio
import threading
import xmlrpc.client
from typing import Any

from app.logging_conf import get_logger
from app.settings import settings

log = get_logger(__name__)


class OdooError(RuntimeError):
    pass


class OdooClient:
    def __init__(self) -> None:
        self._uid: int | None = None
        self._lock = threading.Lock()
        base = settings.odoo_url.rstrip("/")
        self._common_url = f"{base}/xmlrpc/2/common"
        self._object_url = f"{base}/xmlrpc/2/object"

    # ---------------------------------------------------------- internos ---
    def _proxy(self, url: str) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(url, allow_none=True)

    def _authenticate(self) -> int:
        with self._lock:
            if self._uid is not None:
                return self._uid
            try:
                common = self._proxy(self._common_url)
                uid = common.authenticate(
                    settings.odoo_db,
                    settings.odoo_user,
                    settings.odoo_password,
                    {},
                )
                if not uid:
                    raise OdooError("Odoo authenticate() devolvió False")
                self._uid = int(uid)
            except OdooError:
                raise
            except Exception as exc:  # noqa: BLE001
                if settings.odoo_uid_fallback:
                    log.warning(
                        "odoo_auth_fallback",
                        error=str(exc),
                        uid=settings.odoo_uid_fallback,
                    )
                    self._uid = settings.odoo_uid_fallback
                else:
                    raise OdooError(f"No pude autenticar contra Odoo: {exc}") from exc
            return self._uid

    def _execute_sync(
        self, model: str, method: str, args: list, kwargs: dict | None = None
    ) -> Any:
        uid = self._authenticate()
        obj = self._proxy(self._object_url)
        try:
            return obj.execute_kw(
                settings.odoo_db,
                uid,
                settings.odoo_password,
                model,
                method,
                args,
                kwargs or {},
            )
        except xmlrpc.client.Fault as fault:
            # Si el uid quedó viejo, invalidamos y que el próximo intento re-autentique.
            self._uid = None
            raise OdooError(f"Odoo {model}.{method}: {fault.faultString}") from fault

    # ------------------------------------------------------------ público ---
    async def execute(
        self, model: str, method: str, args: list, kwargs: dict | None = None
    ) -> Any:
        return await asyncio.wait_for(
            asyncio.to_thread(self._execute_sync, model, method, args, kwargs),
            timeout=settings.odoo_timeout,
        )

    async def search_read(
        self,
        model: str,
        domain: list,
        fields: list[str],
        limit: int = 80,
        order: str | None = None,
        context: dict | None = None,
    ) -> list[dict]:
        kwargs: dict[str, Any] = {"fields": fields, "limit": limit}
        if order:
            kwargs["order"] = order
        if context:
            kwargs["context"] = context
        return await self.execute(model, "search_read", [domain], kwargs)

    async def create(self, model: str, values: dict) -> int:
        return int(await self.execute(model, "create", [values]))

    async def write(self, model: str, rec_id: int, values: dict) -> bool:
        return bool(await self.execute(model, "write", [[rec_id], values]))

    async def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict]:
        return await self.execute(model, "read", [ids, fields])

    async def get_param(self, key: str) -> str | None:
        res = await self.execute("ir.config_parameter", "get_param", [key])
        return None if res in (False, None) else str(res)

    async def set_param(self, key: str, value: str) -> Any:
        return await self.execute("ir.config_parameter", "set_param", [key, value])

    async def ping(self) -> bool:
        try:
            await self.execute("res.users", "search_count", [[]], {"limit": 1})
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("odoo_ping_failed", error=str(exc))
            return False


odoo = OdooClient()
