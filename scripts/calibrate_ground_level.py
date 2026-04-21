"""Calibracion exacta de px_per_ground_level para visual_grid_global.

Uso:
    python scripts/calibrate_ground_level.py [map_id]

Pide al usuario apuntar el mouse sobre 2 celdas con distinto ground_level,
captura las posiciones reales y calcula el ratio px/nivel para corregir
la proyeccion isometrica en _project_cell_with_visual_grid.
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

import mss
import pyautogui
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dofus_map_data import decode_map_data, load_map_data_from_xml  # noqa: E402


def load_config() -> dict:
    with (ROOT / "config.yaml").open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def merged_settings(cfg: dict, map_id: int) -> dict:
    """Replica _visual_grid_settings_for_map: global + by_map_id."""
    cal = cfg.get("bot", {}).get("cell_calibration", {})
    global_base = dict(cal.get("visual_grid_global") or {})
    by_map = (cal.get("visual_grid_by_map_id") or {})
    raw = by_map.get(str(map_id)) or by_map.get(map_id) or {}
    merged = dict(global_base)
    if isinstance(raw, dict):
        merged.update(raw)
    return merged


def predicted_screen(cell, settings: dict, monitor: dict) -> tuple[float, float]:
    """Replica _project_cell_with_visual_grid sin correccion de ground_level."""
    saved_w = max(float(settings.get("canvas_width") or monitor["width"]), 1.0)
    scale_x = float(monitor["width"]) / saved_w
    cell_width = float(settings["cell_width"]) * scale_x
    cell_height = cell_width / 2.0
    offset_x = float(settings["offset_x"]) * scale_x
    saved_h = max(float(settings.get("canvas_height") or monitor["height"]), 1.0)
    scale_y = float(monitor["height"]) / saved_h
    offset_y = float(settings["offset_y"]) * scale_y
    mid_w = cell_width / 2.0
    mid_h = cell_height / 2.0
    iso_x = (cell.x - cell.y) * mid_w
    iso_y = (cell.x + cell.y) * mid_h
    cx = offset_x + iso_x + mid_w
    cy = offset_y + iso_y + mid_h
    return monitor["left"] + cx, monitor["top"] + cy


def pick_monitor(monitors: list, sample_xy: tuple[int, int]) -> int:
    """Devuelve indice (1-based) del monitor que contiene el punto."""
    sx, sy = sample_xy
    for i, m in enumerate(monitors[1:], start=1):
        if m["left"] <= sx < m["left"] + m["width"] and m["top"] <= sy < m["top"] + m["height"]:
            return i
    return 1


def capture_mouse(label: str, cell, predicted: tuple[float, float]) -> tuple[int, int]:
    print(f"\n--- Celda {label}: id={cell.cell_id} (x={cell.x}, y={cell.y}, gl={cell.ground_level}) ---")
    print(f"  Posicion predicha (sin correccion): ({predicted[0]:.0f}, {predicted[1]:.0f})")
    print(f"  Mueve el mouse al CENTRO real de la celda en el juego.")
    print(f"  Tienes 6 segundos antes de capturar...")
    for remaining in (5, 4, 3, 2, 1):
        time.sleep(1)
        x, y = pyautogui.position()
        print(f"    {remaining}s -> mouse en ({x}, {y})")
    time.sleep(1)
    x, y = pyautogui.position()
    print(f"  CAPTURADO -> ({x}, {y})")
    return x, y


_LOG_LINES: list[str] = []


def _log(msg: str = "") -> None:
    print(msg)
    _LOG_LINES.append(msg)


def main() -> int:
    cfg = load_config()
    cal = cfg.get("bot", {}).get("cell_calibration", {})
    settings = dict(cal.get("visual_grid_global") or {})
    if not settings:
        print("[ERROR] No existe bot.cell_calibration.visual_grid_global en config.yaml")
        return 1
    xml_dir = cal.get("local_map_xml_dir")
    if not xml_dir:
        print("[ERROR] Falta bot.cell_calibration.local_map_xml_dir en config.yaml")
        return 1

    map_id_arg = sys.argv[1] if len(sys.argv) > 1 else input("map_id (ej. 6370): ").strip()
    try:
        map_id = int(map_id_arg)
    except ValueError:
        print(f"[ERROR] map_id invalido: {map_id_arg!r}")
        return 1

    loaded = load_map_data_from_xml(map_id, xml_dir)
    if loaded is None:
        print(f"[ERROR] No se pudo cargar XML del mapa {map_id} en {xml_dir}")
        return 1
    map_data, width, _height = loaded
    cells = decode_map_data(map_data, map_width=width)
    walkable = [c for c in cells if c.is_walkable]
    gl_counter = Counter(c.ground_level for c in walkable)
    print(f"\nMapa {map_id}: {len(cells)} celdas totales, {len(walkable)} caminables")
    print(f"Distribucion ground_level (caminables): {dict(sorted(gl_counter.items()))}")
    print("\nElige 2 celdas con DISTINTO ground_level y POSICION VISIBLE en pantalla.")
    print("Sugerencia: una con gl bajo y otra con gl alto del mapa actual.")

    with mss.mss() as sct:
        monitor = dict(sct.monitors[1])
        print(f"\nMonitor activo: {monitor['width']}x{monitor['height']} @ ({monitor['left']},{monitor['top']})")

        cell_a_id = int(input("\nID celda A: ").strip())
        cell_b_id = int(input("ID celda B: ").strip())

        cell_a = next((c for c in cells if c.cell_id == cell_a_id), None)
        cell_b = next((c for c in cells if c.cell_id == cell_b_id), None)
        if cell_a is None or cell_b is None:
            print("[ERROR] cell_id no encontrado en el XML")
            return 1
        if cell_a.ground_level == cell_b.ground_level:
            print(f"[ERROR] Ambas celdas tienen el mismo ground_level ({cell_a.ground_level}). Elige distintos niveles.")
            return 1

        pred_a = predicted_screen(cell_a, settings, monitor)
        pred_b = predicted_screen(cell_b, settings, monitor)

        real_a = capture_mouse("A", cell_a, pred_a)
        real_b = capture_mouse("B", cell_b, pred_b)

    delta_y_a = pred_a[1] - real_a[1]
    delta_y_b = pred_b[1] - real_b[1]
    delta_x_a = pred_a[0] - real_a[0]
    delta_x_b = pred_b[0] - real_b[0]

    _log("\n=== Resultados ===")
    _log(f"A: gl={cell_a.ground_level} pred=({pred_a[0]:.0f},{pred_a[1]:.0f}) real={real_a} delta=({delta_x_a:+.0f},{delta_y_a:+.0f})")
    _log(f"B: gl={cell_b.ground_level} pred=({pred_b[0]:.0f},{pred_b[1]:.0f}) real={real_b} delta=({delta_x_b:+.0f},{delta_y_b:+.0f})")

    diff_y = delta_y_a - delta_y_b
    diff_gl = cell_a.ground_level - cell_b.ground_level
    px_per_level = diff_y / diff_gl

    ref_gl = cell_b.ground_level
    ref_pred_y_corrected = pred_b[1] - (cell_b.ground_level - ref_gl) * px_per_level
    residual_y = ref_pred_y_corrected - real_b[1]

    _log(f"\npx_per_ground_level = ({delta_y_a:+.1f} - {delta_y_b:+.1f}) / ({cell_a.ground_level} - {cell_b.ground_level})")
    _log(f"                    = {px_per_level:.3f} px/nivel")
    _log(f"\nUsando ref_ground_level = {ref_gl} (gl de celda B)")
    _log(f"Residual vertical sobre B con la correccion: {residual_y:+.1f}px")

    suggested_offset_y = float(settings.get("offset_y", 0)) - residual_y
    _log(f"\n>>> Sugerencia para config.yaml -> bot.cell_calibration.visual_grid_global:")
    _log(f"    px_per_ground_level: {px_per_level:.3f}")
    _log(f"    ref_ground_level: {ref_gl}")
    if abs(residual_y) > 1:
        _log(f"    # Si quieres centrar en B exactamente, ajusta offset_y -> {suggested_offset_y:.2f}")

    out_path = ROOT / "logs" / "calibrate_ground_level.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(_LOG_LINES) + "\n", encoding="utf-8")
    print(f"\n[OK] Resultados guardados en: {out_path}")

    try:
        input("\nPresiona ENTER para salir...")
    except EOFError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
