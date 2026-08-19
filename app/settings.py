"""Configuración de entorno (secretos e infraestructura).

Todo lo que aquí aparece viene de variables de entorno / .env.
La configuración *de negocio* (precios, cuentas, horarios) NO vive acá:
esa la edita el cliente desde el panel y vive en Redis (ver app/business_config.py).
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- App ----------
    app_env: str = "production"
    log_level: str = "INFO"
    # Token que YCloud debe mandar en el header X-Webhook-Token (lo configurás en YCloud).
    # Si queda vacío, el webhook NO valida nada (solo para desarrollo local).
    webhook_token: str = ""

    # ---------- Redis ----------
    redis_url: str = "redis://localhost:6379/1"
    # Prefijo para NO chocar con las keys que ya usa n8n en la misma instancia.
    redis_prefix: str = "pastoriza:"
    session_ttl_seconds: int = 604_800  # 7 días de historial de conversación
    session_max_items: int = 40  # equivalente al contextWindowLength: 20 de n8n
    # Tope de conexiones del pool. DEBE quedar por debajo del máximo de clientes del
    # plan de Redis (en Redis Cloud gratis son 30): al pasarse, Redis rechaza TODO
    # con "max number of clients reached" y el bot deja de poder atender. Si el panel
    # o el bot quedan lentos por esperar conexión, subilo (y revisá el plan).
    redis_max_conexiones: int = 12

    # ---------- Debounce ----------
    debounce_seconds: float = 6.0
    debounce_max_wait: float = 25.0  # techo duro: nunca esperar más que esto

    # ---------- OpenAI ----------
    openai_api_key: str
    model_agente: str = "gpt-4o"  # agente Pedido (el delicado)
    model_mini: str = "gpt-4o-mini"  # enrutador, ventas, soporte
    model_vision: str = "gpt-4o-mini"
    model_transcripcion: str = "whisper-1"
    agente_max_turns: int = 12  # corta loops infinitos de tool-calling
    # Timeout por llamada a OpenAI (visión, transcripción, chat). El default del
    # SDK es de minutos; sin techo, una degradación del proveedor cuelga el turno.
    openai_timeout: float = 30.0
    # Techo del turno del agente completo (Runner.run). Debe quedar por debajo del
    # TTL del lock de conversación (ver redis_client.conversation_lock) para que el
    # lock no expire con el turno todavía vivo.
    agente_timeout: float = 90.0

    # ---------- YCloud ----------
    ycloud_api_key: str
    ycloud_base_url: str = "https://api.ycloud.com/v2"
    ycloud_from: str = ""  # número/WABA emisor; si vacío se toma del payload
    # Dominio público del propio servicio. Se usa para servir las fotos ya
    # convertidas a JPG (endpoint /img) y que YCloud las tome de acá, sin depender
    # del proxy externo weserv. Si está vacío, se cae a RAILWAY_PUBLIC_DOMAIN.
    public_base_url: str = ""
    admin_phone: str = "+18294716701"
    template_alerta_supervisor: str = "alerta_supervisor_cliente"
    template_pedido_creado: str = "notificar_pedido_creado"
    # Plantilla con el comprobante + resumen del pedido y los botones de aprobación
    # (ver PLANTILLA_META.md). Si Meta todavía no la aprobó, el envío falla y el
    # sistema cae al aviso de siempre: el pedido queda igual pendiente en el panel.
    template_aprobacion_pago: str = "aprobacion_pago"
    # La misma plantilla para RETIRO, sin encabezado de imagen: en retiro no hay
    # comprobante, y una plantilla con encabezado de imagen EXIGE una imagen en cada
    # envío (Meta rechaza el mensaje entero si falta). Por eso son dos.
    template_aprobacion_retiro: str = "aprobacion_retiro1"
    template_lang: str = "es_DO"

    # ---------- Odoo (XML-RPC) ----------
    odoo_url: str = "https://pastorizaplastic.net"
    odoo_db: str = "pastoriza-plastics"
    odoo_user: str = ""  # email del usuario de la API
    odoo_password: str = ""  # API key de Odoo (rotala si estuvo expuesta)
    odoo_uid_fallback: int = 2  # si authenticate() falla, usar este uid
    odoo_country_id: int = 61  # República Dominicana
    odoo_timeout: int = 30

    # ---------- Catálogo ----------
    website_pricelist_id: int = 0
    precios_guardados_con_itbis: bool = True
    itbis_rate: float = 0.18
    catalogo_cache_seconds: int = 300
    # Mostrar SOLO productos con stock disponible (qty_available > 0). DEFAULT OFF:
    # en este Odoo qty_available no es confiable (llega 0/None) y vaciaba el catálogo.
    # Activar con SOLO_CON_STOCK=true SOLO si se confirma que el stock está bien cargado.
    solo_con_stock: bool = False
    # Tope de fotos por respuesta. Alto para poder mostrar TODOS los envases de una
    # medida (ej. "las de 12 oz") cuando el cliente pide ver esa categoría.
    max_imagenes_por_mensaje: int = 10

    # Si el supervisor le escribe al cliente desde YCloud, pausar el bot 30 min
    # para ese cliente (toma de control). Se apoya en whatsapp.message.updated.
    pausar_por_agente_humano: bool = True

    # ---------- Panel de operación ----------
    # Token para entrar al panel (/panel). Vacío = sin auth (solo desarrollo local).
    panel_token: str = ""
    # Telegram para alertas de error (opcional). Si faltan, no notifica por Telegram.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Secreto para validar el webhook de Telegram (botones Aprobar/Rechazar). Telegram
    # lo reenvía en el header X-Telegram-Bot-Api-Secret-Token. Vacío = no se registra
    # el webhook ni se aceptan callbacks (los botones quedan inertes por seguridad).
    telegram_webhook_secret: str = ""
    # Telegram es para ALERTAS: errores y cosas rotas. Con esto en True (por defecto) no
    # se manda nada más — las sugerencias del analista, que son de MEJORA y no urgentes,
    # quedan sólo en el panel (módulo Aprendizaje). Ponelo en False si querés que las
    # sugerencias también lleguen por Telegram con sus botones.
    telegram_solo_alertas: bool = True

    # ---------- Analista de aprendizaje ----------
    # Corrida automática del analista (propone reglas a partir de la cola de revisión).
    analista_auto: bool = True
    analista_intervalo_horas: int = 24  # cada cuánto corre el análisis

    # ---------- Canario de producción ----------
    # El bot se vigila a sí mismo (catálogo, Redis, pico de escaladas) y avisa por
    # Telegram cuando algo se rompe. Sólo lee: no toca el flujo de venta.
    canario_activo: bool = True
    canario_intervalo_minutos: int = 10
    canario_max_escaladas_hora: int = 15

    # ---------- Migración gradual ----------
    # Si tiene contenido, SOLO estos números se procesan acá (el resto lo ignora
    # este servicio y lo sigue atendiendo n8n). Separados por coma.
    allowlist_numeros: str = ""

    @property
    def allowlist(self) -> set[str]:
        return {n.strip() for n in self.allowlist_numeros.split(",") if n.strip()}

    @property
    def base_url(self) -> str:
        """URL pública del servicio (sin barra final). Vacío = usar proxy weserv."""
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        dom = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        return f"https://{dom}" if dom else ""

    def key(self, *parts: str) -> str:
        return self.redis_prefix + ":".join(parts)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()

# El SDK openai-agents y la librería openai leen la API key del ENTORNO del SO
# (os.environ["OPENAI_API_KEY"]), no de este objeto Settings. En Docker el
# env_file la exporta al entorno; en ejecución local directa hay que propagarla
# nosotros. setdefault: si ya viene del entorno real, ese gana.
if settings.openai_api_key:
    os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key)
