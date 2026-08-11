from app.tools.catalogo_tools import CATALOGO_TOOLS
from app.tools.cotizar_tools import COTIZAR_TOOLS
from app.tools.odoo_tools import ODOO_TOOLS

TODAS_LAS_TOOLS = [*CATALOGO_TOOLS, *COTIZAR_TOOLS, *ODOO_TOOLS]

__all__ = ["TODAS_LAS_TOOLS", "CATALOGO_TOOLS", "COTIZAR_TOOLS", "ODOO_TOOLS"]
