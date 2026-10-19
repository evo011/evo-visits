import importlib.util
import re
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
MODULE_PATH = TOOLS_DIR / "make_icons.py"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def load_module():
    spec = importlib.util.spec_from_file_location("make_icons", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decode_png(data: bytes):
    """Minimaler RGBA-PNG-Decoder: (breite, hoehe, pixel[zeile][spalte]=(r,g,b,a))."""
    assert data.startswith(PNG_SIGNATURE)
    pos = len(PNG_SIGNATURE)
    ihdr = None
    idat = b""
    while pos < len(data):
        length, tag = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        assert crc == zlib.crc32(tag + chunk) & 0xFFFFFFFF
        if tag == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", chunk)
        elif tag == b"IDAT":
            idat += chunk
        pos += 12 + length
    width, height, depth, colortype = ihdr[0], ihdr[1], ihdr[2], ihdr[3]
    assert (depth, colortype) == (8, 6)
    raw = zlib.decompress(idat)
    stride = width * 4
    assert len(raw) == height * (stride + 1)
    pixels = []
    for y in range(height):
        row_start = y * (stride + 1)
        assert raw[row_start] == 0, "Filter-Byte muss 0 sein"
        row = raw[row_start + 1:row_start + 1 + stride]
        pixels.append([tuple(row[x * 4:x * 4 + 4]) for x in range(width)])
    return width, height, pixels


class MakeIconsTestCase(unittest.TestCase):
    """Skript in ein TemporaryDirectory laufen lassen und die Ausgaben pruefen."""

    EXPECTED = ("icon-192.png", "icon-512.png", "icon-maskable-512.png",
                "apple-touch-icon.png", "icon.svg")
    SIZES = {"icon-192.png": 192, "icon-512.png": 512,
             "icon-maskable-512.png": 512, "apple-touch-icon.png": 180}

    @classmethod
    def setUpClass(cls):
        cls.module = load_module()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.out = Path(cls.tmp.name)
        cls.result = cls.module.build(cls.out)
        cls.pngs = {}
        for name in cls.EXPECTED[:4]:
            data = (cls.out / name).read_bytes()
            cls.pngs[name] = decode_png(data)

    def test_all_files_exist(self):
        for name in self.EXPECTED:
            self.assertTrue((self.out / name).is_file(), name)

    def test_png_signature_and_dimensions(self):
        for name, size in self.SIZES.items():
            width, height, _ = self.pngs[name]
            self.assertEqual((width, height), (size, size), name)
            self.assertTrue((self.out / name).read_bytes().startswith(PNG_SIGNATURE))

    def test_icon_512_corner_transparent(self):
        _, _, px = self.pngs["icon-512.png"]
        self.assertEqual(px[0][0][3], 0)
        self.assertEqual(px[0][511][3], 0)
        self.assertEqual(px[511][0][3], 0)
        self.assertEqual(px[511][511][3], 0)

    def test_icon_512_bar_colors(self):
        _, _, px = self.pngs["icon-512.png"]
        # Mitte des hoechsten Balkens (Balken 4, x=29.5..33.5 im 64er-Raster);
        # Gruppe auf 62 % Breite horizontal zentriert
        scale = 0.62 * 512 / 46.0
        group_left = (512 - 46.0 * scale) / 2.0
        x = int(group_left + (31.5 - 8.5) * scale)
        base_y = 0.78 * 512
        y = int(base_y - (38.0 * scale) / 2.0)  # Balkenmitte
        self.assertEqual(px[y][x], (0xF4, 0xF4, 0xF1, 0xFF))
        # gruen: Balken 7, x=50.5..54.5, Hoehe 10
        xg = int(group_left + (52.5 - 8.5) * scale)
        yg = int(base_y - 5.0 * scale)
        self.assertEqual(px[yg][xg], (0x2D, 0x8A, 0x4A, 0xFF))

    def test_maskable_corner_opaque_background(self):
        _, _, px = self.pngs["icon-maskable-512.png"]
        self.assertEqual(px[0][0], (0x17, 0x18, 0x1A, 0xFF))

    @staticmethod
    def is_bar_pixel(pixel):
        r, g, b, a = pixel
        return a == 255 and (r, g, b) != (0x17, 0x18, 0x1A)

    def test_icon_512_horizontally_centered(self):
        _, _, px = self.pngs["icon-512.png"]
        cols = [x for x in range(512) if any(self.is_bar_pixel(px[y][x]) for y in range(512))]
        self.assertTrue(cols)
        # linke und rechte Randspalte duerfen sich um hoechstens 1 px unterscheiden
        self.assertLessEqual(abs(cols[0] - (511 - cols[-1])), 1)

    def test_maskable_centered(self):
        _, _, px = self.pngs["icon-maskable-512.png"]
        cols = [x for x in range(512) if any(self.is_bar_pixel(px[y][x]) for y in range(512))]
        self.assertTrue(cols)
        self.assertLessEqual(abs(cols[0] - (511 - cols[-1])), 1)
        rows = [y for y in range(512) if any(self.is_bar_pixel(px[y][x]) for x in range(512))]
        self.assertTrue(rows)
        self.assertLessEqual(abs(rows[0] - (511 - rows[-1])), 1)

    def test_icon_512_baseline(self):
        _, _, px = self.pngs["icon-512.png"]
        rows = [y for y in range(512) if any(self.is_bar_pixel(px[y][x]) for x in range(512))]
        self.assertAlmostEqual(rows[-1], 0.78 * 512, delta=1.0)

    def test_svg_content(self):
        svg = (self.out / "icon.svg").read_text(encoding="utf-8")
        self.assertIn('viewBox="0 0 64 64"', svg)
        self.assertIn("#2d8a4a", svg)
        bars = re.findall(
            r'<rect x="([0-9.]+)" y="([0-9.]+)" width="[0-9.]+" height="([0-9.]+)"', svg)
        self.assertEqual(len(bars), 7)
        # erster Balken beginnt bei (64 - 0.62*64)/2 (Gruppe 62 % breit, zentriert)
        self.assertAlmostEqual(float(bars[0][0]), (64 - 0.62 * 64) / 2, delta=0.01)
        # Grundlinie bei 78 %: groesste Balkenunterkante = 0.78*64
        bottoms = [float(y) + float(h) for _, y, h in bars]
        self.assertAlmostEqual(max(bottoms), 0.78 * 64, delta=0.01)

    def test_deterministic_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out2 = Path(tmp)
            self.module.build(out2)
            for name in self.EXPECTED:
                self.assertEqual((out2 / name).read_bytes(),
                                 (self.out / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
