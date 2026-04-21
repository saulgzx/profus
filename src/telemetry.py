"""Telemetria de combate.

Dos canales:
  1) `logs/combat-{date}.log`  — log rotativo human-readable (logging stdlib).
  2) `logs/combat-{date}.jsonl` — un evento por linea, JSON, para reproducir
     o auditar peleas. Cada record lleva `ts`, `kind`, `fight_id`, `turn`.

API minima — singleton:

    from telemetry import get_telemetry
    tel = get_telemetry()
    tel.set_enabled(True)            # opt-in via config
    tel.start_fight(fight_id=42)
    tel.set_turn(3)
    tel.emit("on_turn_begin", pa=8, mp=3, my_cell=255)
    tel.info("SADIDA", "Combo1 listo")
    tel.emit("on_turn_end", result="done", reason="combo_1_completed")
    tel.end_fight()

Si `enabled=False`, todas las llamadas son no-op (overhead minimo).
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")


def _ensure_log_dir() -> None:
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
    except Exception:
        pass


class CombatTelemetry:
    """Singleton-style telemetria. Thread-safe para escrituras JSONL."""

    def __init__(self) -> None:
        self.enabled: bool = False
        self._fight_id = None
        self._turn_number: int = 0
        self._fight_started_at: float = 0.0
        self._jsonl_file = None
        self._jsonl_path = None
        self._logger: logging.Logger | None = None
        self._log_path = None
        self._lock = threading.Lock()
        self._categories_filter: set[str] | None = None  # None = todas
        self._opened_date: str | None = None

    # ── Setup ────────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            if enabled and not self.enabled:
                self._open(reopen=False)
            elif not enabled and self.enabled:
                self._close()
            self.enabled = bool(enabled)

    def set_categories_filter(self, categories: list[str] | None) -> None:
        """Solo loguea categorias indicadas. None = sin filtro."""
        with self._lock:
            self._categories_filter = set(c.upper() for c in categories) if categories else None

    def _open(self, reopen: bool) -> None:
        _ensure_log_dir()
        date = time.strftime("%Y%m%d")
        self._opened_date = date

        # 1) Logger rotativo
        log_path = os.path.join(_LOG_DIR, f"combat-{date}.log")
        logger = logging.getLogger("dofus.combat")
        # Limpia handlers viejos al reabrir (cambio de dia)
        for h in list(logger.handlers):
            if getattr(h, "_combat_managed", False):
                try:
                    h.close()
                except Exception:
                    pass
                logger.removeHandler(h)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False  # evitar duplicar en root logger
        try:
            handler = RotatingFileHandler(
                log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter(
                "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
                datefmt="%H:%M:%S",
            ))
            handler._combat_managed = True
            logger.addHandler(handler)
        except Exception as exc:
            print(f"[TELEMETRY] No se pudo abrir log rotativo: {exc!r}")
        self._logger = logger
        self._log_path = log_path

        # 2) JSONL emitter
        jsonl_path = os.path.join(_LOG_DIR, f"combat-{date}.jsonl")
        try:
            if self._jsonl_file:
                try:
                    self._jsonl_file.close()
                except Exception:
                    pass
            self._jsonl_file = open(jsonl_path, "a", encoding="utf-8", buffering=1)
            self._jsonl_path = jsonl_path
        except Exception as exc:
            print(f"[TELEMETRY] No se pudo abrir JSONL: {exc!r}")
            self._jsonl_file = None

    def _close(self) -> None:
        if self._jsonl_file is not None:
            try:
                self._jsonl_file.close()
            except Exception:
                pass
            self._jsonl_file = None
        if self._logger is not None:
            for h in list(self._logger.handlers):
                if getattr(h, "_combat_managed", False):
                    try:
                        h.close()
                    except Exception:
                        pass
                    self._logger.removeHandler(h)

    def _maybe_rotate_for_day(self) -> None:
        """Si cambio el dia desde que se abrieron los archivos, reabrir."""
        if not self.enabled:
            return
        today = time.strftime("%Y%m%d")
        if self._opened_date is not None and today != self._opened_date:
            self._open(reopen=True)

    # ── Fight lifecycle ──────────────────────────────────────────────────

    def start_fight(self, fight_id) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._fight_id = fight_id
            self._turn_number = 0
            self._fight_started_at = time.time()
        self.emit("fight_start", fight_id=fight_id)

    def end_fight(self, **payload) -> None:
        if not self.enabled:
            return
        duration = time.time() - self._fight_started_at if self._fight_started_at else None
        self.emit("fight_end", duration_s=duration, turns=self._turn_number, **payload)
        with self._lock:
            self._fight_id = None
            self._turn_number = 0
            self._fight_started_at = 0.0

    def set_turn(self, turn_number: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._turn_number = int(turn_number)

    @property
    def fight_id(self):
        return self._fight_id

    @property
    def turn_number(self) -> int:
        return self._turn_number

    # ── Emisores ─────────────────────────────────────────────────────────

    def emit(self, kind: str, **payload) -> None:
        """Escribe un record JSONL. No bloquea si esta deshabilitado."""
        if not self.enabled or self._jsonl_file is None:
            return
        self._maybe_rotate_for_day()
        record = {
            "ts": round(time.time(), 4),
            "kind": kind,
            "fight_id": self._fight_id,
            "turn": self._turn_number,
        }
        record.update(payload)
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except Exception as exc:
            line = json.dumps({
                "ts": record["ts"], "kind": "telemetry_error",
                "error": repr(exc), "kind_attempted": kind,
            })
        try:
            with self._lock:
                if self._jsonl_file is not None:
                    self._jsonl_file.write(line + "\n")
        except Exception:
            pass

    # ── Logger conveniences ──────────────────────────────────────────────

    def _log(self, level: int, category: str, message: str, **kwargs) -> None:
        if not self.enabled or self._logger is None:
            return
        cat = (category or "").upper()
        if self._categories_filter is not None and cat not in self._categories_filter:
            return
        prefix = f"[{cat}]" if cat else ""
        ctx = ""
        if self._fight_id is not None:
            ctx = f" (f={self._fight_id} t={self._turn_number})"
        try:
            self._logger.log(level, f"{prefix}{ctx} {message}")
        except Exception:
            pass
        # Tambien emitir a JSONL si nos pasaron payload extra
        if kwargs:
            self.emit("log", level=logging.getLevelName(level),
                       category=cat, message=message, **kwargs)

    def debug(self, category: str, message: str, **kwargs) -> None:
        self._log(logging.DEBUG, category, message, **kwargs)

    def info(self, category: str, message: str, **kwargs) -> None:
        self._log(logging.INFO, category, message, **kwargs)

    def warn(self, category: str, message: str, **kwargs) -> None:
        self._log(logging.WARNING, category, message, **kwargs)

    def error(self, category: str, message: str, **kwargs) -> None:
        self._log(logging.ERROR, category, message, **kwargs)

    # ── Introspeccion ────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "log_path": self._log_path,
            "jsonl_path": self._jsonl_path,
            "fight_id": self._fight_id,
            "turn_number": self._turn_number,
            "categories_filter": sorted(self._categories_filter) if self._categories_filter else None,
        }


# ── Singleton ────────────────────────────────────────────────────────────

_INSTANCE: CombatTelemetry | None = None
_INSTANCE_LOCK = threading.Lock()


def get_telemetry() -> CombatTelemetry:
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = CombatTelemetry()
    return _INSTANCE


def configure_from_dict(cfg: dict) -> CombatTelemetry:
    """Configura el singleton desde un dict (tipicamente `config['bot']`)."""
    tel = get_telemetry()
    enabled = bool(cfg.get("combat_telemetry", False))
    tel.set_enabled(enabled)
    cats = cfg.get("combat_telemetry_categories")
    if cats:
        if isinstance(cats, str):
            cats = [c.strip() for c in cats.split(",") if c.strip()]
        tel.set_categories_filter(list(cats))
    else:
        tel.set_categories_filter(None)
    return tel
