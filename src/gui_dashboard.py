"""
gui_dashboard.py — Dashboard live (Grafana-style) para la GUI.

Separado de `gui.py` porque:
  * gui.py ya tiene 8k LOC.
  * El dashboard es una vista auto-contenida: recibe un parent frame +
    acceso al bot thread y se dibuja solo, con refresh vía timer.
  * Permite iterar sobre el panel sin tocar el resto de la GUI.

Arquitectura:
  * `DashboardMetrics` — acumula eventos de `telemetry.subscribe()` en
    rolling windows thread-safe. Expone `.summary()` → dict con valores
    para renderizar.
  * `build_dashboard_tab(parent, ctx)` — construye 3 cards arriba + mini
    mapa abajo con posición del PJ y enemigos en vivo. Devuelve un
    `refresh(summary, bot_state)` que el caller debe llamar cada ~250ms.

No crea ventanas ni hace `.mainloop()` — solo widgets dentro de `parent`.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable


# ──────────────────────────────────────────────────────── Metrics collector ──


class DashboardMetrics:
    """Acumula eventos de telemetría en rolling windows para el dashboard.

    Thread-safe. Los callbacks de telemetry.emit() corren en el thread del
    bot; consumir desde el thread de Tk solo vía `summary()`.
    """

    # Ventana en segundos para calcular rate "fights/h"
    _HOUR_S = 3600.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Total de fights desde que arrancó la sesión (monotónico — no decrece).
        # Antes usábamos solo el deque rolling; el usuario reportó que el
        # número "se estancaba en ~32" porque es fights/hora no total.
        self._fights_total: int = 0
        # Fight timestamps (rolling 1h) para calcular rate fights/h
        self._fight_starts_recent: deque[float] = deque(maxlen=500)
        # Contadores simples de spells (usados por la card "Combates")
        self._spell_total = 0
        self._spell_fails = 0
        self._session_started = time.time()
        # Para la card "Mapa actual" no necesitamos acumular — se lee del bot.
        # El dashboard canvas usa últimas posiciones de fighters vía bot_state.

    # ── Telemetry subscriber ────────────────────────────────────────────

    def on_event(self, record: dict) -> None:
        """Callback para `telemetry.subscribe(...)`. Debe ser rápido (µs)."""
        k = record.get("kind")
        if not k:
            return
        ts = record.get("ts", time.time())
        with self._lock:
            if k == "fight_start":
                self._fights_total += 1
                self._fight_starts_recent.append(ts)
            elif k == "spell_result":
                self._spell_total += 1
                if not record.get("ok"):
                    self._spell_fails += 1

    # ── Read API ───────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Snapshot seguro. O(n) sobre ventana de 1h (<500 elementos)."""
        now = time.time()
        with self._lock:
            # Purgar fight_starts más viejos que 1h para calcular rate
            while (self._fight_starts_recent
                   and (now - self._fight_starts_recent[0]) > self._HOUR_S):
                self._fight_starts_recent.popleft()
            fights_hour_rate = len(self._fight_starts_recent)
            fights_total = self._fights_total
            session_min = (now - self._session_started) / 60.0
            spells_total = self._spell_total
            spells_fails = self._spell_fails

        return {
            "fights_total": fights_total,
            "fights_hour_rate": fights_hour_rate,
            "session_min": session_min,
            "spells_total": spells_total,
            "spells_fails": spells_fails,
        }


# ──────────────────────────────────────────────────── Map loader (cached) ──


class _MapCache:
    """Cache simple de mapas cargados desde XML.

    Evita re-parsear el XML cada refresh. Key = map_id (int). Value =
    dict con `cells` (list[MapCell]), `width`, `height`.
    """

    def __init__(self, xml_dir: str) -> None:
        self._xml_dir = xml_dir
        self._cache: dict[int, dict] = {}
        self._miss: set[int] = set()  # map_ids que no existen, para no reintentar

    def get(self, map_id) -> dict | None:
        if map_id is None:
            return None
        try:
            mid = int(map_id)
        except (TypeError, ValueError):
            return None
        if mid in self._cache:
            return self._cache[mid]
        if mid in self._miss:
            return None
        # Import tardío para no forzar dependencia si no se usa el map
        try:
            from dofus_map_data import load_map_data_from_xml, decode_map_data
        except ImportError:
            return None
        try:
            loaded = load_map_data_from_xml(mid, self._xml_dir)
        except Exception:
            self._miss.add(mid)
            return None
        if not loaded:
            self._miss.add(mid)
            return None
        map_data, width, height = loaded
        try:
            cells = decode_map_data(map_data, map_width=width)
        except Exception:
            self._miss.add(mid)
            return None
        entry = {"cells": cells, "width": width, "height": height}
        self._cache[mid] = entry
        return entry


# ──────────────────────────────────────────────────────────── Tab builder ──


def build_dashboard_tab(parent, tokens, get_bot_state: Callable) -> Callable[[], None]:
    """Construye el dashboard en `parent` y devuelve una función `refresh()`.

    Layout:
      Fila 0: 3 cards compactas — Estado · Mapa actual · Combates
      Fila 1: mini-mapa isométrico (canvas) con PJ + enemigos en vivo

    Args:
        parent: tk.Frame donde montar el layout.
        tokens: namespace con constantes de diseño (BG, BRAND, FONT_*, etc).
        get_bot_state: callable -> dict con claves:
            state, sniffer_active, paused, map_id, combat_cell, turn_number,
            pa, pm, hp, max_hp, actor_id, pods_current, pods_max,
            enemy_cells (list[int]), metrics (== DashboardMetrics.summary()).

    Returns:
        refresh(): lee estado y pinta. Llamar cada ~250ms desde tk.after().
    """
    import tkinter as tk

    T = tokens
    parent.configure(bg=T.BG)

    # Map cache — ubicación por default desde config (cell_calibration.local_map_xml_dir)
    # pero fallback a mapas/ al lado de este script.
    xml_dir = os.path.join(os.path.dirname(__file__), "..", "mapas")
    # Intentar leer de bot config si está
    try:
        # La opción más limpia sería pasar el dir por param, pero por ahora
        # usamos el default. Si hace falta, ctx puede exponerlo.
        pass
    except Exception:
        pass
    map_cache = _MapCache(os.path.abspath(xml_dir))

    # Container principal
    root = tk.Frame(parent, bg=T.BG)
    root.pack(fill="both", expand=True, padx=T.SP_4, pady=T.SP_4)

    # ── Fila 0: 3 cards compactas ─────────────────────────────────────
    top = tk.Frame(root, bg=T.BG)
    top.pack(fill="x")
    for col in range(3):
        top.columnconfigure(col, weight=1, uniform="topcards")

    cards: dict[str, dict] = {}

    def _make_card(parent_row, row: int, col: int, key: str, title: str,
                   accent: str = None, row_span: int = 1):
        outer = tk.Frame(parent_row, bg=T.BG_ELEVATED, highlightthickness=1,
                         highlightbackground=T.BORDER_DEFAULT)
        outer.grid(row=row, column=col, padx=T.SP_2, pady=T.SP_2,
                   rowspan=row_span, sticky="nsew")
        if accent:
            tk.Frame(outer, bg=accent, height=2).pack(fill="x", side="top")
        inner = tk.Frame(outer, bg=T.BG_ELEVATED)
        inner.pack(fill="both", expand=True, padx=T.SP_3, pady=T.SP_3)
        tk.Label(inner, text=title.upper(), bg=T.BG_ELEVATED,
                 fg=T.TEXT_TERTIARY, font=T.FONT_LABEL, anchor="w").pack(
            fill="x", anchor="w")
        val_var = tk.StringVar(value="—")
        val_lbl = tk.Label(inner, textvariable=val_var, bg=T.BG_ELEVATED,
                            fg=T.TEXT_PRIMARY,
                            font=(T.FONT_FAMILY, 22, "bold"),
                            anchor="w")
        val_lbl.pack(fill="x", anchor="w", pady=(T.SP_1, 0))
        sub_var = tk.StringVar(value="")
        tk.Label(inner, textvariable=sub_var, bg=T.BG_ELEVATED,
                 fg=T.TEXT_SECONDARY, font=T.FONT_CAPTION,
                 anchor="w", justify="left").pack(
            fill="x", anchor="w", pady=(T.SP_1, 0))
        cards[key] = {"val_var": val_var, "sub_var": sub_var,
                      "val_lbl": val_lbl, "outer": outer, "inner": inner}
        return cards[key]

    _make_card(top, 0, 0, "state",   "Estado",       T.BRAND)
    _make_card(top, 0, 1, "map",     "Mapa actual",  T.INFO)
    _make_card(top, 0, 2, "fights",  "Combates",     T.SUCCESS)

    # ── Fila 1: mini-mapa isométrico ──────────────────────────────────
    map_outer = tk.Frame(root, bg=T.BG_ELEVATED, highlightthickness=1,
                          highlightbackground=T.BORDER_DEFAULT)
    map_outer.pack(fill="both", expand=True, pady=(T.SP_2, 0))
    tk.Frame(map_outer, bg=T.BRAND, height=2).pack(fill="x", side="top")

    map_inner = tk.Frame(map_outer, bg=T.BG_ELEVATED)
    map_inner.pack(fill="both", expand=True, padx=T.SP_3, pady=T.SP_3)

    map_header = tk.Frame(map_inner, bg=T.BG_ELEVATED)
    map_header.pack(fill="x")
    tk.Label(map_header, text="MINIMAPA · TIEMPO REAL", bg=T.BG_ELEVATED,
             fg=T.TEXT_TERTIARY, font=T.FONT_LABEL, anchor="w").pack(
        side="left")
    map_status_var = tk.StringVar(value="")
    tk.Label(map_header, textvariable=map_status_var, bg=T.BG_ELEVATED,
             fg=T.TEXT_SECONDARY, font=T.FONT_CAPTION, anchor="e").pack(
        side="right")

    # Leyenda
    legend = tk.Frame(map_inner, bg=T.BG_ELEVATED)
    legend.pack(fill="x", pady=(T.SP_1, T.SP_2))
    _PJ_COLOR = T.INFO     # azul
    _ENEMY_COLOR = T.DANGER  # rojo
    _WALK_COLOR = "#2A303C"
    _BLOCK_COLOR = "#15181F"
    _GRID_LINE = T.BORDER_DEFAULT
    tk.Frame(legend, bg=_PJ_COLOR, width=10, height=10).pack(side="left", padx=(0, 4))
    tk.Label(legend, text="PJ", bg=T.BG_ELEVATED, fg=T.TEXT_SECONDARY,
             font=T.FONT_CAPTION).pack(side="left", padx=(0, T.SP_3))
    tk.Frame(legend, bg=_ENEMY_COLOR, width=10, height=10).pack(side="left", padx=(0, 4))
    tk.Label(legend, text="Enemigos", bg=T.BG_ELEVATED, fg=T.TEXT_SECONDARY,
             font=T.FONT_CAPTION).pack(side="left")

    # Canvas — usa relwidth/relheight para ocupar todo el espacio disponible
    canvas = tk.Canvas(map_inner, bg=T.BG_BASE, highlightthickness=0, height=280)
    canvas.pack(fill="both", expand=True)

    # State del canvas (se recalcula cuando cambia el map_id o el size)
    canvas_state = {
        "map_id": None,
        "width": 0, "height": 0,           # grid dims
        "cells_cache": [],                  # list[MapCell]
        "canvas_w": 0, "canvas_h": 0,      # ultimo size
        "cell_w": 40.0, "cell_h": 20.0,    # px por celda (calculados)
        "offset_x": 0.0, "offset_y": 0.0,
        "polygons_bg": [],                  # ids de polígonos de fondo
        "overlays": [],                     # ids PJ/enemigos (redibujados)
    }

    def _redraw_bg(cells, width, height) -> bool:
        """Dibuja la grilla base (celdas walkable vs bloqueadas). Se hace una
        sola vez por map_id/size porque es lo que no cambia.

        Devuelve True si efectivamente dibujó, False si el canvas aún no
        tenía tamaño (primer tick antes del layout). El caller debe usar
        esto para decidir si actualizar el sig-cache.
        """
        cw = float(canvas.winfo_width() or 1)
        ch = float(canvas.winfo_height() or 1)
        if cw < 20 or ch < 20:
            return False  # aún no hay layout — no tocar nada, reintentar next tick
        # Borrar todo ANTES de intentar redibujar
        canvas.delete("all")
        canvas_state["polygons_bg"].clear()
        canvas_state["overlays"].clear()
        canvas_state["canvas_w"] = cw
        canvas_state["canvas_h"] = ch

        # Isométrico: celda (x,y) centro en:
        #   screen_x = ox + cell_w/2 * (x - y)
        #   screen_y = oy + cell_h/2 * (x + y)
        #
        # Dofus Retro usa un grid zigzag raro (no un simple rectangular) —
        # 479 cells en un mapa "15×17", no 255. En vez de derivar rangos con
        # fórmulas, escaneamos las celdas reales para obtener el bounding box
        # exacto. Robusto contra cualquier layout.
        if not cells:
            return False
        xs_minus_ys = [(c.x - c.y) for c in cells]
        xs_plus_ys = [(c.x + c.y) for c in cells]
        dmin, dmax = min(xs_minus_ys), max(xs_minus_ys)  # rango x-y
        smin, smax = min(xs_plus_ys), max(xs_plus_ys)    # rango x+y
        span_x = (dmax - dmin)
        span_y = (smax - smin)
        # Ancho del bbox en pixels = (span_x + 2) * cell_w / 2 — el +2 es para
        # contar las medias-celdas en los bordes (cada rombo se extiende
        # cell_w/2 a la izq/der de su centro).
        # Resolver cell_w para hacer fit:
        pad = 12.0
        iso_units_x = span_x + 2
        iso_units_y = span_y + 2
        if iso_units_x <= 0 or iso_units_y <= 0:
            return False
        cell_w_fit = 2.0 * (cw - 2 * pad) / iso_units_x
        cell_h_fit = 2.0 * (ch - 2 * pad) / iso_units_y
        # Ratio 2:1 — la dim más restrictiva manda.
        if cell_w_fit / 2.0 < cell_h_fit:
            cell_h = cell_w_fit / 2.0
            cell_w = cell_w_fit
        else:
            cell_h = cell_h_fit
            cell_w = cell_h * 2.0
        cell_w = max(4.0, cell_w)
        cell_h = max(2.0, cell_h)
        canvas_state["cell_w"] = cell_w
        canvas_state["cell_h"] = cell_h
        # Centrar: queremos que el centro geométrico del bbox caiga en el
        # centro del canvas.
        # bbox center_x = ox + ((dmin + dmax)/2) * cell_w / 2
        # bbox center_y = oy + ((smin + smax)/2) * cell_h / 2
        mid_x = (dmin + dmax) / 2.0
        mid_y = (smin + smax) / 2.0
        ox = cw / 2.0 - mid_x * cell_w / 2.0
        oy = ch / 2.0 - mid_y * cell_h / 2.0
        canvas_state["offset_x"] = ox
        canvas_state["offset_y"] = oy

        # Dibujar todas las celdas como rombos
        for cell in cells:
            cx = ox + cell_w / 2.0 * (cell.x - cell.y)
            cy = oy + cell_h / 2.0 * (cell.x + cell.y)
            # Rombo (diamond): 4 puntos alrededor del centro
            pts = [
                cx, cy - cell_h / 2.0,     # top
                cx + cell_w / 2.0, cy,     # right
                cx, cy + cell_h / 2.0,     # bottom
                cx - cell_w / 2.0, cy,     # left
            ]
            fill = _WALK_COLOR if cell.is_walkable else _BLOCK_COLOR
            pid = canvas.create_polygon(
                pts, fill=fill, outline=_GRID_LINE, width=1,
                tags=("bg",),
            )
            canvas_state["polygons_bg"].append(pid)
        return True

    def _cell_center(cell_id, width, height):
        """Devuelve (cx, cy) en pixeles del canvas para un cell_id. None si fuera."""
        try:
            cid = int(cell_id)
        except (TypeError, ValueError):
            return None
        # Replicar cell_id_to_xy
        mw = max(1, int(width))
        row_block = cid // ((mw * 2) - 1)
        row_offset = cid - (row_block * ((mw * 2) - 1))
        y = row_block - (row_offset % mw)
        x = (cid - ((mw - 1) * y)) // mw
        cw = canvas_state["cell_w"]
        ch = canvas_state["cell_h"]
        cx = canvas_state["offset_x"] + cw / 2.0 * (x - y)
        cy = canvas_state["offset_y"] + ch / 2.0 * (x + y)
        return cx, cy

    def _draw_overlay(cell_id, color, label=None):
        """Pinta un marcador (rombo relleno + anillo) en la celda."""
        width = canvas_state["width"]
        height = canvas_state["height"]
        pos = _cell_center(cell_id, width, height)
        if pos is None:
            return
        cx, cy = pos
        cw = canvas_state["cell_w"]
        ch = canvas_state["cell_h"]
        # Rombo sólido (más pequeño que la celda)
        r = 0.85
        pts = [
            cx, cy - ch * r / 2.0,
            cx + cw * r / 2.0, cy,
            cx, cy + ch * r / 2.0,
            cx - cw * r / 2.0, cy,
        ]
        pid = canvas.create_polygon(
            pts, fill=color, outline="#FFFFFF", width=1,
            tags=("overlay",),
        )
        canvas_state["overlays"].append(pid)
        if label:
            tid = canvas.create_text(
                cx, cy, text=str(label), fill="#FFFFFF",
                font=(T.FONT_FAMILY, max(6, int(ch * 0.55)), "bold"),
                tags=("overlay",),
            )
            canvas_state["overlays"].append(tid)

    def _redraw_overlays(my_cell, enemy_cells):
        """Redibuja solo los marcadores. Bg se mantiene."""
        for oid in canvas_state["overlays"]:
            canvas.delete(oid)
        canvas_state["overlays"].clear()
        # Enemigos primero (para que PJ quede encima si overlapean)
        for ec in enemy_cells or []:
            _draw_overlay(ec, _ENEMY_COLOR)
        if my_cell is not None:
            _draw_overlay(my_cell, _PJ_COLOR, label="PJ")

    def _fmt_s(v, digits=2):
        if v is None:
            return "—"
        return f"{v:.{digits}f}s"

    # Helper: detectar si el canvas cambió de tamaño → re-render bg
    last_sig: dict = {"map_id": None, "cw": 0, "ch": 0}

    def refresh():
        try:
            st = get_bot_state() or {}
        except Exception:
            return
        m = st.get("metrics") or {}

        # Card 1: Estado
        bot_state = st.get("state", "—")
        paused = st.get("paused", False)
        sniffer_on = st.get("sniffer_active", False)
        if paused:
            cards["state"]["val_var"].set("Pausado")
            cards["state"]["val_lbl"].config(fg=T.WARNING)
        elif bot_state and bot_state not in ("idle", "—", None):
            cards["state"]["val_var"].set(str(bot_state))
            cards["state"]["val_lbl"].config(fg=T.SUCCESS)
        else:
            cards["state"]["val_var"].set("Detenido")
            cards["state"]["val_lbl"].config(fg=T.DANGER)
        sniffer_txt = "sniffer on" if sniffer_on else "sniffer off"
        actor = st.get("actor_id") or "?"
        sess = m.get("session_min", 0) or 0
        cards["state"]["sub_var"].set(
            f"{sniffer_txt} · actor {actor} · sesión {sess:.0f} min"
        )

        # Card 2: Mapa
        map_id = st.get("map_id")
        cell = st.get("combat_cell")
        pods_curr = st.get("pods_current")
        pods_max = st.get("pods_max")
        cards["map"]["val_var"].set(str(map_id) if map_id else "—")
        sub = f"cell {cell}" if cell is not None else "fuera de combate"
        if pods_curr is not None and pods_max:
            sub += f" · pods {pods_curr}/{pods_max}"
        cards["map"]["sub_var"].set(sub)

        # Card 3: Combates (total sesión + rate últimas 1h)
        total = m.get("fights_total", 0)
        rate = m.get("fights_hour_rate", 0)
        cards["fights"]["val_var"].set(str(total))
        spells = m.get("spells_total", 0)
        fails = m.get("spells_fails", 0)
        fr = (100.0 * fails / spells) if spells else 0.0
        cards["fights"]["sub_var"].set(
            f"{rate}/h · {spells} spells · {fails} fails ({fr:.1f}%)"
        )

        # ── Mini-mapa ────────────────────────────────────────────────
        enemy_cells = st.get("enemy_cells") or []
        # Detectar cambio de map_id o resize → re-render bg
        cw = int(canvas.winfo_width() or 0)
        ch = int(canvas.winfo_height() or 0)
        sig_changed = (
            last_sig["map_id"] != map_id
            or abs(last_sig["cw"] - cw) > 2
            or abs(last_sig["ch"] - ch) > 2
        )
        if map_id is not None:
            entry = map_cache.get(map_id)
            if entry is not None:
                if sig_changed:
                    canvas_state["map_id"] = int(map_id)
                    canvas_state["width"] = entry["width"]
                    canvas_state["height"] = entry["height"]
                    canvas_state["cells_cache"] = entry["cells"]
                    drew = _redraw_bg(entry["cells"], entry["width"], entry["height"])
                    # Solo marcar last_sig si realmente dibujó; si el canvas
                    # todavía no tenía tamaño, dejar sig_changed=True para
                    # reintentar en el próximo tick.
                    if drew:
                        last_sig["map_id"] = map_id
                        last_sig["cw"] = cw
                        last_sig["ch"] = ch
                # Overlays: solo si el bg ya está dibujado (cells_cache poblado)
                if canvas_state["cells_cache"]:
                    _redraw_overlays(cell, enemy_cells)
                map_status_var.set(
                    f"map {map_id} · {entry['width']}×{entry['height']} · "
                    f"{len(enemy_cells)} enemigos"
                )
            else:
                # No XML para este map_id
                if sig_changed:
                    canvas.delete("all")
                    last_sig["map_id"] = map_id
                    last_sig["cw"] = cw
                    last_sig["ch"] = ch
                map_status_var.set(f"map {map_id} · XML no encontrado")
        else:
            if sig_changed:
                canvas.delete("all")
                last_sig["map_id"] = None
                last_sig["cw"] = cw
                last_sig["ch"] = ch
            map_status_var.set("(esperando map_id)")

    return refresh
