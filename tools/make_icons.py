#!/usr/bin/env python3
"""Erzeugt die PWA-Icons (PNG + SVG) unter web/icons/ aus dem Favicon-Motiv.

Nur Python-Standardbibliothek (zlib, struct, pathlib, argparse) -- das Repo
hat bewusst keine Paket-Abhaengigkeiten, also kein Pillow. PNG-Writer und
Kantenglaettung sind selbst implementiert.
"""

from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path

# Motiv im 64er-Raster (siehe Favicon in web/index.html):
# 7 Balken, je 4 breit, auf gemeinsamer Grundlinie y=64 im 64er-Raster.
BG = (0x17, 0x18, 0x1A)
FG = (0xF4, 0xF4, 0xF1)
GREEN = (0x2D, 0x8A, 0x4A)
BAR_WIDTH = 4.0
BAR_YS = [50.0, 42.0, 34.0, 26.0, 38.0, 46.0, 54.0]  # Balken-Oberkanten bei Grundlinie 64
BAR_XS = [8.5, 15.5, 22.5, 29.5, 36.5, 43.5, 50.5]
GROUP_X0 = 8.5  # linker Rand der Balkengruppe im 64er-Raster
GROUP_W = 46.0  # 8.5 .. 54.5
GROUP_H = 38.0  # hoechster Balken: 64 - 26


def bars() -> list[tuple[float, float, float, float, tuple[int, int, int]]]:
    """Balken als (x, y, w, h, farbe) im 64er-Raster, Ursprung links oben."""
    out = []
    for i, (x, top) in enumerate(zip(BAR_XS, BAR_YS)):
        color = GREEN if i == 6 else FG
        out.append((x, top, BAR_WIDTH, 64.0 - top, color))
    return out


def hex_color(color: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*color)


def scaled_rects(size: float, bar_scale: float,
                 baseline_frac: float | None) -> list[tuple[float, float, float, float, tuple[int, int, int]]]:
    """Balken-Rechtecke in Pixekoordinaten.

    Die Gruppe wird auf bar_scale*size Breite skaliert und horizontal
    zentriert (Gruppenmitte = Bildmitte). baseline_frac legt die Grundlinie
    als Anteil der Hoehe fest; None bedeutet vertikal zentriert (Gruppenmitte
    = Bildmitte), wie fuer maskable/apple-Touch.
    """
    scale = bar_scale * size / GROUP_W
    # Gruppenmitte aufs Bildzentrum legen: linker Rand der Gruppe (x=GROUP_X0)
    # muss bei (size - GROUP_W*scale)/2 liegen.
    off_x = (size - GROUP_W * scale) / 2.0 - GROUP_X0 * scale
    if baseline_frac is None:
        base_y = size / 2.0 + (GROUP_H * scale) / 2.0
    else:
        base_y = baseline_frac * size
    return [(off_x + x * scale, base_y - (64.0 - top) * scale, w * scale, h * scale, c)
            for (x, top, w, h, c) in bars()]


def coverage_rect(px: float, py: float,
                  rect: tuple[float, float, float, float, tuple]) -> float:
    """Flaechenabdeckung eines Pixels (px,py)..(px+1,py+1) durch ein achsparalleles Rechteck.

    `rect` ist (x, y, w, h, farbe); x/y-Intervalle separat schneiden,
    Ueberlappungslaengen multiplizieren.
    """
    rx, ry, rw, rh = rect[:4]
    ox = max(0.0, min(px + 1.0, rx + rw) - max(px, rx))
    if ox <= 0.0:
        return 0.0
    oy = max(0.0, min(py + 1.0, ry + rh) - max(py, ry))
    return ox * oy if oy > 0.0 else 0.0


def in_rounded_square(x: float, y: float, size: float, radius: float) -> bool:
    """Punkt (x,y) im abgerundeten Quadrat der Kantenlaenge `size`?"""
    if radius <= 0.0:
        return 0.0 <= x < size and 0.0 <= y < size
    r = radius
    cx = min(max(x, r), size - r)
    cy = min(max(y, r), size - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render_icon(size: int, rects: list[tuple], radius: float
                 ) -> list[list[tuple[int, int, int, int]]]:
    """Rendert Hintergrund plus Balken; radius > 0 rundet Ecken ab (transparent)."""
    rows: list[list[tuple[int, int, int, int]]] = []
    for py in range(size):
        row: list[tuple[int, int, int, int]] = []
        for px in range(size):
            bg_alpha = 1.0
            if radius > 0.0 and not in_rounded_square(
                    px + 0.5, py + 0.5, float(size), radius):
                # Eck-Pixel: 4x4-Supersampling gegen die Rundung
                hits = sum(1 for i in range(4) for j in range(4)
                           if in_rounded_square(px + (i + 0.5) / 4.0,
                                                py + (j + 0.5) / 4.0,
                                                float(size), radius))
                bg_alpha = hits / 16.0
            if bg_alpha == 0.0:
                row.append((0, 0, 0, 0))
                continue
            color = BG
            for rect in rects:
                cov = coverage_rect(float(px), float(py), rect)
                if cov <= 0.0:
                    continue
                # Balkenfarbe mit Abdeckung auf den bisherigen Hintergrund mischen
                a = min(1.0, cov)
                c = rect[4]
                color = tuple(round(c[k] * a + color[k] * (1.0 - a)) for k in range(3))
            row.append((color[0], color[1], color[2], round(255 * bg_alpha)))
        rows.append(row)
    return rows


def rows_to_png(size: int, rows: list[list[tuple[int, int, int, int]]]) -> bytes:
    """RGBA-PNG (8 Bit, Filter 0 je Zeile) mit eigenem minimalen Writer."""
    raw = b"".join(
        b"\x00" + struct.pack(f">{size * 4}B", *(v for px in row for v in px))
        for row in rows
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def fmt(value: float) -> str:
    """Zahl mit hoechstens 3 Nachkommastellen, ohne haengende Nullen."""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def icon_svg() -> str:
    """Skalierbares Icon als SVG mit dem Layout von icon-512.png (62 %, Grundlinie 78 %)."""
    parts = [f'<rect width="64" height="64" rx="12" fill="{hex_color(BG)}"/>']
    for (x, y, w, h, c) in scaled_rects(64.0, 0.62, 0.78):
        parts.append(f'<rect x="{fmt(x)}" y="{fmt(y)}" width="{fmt(w)}" '
                     f'height="{fmt(h)}" fill="{hex_color(c)}"/>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            + "".join(parts) + "</svg>\n")


def build(out_dir: Path) -> list[tuple[str, int]]:
    """Erzeugt alle Dateien, gibt (pfad, groesse)-Paare zurueck."""
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[tuple[str, int]] = []
    # (name, size, bar_scale, baseline_frac, radius); radius 0 => volle Flaeche
    pngs = [
        ("icon-192.png", 192, 0.62, 0.78, 36.0),
        ("icon-512.png", 512, 0.62, 0.78, 96.0),
        ("icon-maskable-512.png", 512, 0.50, None, 0.0),
        ("apple-touch-icon.png", 180, 0.56, None, 0.0),
    ]
    for name, size, scale, baseline, radius in pngs:
        rows = render_icon(size, scaled_rects(size, scale, baseline), radius)
        data = rows_to_png(size, rows)
        (out_dir / name).write_bytes(data)
        outputs.append((str(out_dir / name), size))
    (out_dir / "icon.svg").write_text(icon_svg(), encoding="utf-8")
    outputs.append((str(out_dir / "icon.svg"), 64))
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PWA-Icons nach web/icons/ schreiben")
    parser.add_argument("--out", default="web/icons", help="Ausgabeverzeichnis")
    args = parser.parse_args(argv)
    for path, size in build(Path(args.out)):
        print(f"{path} {size}x{size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
