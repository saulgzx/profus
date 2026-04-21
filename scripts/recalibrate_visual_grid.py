"""Recalibracion manual de visual_grid_by_map_id usando world_map_samples.

Uso:
    # Ver fit propuesto (no escribe nada):
    python scripts/recalibrate_visual_grid.py 6379

    # Aplicar fit a config.yaml:
    python scripts/recalibrate_visual_grid.py 6379 --apply

    # Aplicar y lockear (auto-fit del bot ya no tocara este mapa):
    python scripts/recalibrate_visual_grid.py 6379 --apply --lock

    # Solo togglear el lock (sin recalcular):
    python scripts/recalibrate_visual_grid.py 6379 --lock-only
    python scripts/recalibrate_visual_grid.py 6379 --unlock

Requiere que el bot haya acumulado samples para ese map_id en
`bot.cell_calibration.world_map_samples_by_map_id`. Cada movimiento confirmado
durante combate genera un sample automaticamente.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from map_logic import cell_id_to_grid  # noqa: E402

CONFIG_PATH = ROOT / "config.yaml"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def save_config(cfg: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)


def get_samples(cfg: dict, map_id: int) -> list[dict]:
    cal = cfg.get("bot", {}).get("cell_calibration", {})
    by_map = cal.get("world_map_samples_by_map_id", {}) or {}
    samples = by_map.get(str(map_id))
    if samples is None:
        samples = by_map.get(map_id, [])
    if not isinstance(samples, list):
        return []
    # Dedupe por cell_id quedandose con el mas reciente
    by_cell: dict[int, dict] = {}
    for s in samples:
        if not isinstance(s, dict):
            continue
        try:
            cid = int(s.get("cell_id"))
        except (TypeError, ValueError):
            continue
        prev = by_cell.get(cid)
        if prev is None:
            by_cell[cid] = s
            continue
        try:
            t_new = float(s.get("saved_at", 0.0) or 0.0)
            t_old = float(prev.get("saved_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            t_new, t_old = 0.0, 0.0
        if t_new >= t_old:
            by_cell[cid] = s
    return list(by_cell.values())


def fit_visual_grid(samples: list[dict], map_width: int, monitor: dict) -> dict | None:
    """Fittea cell_width/offset_x/offset_y por regresion lineal."""
    mon_left = float(monitor.get("left", 0))
    mon_top = float(monitor.get("top", 0))
    xs_x, xs_rhs = [], []
    ys_y, ys_rhs = [], []
    used_cells: list[tuple[int, int, int, int, int]] = []  # (cell_id, gx, gy, sx, sy)
    for s in samples:
        try:
            cid = int(s.get("cell_id"))
            sx = float(s.get("screen_x")) - mon_left
            sy = float(s.get("screen_y")) - mon_top
        except (TypeError, ValueError):
            continue
        gx, gy = cell_id_to_grid(cid, map_width)
        xs_x.append([1.0, float(gx - gy)])
        xs_rhs.append(sx)
        ys_y.append([1.0, float(gx + gy)])
        ys_rhs.append(sy)
        used_cells.append((cid, gx, gy, int(sx + mon_left), int(sy + mon_top)))
    if len(xs_x) < 3:
        return None
    Mx = np.array(xs_x, dtype=float)
    My = np.array(ys_y, dtype=float)
    if np.linalg.matrix_rank(Mx) < 2 or np.linalg.matrix_rank(My) < 2:
        return None
    cx, _, _, _ = np.linalg.lstsq(Mx, np.array(xs_rhs, dtype=float), rcond=None)
    cy, _, _, _ = np.linalg.lstsq(My, np.array(ys_rhs, dtype=float), rcond=None)
    bias_x, slope_x = float(cx[0]), float(cx[1])
    bias_y, slope_y = float(cy[0]), float(cy[1])
    if slope_x <= 0 or slope_y <= 0:
        return None
    cell_width = 2.0 * slope_x
    cell_height = 2.0 * slope_y
    offset_x = bias_x - slope_x
    offset_y = bias_y - slope_y
    # RMSE
    residuals = []
    for cid, gx, gy, sx_abs, sy_abs in used_cells:
        sx = sx_abs - mon_left
        sy = sy_abs - mon_top
        pred_x = bias_x + slope_x * (gx - gy)
        pred_y = bias_y + slope_y * (gx + gy)
        residuals.append((pred_x - sx, pred_y - sy))
    rmse = float(np.sqrt(np.mean([rx * rx + ry * ry for rx, ry in residuals]))) if residuals else -1.0
    # Detalle por cell
    detail = []
    for (cid, gx, gy, sx_abs, sy_abs), (rx, ry) in zip(used_cells, residuals):
        detail.append({
            "cell_id": cid,
            "grid_xy": (gx, gy),
            "click": (sx_abs, sy_abs),
            "residual": (round(rx, 2), round(ry, 2)),
            "err_px": round((rx * rx + ry * ry) ** 0.5, 2),
        })
    return {
        "cell_width": round(cell_width, 2),
        "cell_height": round(cell_height, 2),
        "offset_x": round(offset_x, 2),
        "offset_y": round(offset_y, 2),
        "canvas_width": round(float(monitor.get("width", 0)), 1),
        "canvas_height": round(float(monitor.get("height", 0)), 1),
        "rmse": round(rmse, 2),
        "samples_used": len(used_cells),
        "detail": detail,
    }


def detect_monitor(cfg: dict) -> dict:
    """Devuelve el monitor segun config (o por defecto pantalla primaria via mss)."""
    try:
        import mss
        with mss.mss() as sct:
            mons = sct.monitors
            cfg_mon = cfg.get("bot", {}).get("monitor_index", 1)
            try:
                idx = int(cfg_mon)
            except (TypeError, ValueError):
                idx = 1
            if idx < 0 or idx >= len(mons):
                idx = 1
            return dict(mons[idx])
    except ImportError:
        # Fallback: asumir monitor primario "tipico"
        return {"left": 0, "top": 0, "width": 1920, "height": 1080}


def get_current_entry(cfg: dict, map_id: int) -> dict:
    cal = cfg.get("bot", {}).get("cell_calibration", {})
    by_map = cal.get("visual_grid_by_map_id", {}) or {}
    entry = by_map.get(str(map_id)) or by_map.get(map_id) or {}
    return dict(entry) if isinstance(entry, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("map_id", type=int, help="map_id a recalibrar")
    parser.add_argument("--apply", action="store_true", help="Escribir el fit a config.yaml")
    parser.add_argument("--lock", action="store_true", help="Setear manual_lock=true tras aplicar")
    parser.add_argument("--lock-only", action="store_true", help="Solo lockear (no recalcular)")
    parser.add_argument("--unlock", action="store_true", help="Quitar manual_lock")
    parser.add_argument("--map-width", type=int, default=None, help="Override map_width (default: del config)")
    args = parser.parse_args()

    cfg = load_config()
    cal = cfg.setdefault("bot", {}).setdefault("cell_calibration", {})
    by_map = cal.setdefault("visual_grid_by_map_id", {})
    map_id = args.map_id

    # Toggles puros
    if args.lock_only or args.unlock:
        entry = get_current_entry(cfg, map_id)
        if not entry:
            print(f"[ERR] No existe entry para map_id={map_id} en visual_grid_by_map_id.")
            return 1
        if args.unlock:
            entry.pop("manual_lock", None)
            print(f"[OK] manual_lock REMOVIDO para map={map_id}.")
        else:
            entry["manual_lock"] = True
            print(f"[OK] manual_lock=True para map={map_id}.")
        by_map[str(map_id)] = entry
        save_config(cfg)
        return 0

    map_width = args.map_width or int(cal.get("map_width", 15) or 15)
    samples = get_samples(cfg, map_id)
    if not samples:
        print(f"[ERR] Cero samples para map_id={map_id}. Hace que el bot pelee en ese mapa primero.")
        return 1
    monitor = detect_monitor(cfg)
    print(f"Map_id: {map_id}")
    print(f"Monitor: {monitor}")
    print(f"Map width: {map_width}")
    print(f"Samples disponibles: {len(samples)} celdas unicas")
    print()

    fit = fit_visual_grid(samples, map_width, monitor)
    if not fit:
        print("[ERR] Fit fallo. Necesitas al menos 3 samples y celdas con gx-gy variado.")
        return 1

    current = get_current_entry(cfg, map_id)
    print("=== FIT PROPUESTO ===")
    print(f"  cell_width:   {fit['cell_width']}  (current: {current.get('cell_width', '?')})")
    print(f"  cell_height:  {fit['cell_height']}  (current: {current.get('cell_height', '?')})")
    print(f"  offset_x:     {fit['offset_x']}    (current: {current.get('offset_x', '?')})")
    print(f"  offset_y:     {fit['offset_y']}    (current: {current.get('offset_y', '?')})")
    print(f"  canvas_width: {fit['canvas_width']}")
    print(f"  canvas_height:{fit['canvas_height']}")
    print(f"  RMSE:         {fit['rmse']} px ({fit['samples_used']} samples)")
    print()
    print("=== ERROR POR CELL ===")
    for d in sorted(fit["detail"], key=lambda x: -x["err_px"])[:15]:
        print(f"  cell={d['cell_id']:>4} grid={d['grid_xy']} click={d['click']} residual={d['residual']} err={d['err_px']}px")
    if len(fit["detail"]) > 15:
        print(f"  ... ({len(fit['detail']) - 15} mas)")
    print()

    if not args.apply:
        print("Para aplicar: agrega --apply")
        if args.lock:
            print("Para aplicar y lockear: --apply --lock")
        return 0

    # Aplicar
    new_entry = {
        "canvas_width": fit["canvas_width"],
        "canvas_height": fit["canvas_height"],
        "cell_width": fit["cell_width"],
        "cell_height": fit["cell_height"],
        "offset_x": fit["offset_x"],
        "offset_y": fit["offset_y"],
        "auto_calibrated": True,
        "auto_calibrated_samples": fit["samples_used"],
        "auto_calibrated_rmse": fit["rmse"],
        "auto_calibrated_at": time.time(),
        "calibrated_via_script": True,
    }
    if args.lock or current.get("manual_lock"):
        new_entry["manual_lock"] = True
    by_map[str(map_id)] = new_entry
    save_config(cfg)
    print(f"[OK] visual_grid_by_map_id['{map_id}'] actualizado en config.yaml.")
    if new_entry.get("manual_lock"):
        print(f"[OK] manual_lock=True — el auto-fit del bot no tocara este mapa.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
