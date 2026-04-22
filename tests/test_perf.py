"""
Tests del módulo `perf` — mediciones básicas con overhead nulo cuando
está deshabilitado.

Ejecutar: pytest tests/test_perf.py -v
"""
import json
import os
import tempfile
import time

import perf


def _fresh_recorder(log_dir: str) -> perf.PerfRecorder:
    """Fabrica un PerfRecorder apuntando a un log_dir temporal."""
    rec = perf.PerfRecorder()
    # Redirigir el log_dir privado usando monkey-patching del módulo.
    # `_LOG_DIR` se lee cuando se abre el archivo, así que cambiamos ahí.
    orig = perf._LOG_DIR
    perf._LOG_DIR = log_dir
    try:
        rec.set_enabled(True)
    finally:
        perf._LOG_DIR = orig
    return rec


def _read_jsonl(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ─────────────────────────────────────────── API básica ──

def test_disabled_is_noop():
    rec = perf.PerfRecorder()
    # Sin enable: todas las llamadas son no-op y no explotan
    assert rec.mark("some_op") is None
    rec.mark_end(None)
    rec.point("event")
    rec.record_packet("S→C", "GIC|1;2;3")
    with rec.measure("span"):
        pass
    assert rec.enabled is False


def test_measure_context_emits_span(tmp_path):
    rec = _fresh_recorder(str(tmp_path))
    try:
        with rec.measure("placement.retry", map_id=2966, target=165):
            time.sleep(0.01)
    finally:
        rec.set_enabled(False)

    files = list(tmp_path.glob("perf-*.jsonl"))
    assert len(files) == 1
    records = _read_jsonl(str(files[0]))
    assert len(records) == 1
    r = records[0]
    assert r["kind"] == "span"
    assert r["label"] == "placement.retry"
    assert r["map_id"] == 2966
    assert r["target"] == 165
    assert r["dur_ms"] >= 8  # sleep 10ms, tolerancia baja


def test_mark_mark_end_latency(tmp_path):
    rec = _fresh_recorder(str(tmp_path))
    try:
        mid = rec.mark("placement.click", attempt=0)
        assert isinstance(mid, int)
        time.sleep(0.005)
        rec.mark_end(mid, result="ok", landed_cell=165)
    finally:
        rec.set_enabled(False)

    files = list(tmp_path.glob("perf-*.jsonl"))
    records = _read_jsonl(str(files[0]))
    assert len(records) == 1
    r = records[0]
    assert r["kind"] == "mark"
    assert r["label"] == "placement.click"
    assert r["attempt"] == 0
    assert r["result"] == "ok"
    assert r["landed_cell"] == 165
    assert r["dur_ms"] >= 3


def test_mark_end_with_unknown_id_is_safe():
    rec = perf.PerfRecorder()
    rec.set_enabled(True)
    try:
        rec.mark_end(99999)  # no se rompe
    finally:
        rec.set_enabled(False)


def test_point_emits_record(tmp_path):
    rec = _fresh_recorder(str(tmp_path))
    try:
        rec.point("sniffer.packet_dropped", reason="invalid_utf8", seq=3)
    finally:
        rec.set_enabled(False)

    records = _read_jsonl(str(list(tmp_path.glob("perf-*.jsonl"))[0]))
    assert len(records) == 1
    assert records[0]["kind"] == "point"
    assert records[0]["reason"] == "invalid_utf8"
    assert records[0]["seq"] == 3


def test_packet_capture_writes_separate_file(tmp_path):
    rec = perf.PerfRecorder()
    orig = perf._LOG_DIR
    perf._LOG_DIR = str(tmp_path)
    try:
        rec.set_packet_capture(True)
        t0 = time.time()
        rec.record_packet("S→C", "GIC|22240;165;1",
                          t_recv=t0, t_parsed=t0 + 0.0003)
    finally:
        rec.set_packet_capture(False)
        perf._LOG_DIR = orig

    files = list(tmp_path.glob("packets-*.jsonl"))
    assert len(files) == 1
    records = _read_jsonl(str(files[0]))
    assert len(records) == 1
    r = records[0]
    assert r["dir"] == "S→C"
    assert r["data"] == "GIC|22240;165;1"
    assert r["parse_ms"] is not None
    # parse_ms fue ~0.3ms
    assert 0.2 <= r["parse_ms"] <= 1.0


def test_singleton_get_perf_returns_same():
    a = perf.get_perf()
    b = perf.get_perf()
    assert a is b


def test_configure_from_dict_sets_flags():
    cfg = {"perf_enabled": True, "perf_packet_capture": False}
    rec = perf.configure_from_dict(cfg)
    try:
        assert rec.enabled is True
        assert rec.packet_capture is False
    finally:
        rec.set_enabled(False)
        rec.set_packet_capture(False)
