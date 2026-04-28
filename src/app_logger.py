"""Logger centralizado del bot.

Reemplaza progresivamente los `print()` esparcidos por bot.py / gui.py por
loggers nombrados con niveles. Permite filtrar por capa (combat, route,
sniff, action, calibration) y por severidad.

Uso:
    from app_logger import get_logger
    log = get_logger("bot.calibration")

    log.info("estado X iniciado")
    log.warning("offset invalido para map=%s cell=%s: %r", map_id, cell_id, val)
    log.error("falló click: %s", err)
    log.exception("crash inesperado")  # incluye traceback

Setup:
    Idempotente. La primera llamada a get_logger() configura los handlers:
      - Consola (INFO+)
      - Archivo rotante DEBUG+ en logs/bot_YYYY-MM-DD.log (5MB x 5 backups)
    Las llamadas siguientes solo devuelven el logger.

Convención de nombres:
    Los loggers cuelgan del namespace raíz "bot.*":
      - "bot"              → bot.py general
      - "bot.combat"       → lógica de combate
      - "bot.calibration"  → calibración / proyecciones
      - "bot.route"        → navegación
      - "bot.sniff"        → sniffer / parsing
      - "bot.action"       → input mouse/teclado
      - "bot.gui"          → GUI

    get_logger("foo") sin prefijo se reescribe a "bot.foo" automaticamente.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from datetime import datetime
from pathlib import Path

_CONFIGURED = False
_DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def configure_logging(
    log_dir: Path | None = None,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configura los handlers raíz una sola vez. Idempotente.

    Llamar manualmente al arranque del programa si se quiere fijar niveles
    o un log_dir distinto del default. Si no se llama, el primer get_logger()
    la dispara con los defaults.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # No vamos a explotar el bot por no poder crear logs/. Avisamos por stderr.
        print(f"[app_logger] WARN: no pude crear log_dir={log_dir}: {e}", file=sys.stderr)

    fmt = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("bot")
    root.setLevel(min(console_level, file_level))
    # Limpio handlers previos por si se llama configure_logging dos veces
    # con _CONFIGURED reseteado (tests, reload).
    for h in list(root.handlers):
        root.removeHandler(h)

    # Consola
    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Archivo rotante diario, con buffering en memoria.
    # Por que MemoryHandler? Cada flush a disco cuesta ~0.5ms en Windows.
    # Sin buffer, un combate con 100+ eventos/s del sniffer perdia 50-100ms/turno
    # solo en file IO. MemoryHandler agrupa hasta `buffer_capacity` records y flushea
    # de una. flushLevel=WARNING garantiza que warnings/errors si se escriben de inmediato
    # (no perdemos diagnostico). flushOnClose=True asegura escritura al cierre normal.
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = log_dir / f"bot_{today}.log"
    try:
        fh = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setLevel(file_level)
        fh.setFormatter(fmt)
        # Wrap en MemoryHandler. capacity=50 records, flush si aparece WARNING+.
        memh = logging.handlers.MemoryHandler(
            capacity=1,  # FLUSH INMEDIATO durante debug — bajado de 50 a 1 (2026-04-27)
            flushLevel=logging.WARNING,
            target=fh,
            flushOnClose=True,
        )
        memh.setLevel(file_level)
        root.addHandler(memh)
    except OSError as e:
        print(f"[app_logger] WARN: no pude abrir log_file={log_file}: {e}", file=sys.stderr)

    # No propagar al root global (evita duplicados con basicConfig de terceros).
    root.propagate = False

    _CONFIGURED = True


def get_logger(name: str = "bot") -> logging.Logger:
    """Devuelve un logger nombrado bajo el namespace 'bot.*'.

    - get_logger("bot")            → logger "bot"
    - get_logger("bot.combat")     → logger "bot.combat"
    - get_logger("combat")         → logger "bot.combat" (prefijo automático)
    - get_logger("__main__")       → logger "bot.__main__"
    """
    if not _CONFIGURED:
        configure_logging()

    if name == "bot" or name.startswith("bot."):
        return logging.getLogger(name)
    return logging.getLogger(f"bot.{name}")
