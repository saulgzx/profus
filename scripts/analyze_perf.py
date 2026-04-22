"""
analyze_perf.py — Análisis offline de logs de perf y packets.

Uso:
    python scripts/analyze_perf.py logs/perf-20260422.jsonl
    python scripts/analyze_perf.py logs/perf-20260422.jsonl --label placement.click_to_confirm
    python scripts/analyze_perf.py logs/packets-20260422.jsonl --packets

Output:
  * Por cada label de span/mark: count, p50, p95, p99, max, sum_total.
  * Para packets: distribución por tipo de paquete (GIC/GTM/Gp/As/...) y
    overhead promedio de parsing.
  * Sleep audit: si hay spans `placement.retry_loop` o `actions.quick_click`,
    estima cuánto tiempo se pasa "esperando" vs "haciendo".

No requiere dependencias externas (solo stdlib).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def fmt_ms(v: float) -> str:
    if v >= 1000:
        return f"{v/1000:.2f}s"
    return f"{v:.2f}ms"


def analyze_perf(path: str, filter_label: str | None) -> int:
    if not os.path.exists(path):
        print(f"[ERR] No existe: {path}", file=sys.stderr)
        return 1

    by_label: dict[str, list[float]] = defaultdict(list)
    by_label_extras: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    points: dict[str, int] = defaultdict(int)
    total_records = 0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            total_records += 1
            kind = rec.get("kind")
            label = rec.get("label", "?")
            if filter_label and label != filter_label:
                continue
            if kind in ("span", "mark"):
                dur = rec.get("dur_ms")
                if isinstance(dur, (int, float)):
                    by_label[label].append(float(dur))
                # Catalogar por result/landed_cell/etc para diagnóstico
                for k in ("result", "landed_cell", "has_manual_pixel", "fight_ended"):
                    if k in rec:
                        by_label_extras[label][f"{k}={rec[k]}"] += 1
            elif kind == "point":
                points[label] += 1

    print(f"\n=== perf log: {path} ({total_records:,} records) ===\n")

    if not by_label and not points:
        print("(sin datos para analizar)")
        return 0

    # Tabla principal
    if by_label:
        print(f"{'label':<48} {'n':>6} {'p50':>10} {'p95':>10} {'p99':>10} {'max':>10} {'sum':>12}")
        print("-" * 110)
        rows = sorted(by_label.items(), key=lambda kv: -sum(kv[1]))
        for label, values in rows:
            n = len(values)
            p50 = percentile(values, 50)
            p95 = percentile(values, 95)
            p99 = percentile(values, 99)
            mx = max(values)
            tot = sum(values)
            print(f"{label:<48} {n:>6} {fmt_ms(p50):>10} {fmt_ms(p95):>10} {fmt_ms(p99):>10} {fmt_ms(mx):>10} {fmt_ms(tot):>12}")
        print()

    if points:
        print("Eventos puntuales:")
        for k, v in sorted(points.items(), key=lambda kv: -kv[1]):
            print(f"  {k}: {v}")
        print()

    # Diagnóstico: si hay placement.click_to_confirm con result=miss/no_move, listarlos
    for label, extras in by_label_extras.items():
        if any("=miss" in k or "=no_move" in k for k in extras):
            print(f"Distribución de resultados para {label}:")
            for k, v in sorted(extras.items()):
                print(f"  {k}: {v}")
            print()

    # Insight: tiempo total dormido en placement
    if "placement.click_to_confirm" in by_label:
        total_click = sum(by_label["placement.click_to_confirm"])
        n_clicks = len(by_label["placement.click_to_confirm"])
        avg = total_click / n_clicks
        print(f"Click->confirm: {n_clicks} clicks, promedio {avg:.1f}ms - "
              f"~{int(total_click/1000)}s totales esperando confirmacion")

    if "actions.quick_click" in by_label:
        total = sum(by_label["actions.quick_click"])
        n = len(by_label["actions.quick_click"])
        print(f"Mouse clicks (pyautogui): {n} clicks, total {fmt_ms(total)} "
              f"(promedio {total/n:.1f}ms cada uno)")

    return 0


def analyze_packets(path: str) -> int:
    if not os.path.exists(path):
        print(f"[ERR] No existe: {path}", file=sys.stderr)
        return 1

    by_type: dict[str, int] = defaultdict(int)
    by_dir: dict[str, int] = defaultdict(int)
    parse_times: list[float] = []
    total = 0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            data = rec.get("data", "")
            direction = rec.get("dir", "?")
            by_dir[direction] += 1
            # Tipo de paquete: primeros 2-3 chars
            ptype = ""
            for ch in data[:4]:
                if ch.isalpha():
                    ptype += ch
                else:
                    break
            by_type[ptype or "?"] += 1
            pm = rec.get("parse_ms")
            if isinstance(pm, (int, float)):
                parse_times.append(float(pm))

    print(f"\n=== packets log: {path} ({total:,} packets) ===\n")
    print("Por dirección:")
    for d, n in sorted(by_dir.items(), key=lambda kv: -kv[1]):
        print(f"  {d}: {n}")
    print("\nTipos de paquete (top 15):")
    for t, n in sorted(by_type.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {t:<8} {n:>6}")

    if parse_times:
        print(f"\nOverhead de parsing: n={len(parse_times)} "
              f"p50={percentile(parse_times, 50):.3f}ms "
              f"p95={percentile(parse_times, 95):.3f}ms "
              f"p99={percentile(parse_times, 99):.3f}ms "
              f"max={max(parse_times):.3f}ms")

    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Analiza logs de perf/packets del bot")
    parser.add_argument("path", help="Ruta al .jsonl de perf o packets")
    parser.add_argument("--label", default=None,
                        help="Filtrar por label específico (ej: placement.click_to_confirm)")
    parser.add_argument("--packets", action="store_true",
                        help="Tratar el archivo como packets-*.jsonl")
    args = parser.parse_args(argv)

    if args.packets or "packets" in os.path.basename(args.path):
        return analyze_packets(args.path)
    return analyze_perf(args.path, args.label)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
