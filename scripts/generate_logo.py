"""generate_logo.py — Genera el logo del bot (dofus Sadida + engranaje).

Renderiza con PIL (ya dependencia del proyecto) el mismo diseño que el SVG
inline en gui_web.py. Outputs:
  * assets/brand/logo_48.png   (usado por tkinter header, 28x28 resize)
  * assets/brand/logo_96.png   (tamaños intermedios)
  * assets/brand/logo_192.png  (fallback PNG si el browser no soporta SVG)
  * assets/brand/logo_256.png  (source de alta resolución)
  * assets/brand/logo.ico      (icono de ventana Windows, multi-size)
  * assets/brand/logo.svg      (copia del SVG del gui_web.py para referencia)

Run:
    python scripts/generate_logo.py
"""
from __future__ import annotations

import math
import os
import sys
from PIL import Image, ImageDraw, ImageFilter


# Color stops del gradient (match con SVG en gui_web.py)
_STOPS = [
    (0.00, (255, 255, 140)),  # bright yellow core
    (0.25, (230, 255, 80)),
    (0.55, (158, 203, 40)),
    (0.85, (78, 122, 18)),
    (1.00, (46, 74, 8)),      # dark green edge
]
_OUTLINE = (30, 50, 6)
_SPOT_COLOR = (46, 74, 8)
_GEAR_OUTER = (52, 78, 22)       # gear body highlight
_GEAR_MID = (34, 54, 8)           # gear main color
_GEAR_DARK = (14, 26, 2)          # gear outline
_HOLE_COLOR = (216, 255, 64)      # hole matches egg bright core
_HIGHLIGHT = (255, 255, 255)


def _interp(t, stops):
    t = max(0.0, min(1.0, t))
    for i in range(len(stops) - 1):
        a_off, a_col = stops[i]
        b_off, b_col = stops[i + 1]
        if t <= b_off:
            span = b_off - a_off
            local = (t - a_off) / span if span > 0 else 0.0
            return tuple(
                int(a_col[k] * (1 - local) + b_col[k] * local)
                for k in range(3)
            )
    return stops[-1][1]


def _rotated_rect_polygon(cx, cy, w, h, angle_rad):
    """Devuelve 4 puntos de un rectángulo rotado alrededor de (cx, cy)."""
    pts = []
    for dx, dy in ((-w/2, -h/2), (w/2, -h/2), (w/2, h/2), (-w/2, h/2)):
        rx = dx * math.cos(angle_rad) - dy * math.sin(angle_rad)
        ry = dx * math.sin(angle_rad) + dy * math.cos(angle_rad)
        pts.append((cx + rx, cy + ry))
    return pts


def render(size: int = 256) -> Image.Image:
    """Renderiza el logo a `size` pixels."""
    W = H = size
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # ── Egg shape (ellipse vertical, un toque más ancho abajo) ─────────
    egg_cx = W / 2
    egg_cy_top = H * 0.46       # centro para mitad superior (más chata)
    egg_cy_bot = H * 0.54       # centro para mitad inferior (más panzona)
    egg_a_top = W * 0.41
    egg_a_bot = W * 0.44
    egg_b_top = H * 0.36
    egg_b_bot = H * 0.45

    # Gradient anchor (top-left para el brillo)
    grad_cx = W * 0.34
    grad_cy = H * 0.28
    grad_r_max = W * 0.82

    # Render pixel por pixel (img chico, aceptable)
    px = img.load()
    for y in range(H):
        for x in range(W):
            # 1. Determinar si (x,y) está dentro del egg (dos mitades de ellipse)
            if y <= egg_cy_top:
                in_egg = ((x - egg_cx) / egg_a_top) ** 2 + ((y - egg_cy_top) / egg_b_top) ** 2 <= 1.0
            else:
                in_egg = ((x - egg_cx) / egg_a_bot) ** 2 + ((y - egg_cy_bot) / egg_b_bot) ** 2 <= 1.0
            if not in_egg:
                continue
            # 2. Color por gradient radial
            d = math.sqrt((x - grad_cx) ** 2 + (y - grad_cy) ** 2) / grad_r_max
            r, g, b = _interp(d, _STOPS)
            px[x, y] = (r, g, b, 255)

    # ── Outline del egg (stroke) ───────────────────────────────────────
    # Approx: dibujar stroke sobre cada mitad de la ellipse
    draw = ImageDraw.Draw(img, "RGBA")
    stroke_w = max(2, int(W * 0.015))
    # top half arc
    draw.chord(
        [egg_cx - egg_a_top, egg_cy_top - egg_b_top,
         egg_cx + egg_a_top, egg_cy_top + egg_b_top],
        180, 360, fill=None, outline=_OUTLINE + (255,), width=stroke_w,
    )
    draw.chord(
        [egg_cx - egg_a_bot, egg_cy_bot - egg_b_bot,
         egg_cx + egg_a_bot, egg_cy_bot + egg_b_bot],
        0, 180, fill=None, outline=_OUTLINE + (255,), width=stroke_w,
    )

    # ── Texture spots ──────────────────────────────────────────────────
    spots = [
        (W * 0.30, H * 0.78, W * 0.035, H * 0.018),
        (W * 0.72, H * 0.47, W * 0.028, H * 0.015),
        (W * 0.625, H * 0.87, W * 0.020, H * 0.011),
        (W * 0.20, H * 0.48, W * 0.018, H * 0.010),
        (W * 0.77, H * 0.73, W * 0.020, H * 0.011),
    ]
    spot_alpha = 110
    for sx, sy, sa, sb in spots:
        draw.ellipse(
            [sx - sa, sy - sb, sx + sa, sy + sb],
            fill=(*_SPOT_COLOR, spot_alpha),
        )

    # ── Gear (engranaje) ───────────────────────────────────────────────
    gear_cx = W / 2
    gear_cy = H * 0.56
    gear_R_body = W * 0.155
    gear_R_hole = W * 0.060
    tooth_w = W * 0.080
    tooth_h = W * 0.105
    tooth_orbit_r = gear_R_body + tooth_h * 0.35  # centro del tooth
    gear_outline_w = max(1, int(W * 0.008))

    # Dibujamos cada tooth como polígono rotado
    for i in range(8):
        ang = math.radians(i * 45)
        tx = gear_cx + tooth_orbit_r * math.sin(ang)
        ty = gear_cy - tooth_orbit_r * math.cos(ang)
        poly = _rotated_rect_polygon(tx, ty, tooth_w, tooth_h, ang)
        draw.polygon(
            poly,
            fill=(*_GEAR_MID, 255),
            outline=(*_GEAR_DARK, 255),
        )

    # Gear body disc
    draw.ellipse(
        [gear_cx - gear_R_body, gear_cy - gear_R_body,
         gear_cx + gear_R_body, gear_cy + gear_R_body],
        fill=(*_GEAR_MID, 255),
        outline=(*_GEAR_DARK, 255),
        width=gear_outline_w,
    )

    # Hole
    draw.ellipse(
        [gear_cx - gear_R_hole, gear_cy - gear_R_hole,
         gear_cx + gear_R_hole, gear_cy + gear_R_hole],
        fill=(*_HOLE_COLOR, 255),
        outline=(*_GEAR_DARK, 255),
        width=gear_outline_w,
    )
    # Center dot (screw)
    dot_r = max(1, int(W * 0.013))
    draw.ellipse(
        [gear_cx - dot_r, gear_cy - dot_r, gear_cx + dot_r, gear_cy + dot_r],
        fill=(*_GEAR_DARK, 255),
    )

    # ── Glossy highlight (ellipse blureada blanca) ────────────────────
    hl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hldraw = ImageDraw.Draw(hl)
    hl_cx = W * 0.36
    hl_cy = H * 0.29
    hl_rx = W * 0.12
    hl_ry = H * 0.20
    hldraw.ellipse(
        [hl_cx - hl_rx, hl_cy - hl_ry, hl_cx + hl_rx, hl_cy + hl_ry],
        fill=(*_HIGHLIGHT, 150),
    )
    hl = hl.filter(ImageFilter.GaussianBlur(radius=max(1, int(W * 0.008))))
    img = Image.alpha_composite(img, hl)

    # ── Sparkle (punto de luz pequeño) ────────────────────────────────
    draw = ImageDraw.Draw(img, "RGBA")
    sp_cx = W * 0.28
    sp_cy = H * 0.20
    sp_r = max(1, int(W * 0.015))
    draw.ellipse(
        [sp_cx - sp_r, sp_cy - sp_r, sp_cx + sp_r, sp_cy + sp_r],
        fill=(*_HIGHLIGHT, 210),
    )
    # Segundo sparkle (más chico)
    sp2_r = max(1, int(W * 0.008))
    draw.ellipse(
        [W * 0.41 - sp2_r, H * 0.15 - sp2_r, W * 0.41 + sp2_r, H * 0.15 + sp2_r],
        fill=(*_HIGHLIGHT, 150),
    )

    return img


def main():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    out_dir = os.path.join(repo_root, "assets", "brand")
    os.makedirs(out_dir, exist_ok=True)

    # PNGs en varios tamaños (renderizamos a 256 y redimensionamos para
    # nitidez consistente).
    base = render(256)
    for sz in (48, 96, 192, 256):
        path = os.path.join(out_dir, f"logo_{sz}.png")
        if sz == 256:
            base.save(path, "PNG", optimize=True)
        else:
            base.resize((sz, sz), Image.LANCZOS).save(path, "PNG", optimize=True)
        print(f"  saved {path}")

    # ICO multi-size (Windows taskbar + ventana)
    ico_path = os.path.join(out_dir, "logo.ico")
    base.save(ico_path, sizes=[(16, 16), (24, 24), (32, 32),
                                (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"  saved {ico_path}")

    # Copia del SVG (source of truth vive en gui_web.py)
    svg_path = os.path.join(out_dir, "logo.svg")
    sys.path.insert(0, os.path.join(repo_root, "src"))
    try:
        from gui_web import _ICON_SVG
        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(_ICON_SVG)
        print(f"  saved {svg_path}")
    except Exception as exc:
        print(f"  (skipped SVG copy: {exc})")

    print("\nOK. Reiniciá el bot para que tkinter cargue el nuevo logo_48.png.")


if __name__ == "__main__":
    main()
