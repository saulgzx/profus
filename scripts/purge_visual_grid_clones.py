"""Purga clonas de visual_grid_by_map_id sin marcador 'specific: true'.

Uso:
    python scripts/purge_visual_grid_clones.py          # dry-run
    python scripts/purge_visual_grid_clones.py --apply  # escribe
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.yaml"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    with CONFIG.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cal = cfg.get("bot", {}).get("cell_calibration", {}) or {}
    by_map = cal.get("visual_grid_by_map_id", {}) or {}
    if not isinstance(by_map, dict):
        print("[ERR] visual_grid_by_map_id no es dict.")
        return 1

    kept, purged = {}, []
    for k, v in by_map.items():
        if isinstance(v, dict) and (v.get("specific") is True or v.get("manual_lock") is True):
            kept[k] = v
        else:
            purged.append(k)

    print(f"Total entradas : {len(by_map)}")
    print(f"Preservadas    : {len(kept)}  (specific=True o manual_lock=True)")
    print(f"A purgar       : {len(purged)}  (clones sin marcador)")
    if kept:
        print("\nPreservadas:")
        for k, v in kept.items():
            flags = []
            if v.get("specific") is True:
                flags.append("specific")
            if v.get("manual_lock") is True:
                flags.append("manual_lock")
            print(f"  map={k}  cw={v.get('cell_width')} ch={v.get('cell_height')} "
                  f"ox={v.get('offset_x')} oy={v.get('offset_y')}  [{','.join(flags)}]")

    if not args.apply:
        print("\nDry-run. Para aplicar: --apply")
        return 0

    cal["visual_grid_by_map_id"] = kept
    cfg.setdefault("bot", {})["cell_calibration"] = cal
    with CONFIG.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)
    print(f"\n[OK] Purgadas {len(purged)} entradas. Config guardado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
