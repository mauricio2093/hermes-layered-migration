"""hermes-updater: orquestador de mantenimiento y actualizaciones de Hermes.

Separado a proposito de `hermes-maint`: aquel OBSERVA y no puede actuar; este
actua (backups, preparacion de candidatos, actualizaciones auxiliares) y, solo
con una aprobacion atestiguada por TOTP, despliega en produccion.

Solo stdlib: corre con el python del sistema, no con el venv de Hermes, porque
ese venv es justo lo que puede cambiar durante una actualizacion.
"""

__version__ = "0.1.0"
