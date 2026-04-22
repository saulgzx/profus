"""
perf.py — Instrumentación de rendimiento y latencia.

Diseño:
  * Overhead mínimo cuando está deshabilitado (todo no-op).
  * Nunca modifica la lógica del bot: solo observa y registra.
  * Registra a `logs/perf-YYYYMMDD.jsonl` cuando `enabled=True`.
  * Provee también un modo "packet capture" que graba cada paquete del sniffer
    con timestamp a `logs/packets-YYYYMMDD.jsonl` para replay offline.

Uso:

    from perf import get_perf
    perf = get_perf()
    perf.set_enabled(True)
    perf.set_packet_capture(True)          # opcional, genera fixtures de sniffer

    # Medir duración de un bloque:
    with perf.measure("placement.retry_loop", map_id=2966, target=165):
        ...

    # Medir latencia entre dos eventos separados:
    tid = perf.mark("placement.click_sent", target=165)
    # ... cuando llega la confirmación:
    perf.mark_end(tid, result="ok", to_cell=165)

    # Record puntual (sin duración):
    perf.point("sniffer.packet_dropped", reason="invalid_utf8")

    # Capturar un paquete del sniffer (si packet_capture está activo):
    perf.record_packet(direction="S→C", data="GIC|22240;165;1",
                       t_recv=t_recv, t_parsed=t_parsed)

API interna del bot: `get_perf()` devuelve un singleton thread-safe.

El overhead cuando `enabled=False`:
  - `measure()`   → context manager que solo toma un `time.perf_counter()` al
                     entrar si ya está deshabilitado, sale inmediatamente.
  - `mark()`      → retorna `None` sin tocar archivos.
  - `point()`     → return inmediato.
  - `record_packet()` → return inmediato.

Ver `scripts/analyze_perf.py` para análisis offline.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager


_LOG_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")


def _ensure_log_dir() -> None:
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
    except Exception:
        pass


class PerfRecorder:
    """Registrador de timings y paquetes. Singleton-style, thread-safe."""

    def __init__(self) -> None:
        self.enabled: bool = False
        self.packet_capture: bool = False

        self._perf_file = None
        self._perf_path: str | None = None
        self._packet_file = None
        self._packet_path: str | None = None

        self._lock = threading.Lock()
        self._opened_date: str | None = None

        # mark_id -> (label, t_start, payload) para medir latencias asíncronas
        self._marks: dict[int, tuple[str, float, dict]] = {}
        self._next_mark_id: int = 1
        self._marks_lock = threading.Lock()

    # ── Setup ────────────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            if enabled and not self.enabled:
                self._open_perf()
            elif not enabled and self.enabled:
                self._close_perf()
            self.enabled = bool(enabled)

    def set_packet_capture(self, enabled: bool) -> None:
        with self._lock:
            if enabled and not self.packet_capture:
                self._open_packets()
            elif not enabled and self.packet_capture:
                self._close_packets()
            self.packet_capture = bool(enabled)

    def _open_perf(self) -> None:
        _ensure_log_dir()
        date = time.strftime("%Y%m%d")
        self._opened_date = date
        path = os.path.join(_LOG_DIR, f"perf-{date}.jsonl")
        try:
            self._perf_file = open(path, "a", encoding="utf-8", buffering=1)
            self._perf_path = path
        except Exception as exc:
            print(f"[PERF] No se pudo abrir {path}: {exc!r}")
            self._perf_file = None

    def _close_perf(self) -> None:
        if self._perf_file is not None:
            try:
                self._perf_file.close()
            except Exception:
                pass
            self._perf_file = None

    def _open_packets(self) -> None:
        _ensure_log_dir()
        date = time.strftime("%Y%m%d")
        path = os.path.join(_LOG_DIR, f"packets-{date}.jsonl")
        try:
            self._packet_file = open(path, "a", encoding="utf-8", buffering=1)
            self._packet_path = path
        except Exception as exc:
            print(f"[PERF] No se pudo abrir {path}: {exc!r}")
            self._packet_file = None

    def _close_packets(self) -> None:
        if self._packet_file is not None:
            try:
                self._packet_file.close()
            except Exception:
                pass
            self._packet_file = None

    def _maybe_rotate_for_day(self) -> None:
        if not self.enabled:
            return
        today = time.strftime("%Y%m%d")
        if self._opened_date is not None and today != self._opened_date:
            if self.enabled:
                self._close_perf()
                self._open_perf()
            if self.packet_capture:
                self._close_packets()
                self._open_packets()

    # ── Emisión interna ──────────────────────────────────────────────────

    def _write_perf(self, record: dict) -> None:
        if not self.enabled or self._perf_file is None:
            return
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except Exception as exc:
            line = json.dumps({
                "ts": record.get("ts", time.time()),
                "kind": "perf_serialize_error",
                "error": repr(exc),
            })
        try:
            with self._lock:
                if self._perf_file is not None:
                    self._perf_file.write(line + "\n")
        except Exception:
            pass

    def _write_packet(self, record: dict) -> None:
        if not self.packet_capture or self._packet_file is None:
            return
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except Exception:
            return
        try:
            with self._lock:
                if self._packet_file is not None:
                    self._packet_file.write(line + "\n")
        except Exception:
            pass

    # ── API pública ──────────────────────────────────────────────────────

    @contextmanager
    def measure(self, label: str, **payload):
        """Context manager que mide la duración de un bloque.

        El bloque corre siempre. Si `enabled=False`, el overhead es solo
        dos llamadas a `time.perf_counter()` y un `yield`.
        """
        if not self.enabled:
            yield
            return
        self._maybe_rotate_for_day()
        t0 = time.perf_counter()
        t_wall = time.time()
        try:
            yield
        finally:
            dur_ms = (time.perf_counter() - t0) * 1000.0
            record = {
                "ts": round(t_wall, 6),
                "kind": "span",
                "label": label,
                "dur_ms": round(dur_ms, 3),
            }
            if payload:
                record.update(payload)
            self._write_perf(record)

    def mark(self, label: str, **payload) -> int | None:
        """Registra el inicio de una operación asíncrona.

        Devuelve un `mark_id` que se usa después en `mark_end`. Si está
        deshabilitado devuelve `None`.
        """
        if not self.enabled:
            return None
        self._maybe_rotate_for_day()
        with self._marks_lock:
            mid = self._next_mark_id
            self._next_mark_id += 1
            self._marks[mid] = (label, time.perf_counter(), dict(payload or {}))
        return mid

    def mark_end(self, mark_id: int | None, **extra) -> None:
        """Completa un mark y escribe la latencia."""
        if mark_id is None or not self.enabled:
            return
        with self._marks_lock:
            entry = self._marks.pop(mark_id, None)
        if entry is None:
            return
        label, t0, payload = entry
        dur_ms = (time.perf_counter() - t0) * 1000.0
        record = {
            "ts": round(time.time(), 6),
            "kind": "mark",
            "label": label,
            "dur_ms": round(dur_ms, 3),
        }
        if payload:
            record.update(payload)
        if extra:
            record.update(extra)
        self._write_perf(record)

    def point(self, label: str, **payload) -> None:
        """Registra un evento puntual sin duración."""
        if not self.enabled:
            return
        self._maybe_rotate_for_day()
        record = {
            "ts": round(time.time(), 6),
            "kind": "point",
            "label": label,
        }
        if payload:
            record.update(payload)
        self._write_perf(record)

    def record_packet(self, direction: str, data: str,
                      t_recv: float | None = None,
                      t_parsed: float | None = None,
                      **extra) -> None:
        """Graba un paquete del sniffer al fixture de replay.

        `t_recv`   = timestamp de cuando scapy entregó el paquete.
        `t_parsed` = timestamp de cuando el parser terminó de procesarlo.
        La diferencia mide el overhead de parsing.
        """
        if not self.packet_capture:
            return
        self._maybe_rotate_for_day()
        now = time.time()
        record = {
            "ts": round(now, 6),
            "dir": direction,
            "data": data,
        }
        if t_recv is not None:
            record["t_recv"] = round(t_recv, 6)
        if t_parsed is not None:
            record["t_parsed"] = round(t_parsed, 6)
            if t_recv is not None:
                record["parse_ms"] = round((t_parsed - t_recv) * 1000.0, 3)
        if extra:
            record.update(extra)
        self._write_packet(record)

    # ── Introspección ───────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "packet_capture": self.packet_capture,
            "perf_path": self._perf_path,
            "packet_path": self._packet_path,
            "open_marks": len(self._marks),
        }


# ── Singleton ─────────────────────────────────────────────────────────────

_INSTANCE: PerfRecorder | None = None
_INSTANCE_LOCK = threading.Lock()


def get_perf() -> PerfRecorder:
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = PerfRecorder()
    return _INSTANCE


def configure_from_dict(cfg: dict) -> PerfRecorder:
    """Configura el singleton desde `config['bot']`.

    Claves reconocidas:
      * `perf_enabled`       (bool, default False)
      * `perf_packet_capture` (bool, default False) — graba fixture del sniffer
    """
    perf = get_perf()
    perf.set_enabled(bool(cfg.get("perf_enabled", False)))
    perf.set_packet_capture(bool(cfg.get("perf_packet_capture", False)))
    return perf
