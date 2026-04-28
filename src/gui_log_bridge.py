"""Puente OPCIONAL entre el logger centralizado y la pestaña Log de la GUI.

NO esta conectado por default — el usuario decidio que tener los logs en
`logs/bot_YYYY-MM-DD.log` es suficiente, sin necesidad de duplicarlos en la GUI.

Si en el futuro se quiere activar (e.g. para sesiones de debug en vivo),
agregar UNA linea en `gui.py::App.__init__` despues de crear `self.log_queue`:

    from gui_log_bridge import attach_logger_to_gui_queue
    attach_logger_to_gui_queue(self.log_queue, level=logging.INFO)

Para desconectar:

    from gui_log_bridge import detach_logger_from_gui_queue
    detach_logger_from_gui_queue()

El modulo es seguro de importar sin efectos colaterales — solo se conecta
cuando se llama explicitamente a attach_logger_to_gui_queue().
"""

from __future__ import annotations

import logging
from queue import Queue
from typing import Optional


_HANDLER_INSTANCE: Optional[logging.Handler] = None


class GuiQueueHandler(logging.Handler):
    """logging.Handler que pushea cada record al log_queue de la GUI.

    Formato:
        INFO/DEBUG: "[bot.combat] mensaje"
        WARNING+:   "[WARN][bot.combat] mensaje"
    """

    def __init__(self, gui_queue: Queue):
        super().__init__()
        self.gui_queue = gui_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.levelno >= logging.WARNING:
                level_tag = record.levelname[:4]
                formatted = f"[{level_tag}][{record.name}] {msg}"
            else:
                formatted = f"[{record.name}] {msg}"
            try:
                self.gui_queue.put_nowait(("log", formatted))
            except Exception:
                pass
        except Exception:
            self.handleError(record)


def attach_logger_to_gui_queue(
    gui_queue: Queue,
    level: int = logging.INFO,
    logger_name: str = "bot",
) -> GuiQueueHandler:
    """Engancha un GuiQueueHandler al logger 'bot.*'. Idempotente."""
    global _HANDLER_INSTANCE
    if _HANDLER_INSTANCE is not None:
        detach_logger_from_gui_queue()
    handler = GuiQueueHandler(gui_queue)
    handler.setLevel(level)
    logging.getLogger(logger_name).addHandler(handler)
    _HANDLER_INSTANCE = handler
    return handler


def detach_logger_from_gui_queue(logger_name: str = "bot") -> None:
    """Desconecta el handler. Util al cerrar la App."""
    global _HANDLER_INSTANCE
    if _HANDLER_INSTANCE is None:
        return
    logging.getLogger(logger_name).removeHandler(_HANDLER_INSTANCE)
    _HANDLER_INSTANCE = None
