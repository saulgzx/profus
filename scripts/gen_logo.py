"""
Genera el logo de Dofus Autofarm en multiples tamanos + un ICO multi-tamano.

Diseno:
  - Squircle (iOS-like) con gradiente calido amber -> orange.
  - Engranaje blanco centrado con 8 dientes redondeados y hueco central.
  - Aesthetics inspiradas en Apple: simetria, generous negative space, sin detalles
    mecanicos de mas, un solo acento.

Salida: assets/brand/
  - logo_16.png ... logo_1024.png
  - logo.ico (multi-size: 16/32/48/64/128/256)
  - logo_wordmark.png (logo + "Dofus Autofarm" para splash/header)

Uso:
  python scripts/gen_logo.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent.parent / "assets" / "brand"
OUT.mkdir(parents=True, exist_ok=True)

MASTER = 1024  # dibuja grande y downscale con LANCZOS

# Paleta del gradient (ambar -> naranja profundo)
GRAD_TOP = "#FFC04C"
GRAD_BOT = "#E88A10"

# Color del glifo
GLYPH = "#FFFFFF"


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def vertical_gradient(size: tuple[int, int], top: str, bottom: str) -> Image.Image:
    w, h = size
    t = hex_to_rgb(top)
    b = hex_to_rgb(bottom)
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        f = y / (h - 1) if h > 1 else 0.0
        row = (lerp(t[0], b[0], f), lerp(t[1], b[1], f), lerp(t[2], b[2], f))
        for x in range(w):
            px[x, y] = row
    return img


def squircle_mask(size: int, radius_ratio: float = 0.22) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    r = int(size * radius_ratio)
    d.rounded_rectangle([(0, 0), (size - 1, size - 1)], radius=r, fill=255)
    return m


def gear_polygon(
    cx: float, cy: float, teeth: int,
    r_inner: float, r_outer: float,
    tooth_base_frac: float = 0.55,
    tooth_tip_frac: float = 0.40,
) -> list[tuple[float, float]]:
    """Devuelve los vertices de un engranaje como poligono cerrado.
    `tooth_base_frac`: fraccion del arco ocupado por la base del diente.
    `tooth_tip_frac`: fraccion del arco ocupado por la punta (mas pequena => trapecio).
    """
    points: list[tuple[float, float]] = []
    step = 2 * math.pi / teeth
    half_base = step * tooth_base_frac / 2
    half_tip = step * tooth_tip_frac / 2
    for i in range(teeth):
        base = i * step - math.pi / 2  # empieza arriba
        a1 = base - half_base   # inner izq
        a2 = base - half_tip    # outer izq
        a3 = base + half_tip    # outer der
        a4 = base + half_base   # inner der
        for a, r in ((a1, r_inner), (a2, r_outer), (a3, r_outer), (a4, r_inner)):
            points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return points


def make_gear_layer(size: int, color: str = GLYPH) -> Image.Image:
    """RGBA con engranaje centrado. Engranaje con cuerpo + hueco centrado."""
    s = size
    layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    cx = cy = s / 2
    r_outer = s * 0.40
    r_inner = s * 0.32
    r_body = s * 0.30   # cuerpo solido (un pelo menor que r_inner para empalme suave)
    r_hole = s * 0.14   # hueco central

    # Dientes (poligono)
    pts = gear_polygon(cx, cy, teeth=8, r_inner=r_inner, r_outer=r_outer,
                       tooth_base_frac=0.55, tooth_tip_frac=0.42)
    d.polygon(pts, fill=color)

    # Cuerpo (disco) — cubre las bases de los dientes y las une suave
    d.ellipse((cx - r_body, cy - r_body, cx + r_body, cy + r_body), fill=color)

    # Hueco central — horadamos (alpha=0) para dejar transparente
    d.ellipse((cx - r_hole, cy - r_hole, cx + r_hole, cy + r_hole), fill=(0, 0, 0, 0))

    return layer


def compose_master() -> Image.Image:
    """Ensambla el squircle gradient + engranaje blanco + highlight sutil."""
    size = MASTER
    # 1. Gradient vertical
    grad = vertical_gradient((size, size), GRAD_TOP, GRAD_BOT).convert("RGBA")
    # 2. Recorta con squircle
    mask = squircle_mask(size, radius_ratio=0.22)
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg.paste(grad, (0, 0), mask=mask)

    # 3. Highlight sutil tipo "cristal" en el top (Apple icon vibe)
    highlight = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    # Elipse blanca translucida en la mitad superior
    hd.ellipse(
        (int(size * 0.06), int(-size * 0.55),
         int(size * 0.94), int(size * 0.48)),
        fill=(255, 255, 255, 38),
    )
    # Recorto el highlight al squircle para que no salga
    highlight.putalpha(
        Image.eval(Image.merge("L", (highlight.split()[3],))
                   .resize((size, size)), lambda v: v)
    )
    highlight = Image.composite(highlight, Image.new("RGBA", (size, size), 0), mask)
    bg = Image.alpha_composite(bg, highlight)

    # 4. Engranaje blanco con un mini drop-shadow para "despegarlo"
    gear = make_gear_layer(size, color=GLYPH)
    shadow = gear.copy()
    shadow_alpha = shadow.split()[3].point(lambda v: int(v * 0.35))
    shadow.putalpha(shadow_alpha)
    # tintar shadow a negro
    black = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    black.putalpha(shadow_alpha)
    black = black.filter(ImageFilter.GaussianBlur(radius=max(2, size // 120)))
    offset = max(1, size // 180)
    bg.paste(black, (0, offset), mask=black.split()[3])
    bg = Image.alpha_composite(bg, gear)

    return bg


def export_sizes(master: Image.Image):
    sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 180, 192, 256, 384, 512, 1024]
    for s in sizes:
        img = master.resize((s, s), resample=Image.LANCZOS)
        img.save(OUT / f"logo_{s}.png", "PNG")
    # ICO multi-size (Windows)
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(OUT / "logo.ico", format="ICO", sizes=ico_sizes)
    # Alias "logo.png" (256 para Tk PhotoImage)
    master.resize((256, 256), Image.LANCZOS).save(OUT / "logo.png", "PNG")


def export_wordmark(master: Image.Image):
    """Logo + wordmark en una sola imagen, util para splash/header."""
    W = 1600
    H = 480
    canvas = Image.new("RGBA", (W, H), (10, 12, 16, 255))  # BG_BASE
    logo = master.resize((320, 320), Image.LANCZOS)
    canvas.paste(logo, (80, (H - 320) // 2), mask=logo)

    # Texto "Dofus Autofarm"
    try:
        font_big = ImageFont.truetype("seguisb.ttf", 130)    # Segoe UI Semibold
    except OSError:
        try:
            font_big = ImageFont.truetype("segoeuib.ttf", 130)
        except OSError:
            font_big = ImageFont.load_default()
    try:
        font_sm = ImageFont.truetype("segoeui.ttf", 40)
    except OSError:
        font_sm = ImageFont.load_default()

    d = ImageDraw.Draw(canvas)
    tx = 80 + 320 + 40
    ty = H // 2 - 90
    d.text((tx, ty), "Dofus Autofarm", fill=(230, 232, 236, 255), font=font_big)
    d.text((tx, ty + 150), "retro 1.29.1  ·  sniffer + visual",
           fill=(107, 114, 128, 255), font=font_sm)
    canvas.convert("RGB").save(OUT / "logo_wordmark.png", "PNG")


def main():
    print(f"[logo] output dir: {OUT}")
    master = compose_master()
    master.save(OUT / "logo_master.png", "PNG")
    export_sizes(master)
    export_wordmark(master)
    print("[logo] done. Files:")
    for p in sorted(OUT.iterdir()):
        print(f"   - {p.name}  ({p.stat().st_size // 1024}kb)")


if __name__ == "__main__":
    main()
