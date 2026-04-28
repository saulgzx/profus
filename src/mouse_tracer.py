"""Tracer de pyautogui.moveTo — instrumentación para diagnosticar movimientos del cursor.

Uso:
    from mouse_tracer import enable_mouse_trace
    enable_mouse_trace()  # antes de cualquier import del bot que use pyautogui

Una vez activo, cada `pyautogui.moveTo(x, y)` loguea como:
    [INFO] [bot.mouse] moveTo(1234, 567) caller=src/bot.py:8123 fn=_some_function

Esto permite identificar qué código mueve el cursor en runtime.

Para desactivar después: `disable_mouse_trace()`.
"""

from __future__ import annotations

import inspect
import os

import pyautogui

from app_logger import get_logger

_log = get_logger("bot.mouse")
_ORIGINAL_MOVETO = None


def _format_caller() -> str:
    """Devuelve 'archivo.py:LINEA fn=funcion' del primer frame externo a este wrapper."""
    frame = inspect.currentframe()
    # Saltar: este frame + el del wrapper
    if frame is None:
        return "?"
    cur = frame.f_back  # el wrapper
    if cur is not None:
        cur = cur.f_back  # el caller real
    if cur is None:
        return "?"
    fname = os.path.basename(cur.f_code.co_filename)
    lineno = cur.f_lineno
    fn_name = cur.f_code.co_name
    return f"{fname}:{lineno} fn={fn_name}"


def _traced_moveTo(x=None, y=None, duration=0.0, tween=None, logScreenshot=False, _pause=True):
    """Replacement de pyautogui.moveTo con logging."""
    caller = _format_caller()
    _log.info("moveTo(%s, %s) caller=%s", x, y, caller)
    return _ORIGINAL_MOVETO(x, y, duration=duration, tween=tween or pyautogui.linear,
                            logScreenshot=logScreenshot, _pause=_pause)


def enable_mouse_trace() -> None:
    """Reemplaza pyautogui.moveTo por la versión instrumentada. Idempotente."""
    global _ORIGINAL_MOVETO
    if _ORIGINAL_MOVETO is not None:
        return  # ya activo
    _ORIGINAL_MOVETO = pyautogui.moveTo
    pyautogui.moveTo = _traced_moveTo
    _log.info("mouse_tracer ENABLED — cada moveTo va a loggearse con caller")


def disable_mouse_trace() -> None:
    """Restaura pyautogui.moveTo original."""
    global _ORIGINAL_MOVETO
    if _ORIGINAL_MOVETO is None:
        return
    pyautogui.moveTo = _ORIGINAL_MOVETO
    _ORIGINAL_MOVETO = None
    _log.info("mouse_tracer DISABLED")
