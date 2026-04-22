"""
Test de replay: lee un fixture de packets-*.jsonl y los pasa por el
parser del sniffer para validar que no rompe.

Esto es la base para reproducir bugs (como el del Duna Yar) sin tener
Dofus abierto.
"""
import json
import os

import sniffer as sn

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_packets.jsonl")


def _load_fixture(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_fixture_loads():
    records = _load_fixture(FIXTURE)
    assert len(records) >= 5
    assert all("data" in r and "dir" in r for r in records)


def test_fixture_dunayar_sequence_present():
    """El fixture contiene la secuencia exitosa Duna Yar → BN → Gp165 → GIC."""
    records = _load_fixture(FIXTURE)
    datas = [r["data"] for r in records]
    assert "OU24608846|||1" in datas       # uso del Duna Yar
    assert "BN" in datas                   # ack del cliente
    assert "Gp165" in datas                # placement enviado
    assert "GIC|22240;165;1" in datas      # confirmación servidor


def test_replay_through_parser_does_not_crash():
    """Pasar todos los packets del fixture por el parser correspondiente
    no debe lanzar excepción.
    """
    records = _load_fixture(FIXTURE)
    for rec in records:
        data = rec["data"]
        # Smoke: aplicar el parser que correspondería según prefijo
        if data.startswith("Im"):
            sn._parse_info_msg(data[2:])
        elif data.startswith("Gp") and len(data) > 2 and not data[2].isdigit():
            # Gp con hashes (placement cells)
            sn._parse_placement_cells(data[2:])
        elif data.startswith("GTM"):
            sn._parse_gtm(data[3:])
        elif data.startswith("As"):
            sn._parse_as(data[2:])
        # Otros (GIC, GTS, BN, OU, Gp<num>) no tienen parser standalone
        # — los maneja _parse_server_packet/client. No los testamos aquí.


def test_packet_intervals_are_reasonable():
    """En la secuencia exitosa, GIC llega <1s después de Gp."""
    records = _load_fixture(FIXTURE)
    gp_ts = next((r["ts"] for r in records if r["data"].startswith("Gp1")), None)
    gic_ts = next((r["ts"] for r in records if r["data"].startswith("GIC")), None)
    assert gp_ts is not None and gic_ts is not None
    assert 0 < (gic_ts - gp_ts) < 1.0  # esperado: confirm <1s
