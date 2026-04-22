"""
Configuración pytest para el proyecto dofus-autofarm.

Agrega `src/` al `sys.path` para que los tests puedan importar módulos
directamente (`import sniffer`, `import perf`, ...).
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
