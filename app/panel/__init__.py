"""Panel de operación: observabilidad + edición del bot + errores.

Se monta sobre el mismo FastAPI (rutas /panel/*). Reusa Redis, estado y ycloud
que ya existen; sólo agrega una capa de lectura/control encima.
"""
