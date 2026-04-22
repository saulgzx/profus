# Tests

Suite de pytest para `dofus-autofarm`.

## Correr

```bash
pip install -r requirements.txt   # incluye pytest y pytest-mock
pytest tests/ -v
```

Tiempo esperado: <1s para los 30 tests actuales.

## Estructura

| Archivo | Propósito |
|---|---|
| `conftest.py` | Agrega `src/` al `sys.path`. |
| `test_perf.py` | Valida el módulo `perf.py`: spans, marks, points, packet capture. |
| `test_sniffer_parser.py` | Tests de **caracterización** del parser del sniffer. Documentan el comportamiento actual (no necesariamente "correcto"). Si rompen tras un refactor, evaluar si el cambio es intencional. |
| `test_packet_replay.py` | Carga un fixture `.jsonl` de paquetes y los pasa por el parser. Base para reproducir bugs offline (ej: el del Duna Yar). |
| `fixtures/sample_packets.jsonl` | Fixture sintético — secuencia exitosa Duna Yar → BN → Gp → GIC. |

## Cómo capturar fixtures reales

Para grabar una pelea real y agregarla como fixture:

1. En `config.yaml`, setear:
   ```yaml
   bot:
     perf_enabled: true
     perf_packet_capture: true
   ```
2. Correr el bot durante una pelea.
3. El archivo `logs/packets-YYYYMMDD.jsonl` contiene todos los paquetes con `t_recv` / `t_parsed`.
4. Copiar el rango relevante a `tests/fixtures/<nombre>.jsonl` (filtrar con `jq` si hace falta).

## Análisis de performance

```bash
python scripts/analyze_perf.py logs/perf-YYYYMMDD.jsonl
python scripts/analyze_perf.py logs/perf-YYYYMMDD.jsonl --label placement.click_to_confirm
python scripts/analyze_perf.py logs/packets-YYYYMMDD.jsonl --packets
```

Muestra p50/p95/p99 de cada operación medida y desglose por tipo de paquete.
