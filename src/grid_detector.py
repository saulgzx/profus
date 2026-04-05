"""
IsoGridDetector — detecta automáticamente el origen del grid isométrico
de Dofus Retro en combate usando los aros de combatientes y datos de GIC.

Algoritmo en dos fases:

Fase 1 — Anclaje directo (requiere aro rojo + validación de pantalla):
  Si sabemos nuestra celda (my_cell_id), buscar el aro ROJO (= nuestro PJ)
  que, usado como anclaje, produce un origen donde nuestra celda proyecta
  dentro del monitor. Confianza = "low" (único aro, sin validación cruzada).
  NO se persiste en config.yaml para evitar guardar orígenes incorrectos.

Fase 2 — RANSAC (requiere score ≥ 2):
  Hipótesis (aro, celda) → votar cuántos otros aros son explicados.
  Solo acepta score ≥ 2 Y todas las celdas en pantalla.
  Confianza = "high". Se persiste en config.yaml.

El único campo variable entre arenas es el origen (x, y).
Los slopes son constantes para el zoom del juego y vienen del config.yaml.
"""

import cv2
import numpy as np
from typing import NamedTuple


# ─────────────────────────────── filtros de contorno ──
_MIN_AREA   = 300
_MAX_AREA   = 12000
_MIN_ASPECT = 1.3
_MAX_ASPECT = 4.2
_MIN_FILL   = 0.20
_MAX_FILL   = 0.88
_MIN_RADIUS = 10
_MAX_RADIUS = 90
_MIN_RED_DOMINANCE = 1.28
_MIN_RED_BLUE_DOMINANCE = 1.45

# Tolerancia en px para contar un aro como inlier del RANSAC
INLIER_TOLERANCE = 55

# Radio NMS: varios contornos del mismo aro se fusionan
_NMS_RADIUS = 60

# Score mínimo del RANSAC para aceptar (requiere ≥2 aros consistentes)
_MIN_RANSAC_SCORE = 2

# Dispersión máxima aceptable entre inliers (px).
# Con spread alto los "inliers" son coincidencias falsas, no aros reales.
# Los combates reales tienen todos los actores dentro de ~600px entre sí.
_MAX_RANSAC_SPREAD = 300

# Margen interior: las proyecciones deben estar dentro del monitor menos este margen
_SCREEN_MARGIN = 80


class GridResult(NamedTuple):
    origin: tuple      # (origin_x, origin_y) en coords absolutas
    confidence: str    # "high" | "low"
    score: int         # inliers del RANSAC, o 1 para anclaje directo


class IsoGridDetector:
    """
    Detecta el origen del grid isométrico a partir de una captura de pantalla
    en combate y las posiciones de celda del protocolo (GIC).
    """

    def __init__(self, slopes: dict, map_width: int = 14):
        self.col_x     = float(slopes["col_x"])
        self.col_y     = float(slopes["col_y"])
        self.row_x     = float(slopes["row_x"])
        self.row_y     = float(slopes["row_y"])
        self.map_width = map_width

    # ─────────────────────────────────────── public api ──

    def detect(
        self,
        frame: np.ndarray,
        monitor: dict,
        gic_entries: list[dict],
        my_cell_id: int | None = None,
        debug_path: str | None = None,
    ) -> GridResult | None:
        """
        Pipeline completo. Retorna un GridResult o None si sin confianza.

        frame       : BGR image capturada del monitor (coords relativas al monitor)
        monitor     : dict con {"left", "top", "width", "height"} en coords absolutas
        gic_entries : lista de {"actor_id": str, "cell_id": int} del evento GIC
        my_cell_id  : cell_id propio (de _combat_cell). Permite anclaje directo.
        debug_path  : si se indica, guarda una imagen de debug en esa ruta
        """
        cell_ids = [
            int(e["cell_id"])
            for e in gic_entries
            if e.get("cell_id") is not None
        ]
        if not cell_ids:
            return None

        red_rings, blue_rings = self._detect_rings(frame, monitor)
        red_rings  = self._nms(red_rings,  radius=_NMS_RADIUS)
        blue_rings = self._nms(blue_rings, radius=_NMS_RADIUS)
        all_rings  = self._nms(red_rings + blue_rings, radius=_NMS_RADIUS)

        print(
            f"[GRID] Aros: total={len(all_rings)} "
            f"rojo={len(red_rings)} azul={len(blue_rings)}  "
            f"Celdas GIC: {len(cell_ids)}"
        )

        result: GridResult | None = None

        # ── Fase 2: RANSAC (alta confianza) ──────────────────────────────────
        if len(all_rings) >= _MIN_RANSAC_SCORE:
            ransac_result = self._ransac(all_rings, cell_ids, monitor)
            if ransac_result is not None:
                origin, score = ransac_result
                result = GridResult(origin=origin, confidence="high", score=score)

        # ── Fase 1 deshabilitada: un solo aro rojo produce demasiados falsos positivos.

        # ── Debug image ───────────────────────────────────────────────────────
        if debug_path:
            self._save_debug_image(
                frame, monitor, red_rings, blue_rings, cell_ids, result, debug_path
            )

        return result

    def cell_to_screen(
        self,
        cell_id: int,
        origin: tuple[float, float],
    ) -> tuple[int, int]:
        lx, ly = self._cell_to_local(cell_id)
        return (int(round(origin[0] + lx)), int(round(origin[1] + ly)))

    def origin_from_anchor(
        self,
        cell_id: int,
        screen_pos: tuple,
    ) -> tuple[float, float]:
        lx, ly = self._cell_to_local(cell_id)
        return (float(screen_pos[0]) - lx, float(screen_pos[1]) - ly)

    # ─────────────────────────────────────── fase 1 ──

    def _anchor_from_own_ring(
        self,
        red_rings: list,
        my_cell_id: int,
        cell_ids: list[int],
        monitor: dict,
    ) -> tuple | None:
        """
        Para cada aro rojo, calcula el origen y verifica que NUESTRA celda
        quede en pantalla (no todas las celdas — en peleas grandes no caben).
        Retorna (origin, ring_pos) o None.
        """
        valid = []
        for ring in red_rings:
            ox, oy = self.origin_from_anchor(my_cell_id, ring)
            if self._cell_on_screen(my_cell_id, (ox, oy), monitor):
                valid.append(((ox, oy), ring))

        if len(valid) == 1:
            return valid[0]
        if len(valid) > 1:
            print(f"[GRID] {len(valid)} candidatos rojos — seleccionando el más central")
            cx = monitor["left"] + monitor["width"]  / 2
            cy = monitor["top"]  + monitor["height"] / 2
            # Preferir el aro más cercano al centro del monitor
            return min(valid, key=lambda v: (v[1][0] - cx) ** 2 + (v[1][1] - cy) ** 2)
        return None

    def _cell_on_screen(self, cell_id: int, origin, monitor) -> bool:
        """Verifica que UNA celda específica proyecte dentro del monitor."""
        ml = monitor["left"]  + _SCREEN_MARGIN
        mt = monitor["top"]   + _SCREEN_MARGIN
        mr = monitor["left"]  + monitor["width"]  - _SCREEN_MARGIN
        mb = monitor["top"]   + monitor["height"] - _SCREEN_MARGIN
        x, y = self.cell_to_screen(cell_id, origin)
        return ml <= x <= mr and mt <= y <= mb

    def _projection_spread(self, cell_ids, origin) -> float:
        coords = [self.cell_to_screen(cid, origin) for cid in cell_ids]
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        return float(np.std(xs) + np.std(ys))

    # ─────────────────────────────────────── fase 2 ──

    def _ransac(
        self,
        ring_positions: list,
        cell_ids: list[int],
        monitor: dict,
    ) -> tuple | None:
        """RANSAC. Retorna (origin, score) si score >= _MIN_RANSAC_SCORE, o None."""
        cell_locals = {cid: self._cell_to_local(cid) for cid in cell_ids}
        best_score   = 0
        best_inliers = []

        for ring_pos in ring_positions:
            for cell_id in cell_ids:
                lx, ly = cell_locals[cell_id]
                ox = ring_pos[0] - lx
                oy = ring_pos[1] - ly
                # Solo verificar que el aro semilla proyecte en pantalla
                if not self._cell_on_screen(cell_id, (ox, oy), monitor):
                    continue
                inlier_origins = []
                for rp in ring_positions:
                    best_dist = float("inf")
                    best_cid  = None
                    for cid in cell_ids:
                        lx2, ly2 = cell_locals[cid]
                        d = ((rp[0] - (ox + lx2)) ** 2 + (rp[1] - (oy + ly2)) ** 2) ** 0.5
                        if d < best_dist:
                            best_dist = d
                            best_cid  = cid
                    if best_dist <= INLIER_TOLERANCE and best_cid is not None:
                        lx2, ly2 = cell_locals[best_cid]
                        inlier_origins.append((rp[0] - lx2, rp[1] - ly2))
                score = len(inlier_origins)
                if score > best_score:
                    best_score   = score
                    best_inliers = inlier_origins

        if best_score < _MIN_RANSAC_SCORE:
            print(
                f"[GRID] RANSAC score={best_score}/{len(ring_positions)} "
                f"< mínimo ({_MIN_RANSAC_SCORE}) — descartado"
            )
            return None

        xs = [o[0] for o in best_inliers]
        ys = [o[1] for o in best_inliers]
        refined = (float(np.median(xs)), float(np.median(ys)))
        spread  = float(np.std(xs + ys)) if len(xs) > 1 else 0.0
        print(
            f"[GRID] RANSAC score={best_score}/{len(ring_positions)} "
            f"spread={spread:.1f}px origin={refined}"
        )
        if spread > _MAX_RANSAC_SPREAD:
            print(
                f"[GRID] RANSAC rechazado — spread={spread:.1f}px "
                f"> máximo ({_MAX_RANSAC_SPREAD}px), probable falso positivo"
            )
            return None
        return refined, best_score

    # ─────────────────────────────────────── detección visual ──

    # Zona de juego como fracción del monitor (excluye UI de Dofus Retro).
    # Panel izquierdo (botones fuga, info): ~14% izquierdo
    # Barra inferior (hechizos, HP, stats): ~18% inferior
    # Cabecera (nombre mapa, coordenadas):  ~8% superior
    # Panel derecho (minimapa, etc.):       ~10% derecho
    _GAME_LEFT   = 0.14
    _GAME_RIGHT  = 0.90
    _GAME_TOP    = 0.09
    _GAME_BOTTOM = 0.70

    def _detect_rings(
        self,
        frame: np.ndarray,
        monitor: dict,
    ) -> tuple[list, list]:
        """
        Detecta aros de combatientes en la zona de juego.
        En Dofus Retro:
          - Aro ROJO  = nuestro personaje
          - Aros AZULES = enemigos (y aliados)
        Retorna (red_rings, blue_rings) con posiciones absolutas.
        """
        h_frame, w_frame = frame.shape[:2]

        # Recortar a la zona de juego
        x1 = int(w_frame * self._GAME_LEFT)
        x2 = int(w_frame * self._GAME_RIGHT)
        y1 = int(h_frame * self._GAME_TOP)
        y2 = int(h_frame * self._GAME_BOTTOM)
        crop = frame[y1:y2, x1:x2]

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)

        # Solo OPEN (eliminar ruido pequeño).
        # NO CLOSE: el CLOSE rellena el hueco interior del aro y luego es
        # indistinguible de un elemento de terreno sólido.
        kernel = np.ones((3, 3), np.uint8)

        # Máscara roja estricta. Evita naranjos saturados de la UI.
        mask_red = cv2.bitwise_or(
            cv2.inRange(hsv, (0,   140, 95), (8,   255, 255)),
            cv2.inRange(hsv, (172, 140, 95), (180, 255, 255)),
        )
        mask_red = cv2.morphologyEx(mask_red, cv2.MORPH_OPEN, kernel)

        # Máscara azul (cyan, azul, azul-violeta)
        mask_blue = cv2.inRange(hsv, (85, 80, 50), (145, 255, 255))
        mask_blue = cv2.morphologyEx(mask_blue, cv2.MORPH_OPEN, kernel)

        def _extract(mask):
            contours, hierarchy = cv2.findContours(
                mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
            )
            if hierarchy is None:
                return []
            hier = hierarchy[0]  # shape (N, 4): [next, prev, first_child, parent]
            rings: list = []
            for i, cnt in enumerate(contours):
                # Solo contornos exteriores (sin padre) que tengan un hueco interior
                # (primer hijo != -1). Filtra formas sólidas: flores, íconos, rocas.
                if hier[i][3] != -1:   # tiene padre → es contorno interior, skip
                    continue
                if hier[i][2] == -1:   # sin hijo → forma sólida, no es aro
                    continue
                area = cv2.contourArea(cnt)
                if not (_MIN_AREA <= area <= _MAX_AREA):
                    continue
                bx, by, bw, bh = cv2.boundingRect(cnt)
                if bw < 16 or bh < 8:
                    continue
                aspect = bw / max(bh, 1)
                if not (_MIN_ASPECT <= aspect <= _MAX_ASPECT):
                    continue
                hull_area = cv2.contourArea(cv2.convexHull(cnt))
                if hull_area <= 0:
                    continue
                fill = area / hull_area
                if not (_MIN_FILL <= fill <= _MAX_FILL):
                    continue
                (cx, cy), radius = cv2.minEnclosingCircle(cnt)
                if not (_MIN_RADIUS <= radius <= _MAX_RADIUS):
                    continue

                # Validación de color: un aro rojo real tiene mucho más canal R que G/B.
                obj_mask = np.zeros(mask.shape, dtype=np.uint8)
                cv2.drawContours(obj_mask, [cnt], -1, 255, thickness=-1)
                mean_b, mean_g, mean_r, _ = cv2.mean(crop, mask=obj_mask)
                if mean_r < mean_g * _MIN_RED_DOMINANCE:
                    continue
                if mean_r < mean_b * _MIN_RED_BLUE_DOMINANCE:
                    continue

                abs_x = monitor["left"] + x1 + cx
                abs_y = monitor["top"]  + y1 + cy
                rings.append((float(abs_x), float(abs_y)))
            return rings

        return _extract(mask_red), _extract(mask_blue)

    # ─────────────────────────────────────── debug ──

    def _save_debug_image(
        self,
        frame: np.ndarray,
        monitor: dict,
        red_rings: list,
        blue_rings: list,
        cell_ids: list[int],
        result: GridResult | None,
        path: str,
    ) -> None:
        """Guarda imagen de debug con aros detectados y proyecciones de celdas."""
        try:
            dbg = frame.copy()
            ml, mt = monitor["left"], monitor["top"]
            h, w = frame.shape[:2]

            # Mostrar la zona de juego usada para la búsqueda (rectángulo blanco)
            gx1 = int(w * self._GAME_LEFT)
            gx2 = int(w * self._GAME_RIGHT)
            gy1 = int(h * self._GAME_TOP)
            gy2 = int(h * self._GAME_BOTTOM)
            cv2.rectangle(dbg, (gx1, gy1), (gx2, gy2), (200, 200, 200), 1)

            # Aros rojos detectados: círculo verde con "R"
            for rx, ry in red_rings:
                cx, cy = int(rx - ml), int(ry - mt)
                cv2.circle(dbg, (cx, cy), 22, (0, 255, 0), 3)
                cv2.putText(dbg, "R", (cx - 6, cy + 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

            # Aros azules detectados: círculo cian con "B"
            for bx, by in blue_rings:
                cx, cy = int(bx - ml), int(by - mt)
                cv2.circle(dbg, (cx, cy), 22, (255, 255, 0), 3)
                cv2.putText(dbg, "B", (cx - 6, cy + 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)

            # Proyecciones de celdas GIC: cruz magenta con número de celda
            if result is not None:
                for cid in cell_ids:
                    px, py = self.cell_to_screen(cid, result.origin)
                    fx, fy = int(px - ml), int(py - mt)
                    # Dibujar siempre aunque esté fuera (clipeado)
                    fx_c = max(5, min(w - 5, fx))
                    fy_c = max(5, min(h - 5, fy))
                    cv2.drawMarker(dbg, (fx_c, fy_c), (255, 0, 255),
                                   cv2.MARKER_CROSS, 30, 3)
                    cv2.putText(dbg, str(cid), (fx_c + 8, fy_c - 8),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 0, 255), 2)

            conf = result.confidence if result else "FALLO"
            score = result.score if result else 0
            cv2.putText(dbg, f"conf={conf} score={score}", (10, 35),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 4)
            cv2.putText(dbg, f"conf={conf} score={score}", (10, 35),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
            cv2.imwrite(path, dbg)
            print(f"[GRID] Debug imagen guardada: {path}")
        except Exception as e:
            print(f"[GRID] Error guardando debug image: {e}")

    # ─────────────────────────────────────── utils ──

    def _cell_to_local(self, cell_id: int) -> tuple:
        col = cell_id % self.map_width
        row = cell_id // self.map_width
        return (
            self.col_x * col + self.row_x * row,
            self.col_y * col + self.row_y * row,
        )

    def _nms(self, points: list, radius: int = 35) -> list:
        kept = []
        r2 = radius * radius
        for p in points:
            if not any((p[0]-q[0])**2 + (p[1]-q[1])**2 < r2 for q in kept):
                kept.append(p)
        return kept
