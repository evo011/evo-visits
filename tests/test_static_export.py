import json
import struct
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from evo.config import Config
from evo.static_export import export_site, slug
from evo.store import Reading, Store

from .test_server import FakeContext, NOW, synth_values, synth_weather

NOW_TS = int(NOW.timestamp())
TODAY = NOW.date()


class SlugTestCase(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(slug("EVO Dern'sche Höfe"), "evo-dern-sche-hoefe")
        self.assertEqual(slug("EVO Stadthausbrücke"), "evo-stadthausbruecke")
        self.assertEqual(slug("EVO Prager Straße"), "evo-prager-strasse")

    def test_collapses_and_trims_dashes(self):
        self.assertEqual(slug("A -- B!"), "a-b")
        self.assertEqual(slug("--x--"), "x")


class ExportSiteTestCase(unittest.TestCase):
    """export_site in ein TempDir: Dateien da, Slugs stimmen, Altes wird geloescht."""

    GYMS = ("EVO Unterbilk", "EVO Schanze")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.out = Path(cls.tmp.name) / "site"
        cls.store = Store(str(Path(cls.tmp.name) / "visits.db"))
        start = TODAY - timedelta(days=9)
        for i, gym in enumerate(cls.GYMS):
            items = [(ts, Reading(gym, count=v))
                     for ts, v in synth_values(start, 10, seed=i + 1)]
            cls.store.add_batch(items)
        cls.cfg = Config()
        cls.cfg.db_path = str(Path(cls.tmp.name) / "visits.db")

    def test_export(self):
        # veraltete Datei im Ausgabeverzeichnis, die danach weg sein muss
        (self.out / "api" / "gym").mkdir(parents=True, exist_ok=True)
        (self.out / "api" / "weekly").mkdir(parents=True, exist_ok=True)
        (self.out / "api" / "gym" / "alt.json").write_text("{}", encoding="utf-8")
        # veraltete Schriftdatei, die ersetzt werden muss
        (self.out / "fonts").mkdir(exist_ok=True)
        (self.out / "fonts" / "alt.woff2").write_bytes(b"alt")
        # veraltetes Icon, das ersetzt werden muss
        (self.out / "icons").mkdir(exist_ok=True)
        (self.out / "icons" / "alt.png").write_bytes(b"alt")

        ctx = FakeContext(weather_map=synth_weather())
        res = export_site(self.cfg, self.store, self.out, ctx=ctx, now=NOW_TS)

        self.assertEqual(res["gyms"], 2)
        self.assertEqual(res["out"], str(self.out))
        # index.html mit Static-Marker, .nojekyll leer
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn("window.EVO_STATIC", html)
        self.assertIn(f'"generated_at": {NOW_TS}', html)
        self.assertIn("<script>", html)
        self.assertEqual((self.out / ".nojekyll").read_text(encoding="utf-8"), "")

        # Schriften mitgeliefert, index.html laedt sie lokal statt von Google Fonts
        self.assertTrue((self.out / "fonts" / "archivo.woff2").is_file())
        self.assertTrue((self.out / "fonts" / "OFL-Archivo.txt").is_file())
        self.assertFalse((self.out / "fonts" / "alt.woff2").exists())
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertIn("fonts/archivo.woff2", html)

        # PWA: Manifest, Service Worker und Icons mitgeliefert, Altes geloescht
        self.assertTrue((self.out / "manifest.webmanifest").is_file())
        self.assertTrue((self.out / "sw.js").is_file())
        for icon in ("icon.svg", "icon-192.png", "icon-512.png",
                     "icon-maskable-512.png", "apple-touch-icon.png"):
            self.assertTrue((self.out / "icons" / icon).is_file(), icon)
        self.assertFalse((self.out / "icons" / "alt.png").exists())

        manifest = json.loads((self.out / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "EVO Auslastung")
        self.assertEqual(manifest["start_url"], "./")
        self.assertEqual(manifest["display"], "standalone")

        def png_size(p):
            # Breite/Hoehe aus dem IHDR-Chunk (Bytes 16-24)
            return struct.unpack(">II", p.read_bytes()[16:24])

        for icon in manifest["icons"]:
            p = self.out / icon["src"]
            self.assertTrue(p.is_file(), icon["src"])
            if p.name.endswith(".png"):
                w, h = png_size(p)
                self.assertEqual(icon["sizes"], f"{w}x{h}", icon["src"])

        # index.html verweist auf Manifest und Apple-Icon; der Static-Marker
        # sitzt vor dem App-Script, die SW-Registrierung dahinter
        self.assertIn('rel="manifest"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn("serviceWorker.register", html)
        self.assertLess(html.index("window.EVO_STATIC"), html.index('"use strict"'))
        self.assertGreater(html.index("serviceWorker.register"), html.index('"use strict"'))

        def load(p):
            return json.loads((self.out / p).read_text(encoding="utf-8"))

        status = load("api/status.json")
        self.assertTrue(status["static"])
        self.assertEqual(status["generated_at"], NOW_TS)

        gyms = load("api/gyms.json")
        self.assertEqual(len(gyms), 2)
        by_name = {g["gym"]: g for g in gyms}
        slugs = {g["gym"]: g["slug"] for g in gyms}
        self.assertEqual(slugs["EVO Unterbilk"], "evo-unterbilk")
        self.assertEqual(slugs["EVO Schanze"], "evo-schanze")

        for name in self.GYMS:
            s = slugs[name]
            gym = load(f"api/gym/{s}.json")
            self.assertNotIn("error", gym)
            self.assertIn("forecast", gym)
            weekly = load(f"api/weekly/{s}.json")
            self.assertNotIn("error", weekly)
            self.assertEqual(weekly["gym"], name)

        csv = (self.out / "api" / "export.csv").read_text(encoding="utf-8")
        self.assertIn("timestamp_utc", csv)

        # veraltete Datei geloescht, nur die beiden Slugs uebrig
        self.assertFalse((self.out / "api" / "gym" / "alt.json").exists())
        self.assertEqual(sorted(p.name for p in (self.out / "api" / "gym").iterdir()),
                         ["evo-schanze.json", "evo-unterbilk.json"])
        self.assertEqual(sorted(p.name for p in (self.out / "api" / "weekly").iterdir()),
                         ["evo-schanze.json", "evo-unterbilk.json"])

    def test_export_without_ctx(self):
        out = Path(self.tmp.name) / "site2"
        res = export_site(self.cfg, self.store, out, now=NOW_TS)
        self.assertEqual(res["gyms"], 2)
        self.assertTrue((out / "api" / "status.json").exists())
        accuracy = json.loads((out / "api" / "accuracy.json").read_text(encoding="utf-8"))
        self.assertEqual(accuracy["days"], 0)
        self.assertEqual(accuracy["generated_at"], NOW_TS)

    def test_export_with_snapshot_dir(self):
        from evo.snapshot import HEADER

        snap = Path(self.tmp.name) / "forecasts"
        snap.mkdir()
        target = NOW_TS - 3600  # 11:00 lokale Zeit, Zielfenster laengst vorbei
        gym = self.GYMS[0]
        row = ("2026-10-05", NOW_TS - 7 * 86400, gym, target,
               datetime.fromtimestamp(target, NOW.tzinfo).strftime("%Y-%m-%d %H:%M"),
               "161.0", 41, 39, 43, "", "", "", "", "", "abc1234")
        (snap / "forecast-2026-10.csv").write_text(
            ",".join(HEADER) + "\n" + ",".join(str(x) for x in row) + "\n",
            encoding="utf-8")

        out = Path(self.tmp.name) / "site3"
        res = export_site(self.cfg, self.store, out, ctx=FakeContext(weather_map=synth_weather()),
                          now=NOW_TS, snapshot_dir=snap)
        self.assertEqual(res["gyms"], 2)
        accuracy = json.loads((out / "api" / "accuracy.json").read_text(encoding="utf-8"))
        self.assertEqual(accuracy["generated_at"], NOW_TS)
        self.assertGreaterEqual(accuracy["days"], 1)
        self.assertIn("4_7d", accuracy["all"])
        self.assertEqual(accuracy["all"]["4_7d"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
