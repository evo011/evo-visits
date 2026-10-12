"""Ende-zu-Ende gegen einen lokalen Fake-Server mit drei typischen Seitenbauarten.

Die Browser-Tests laufen nur, wenn Playwright installiert ist. Mit EVO_TEST_CHROMIUM kann ein
eigener Chromium-Pfad gesetzt werden.
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from evo import fetch
from evo.config import Config

CLUBS = [{"id": 1, "name": "HAMBURG ALTONA", "current": 42, "max": 120},
         {"id": 2, "name": "BERLIN MITTE", "current": 17, "max": 90},
         {"id": 3, "name": "KÖLN EHRENFELD", "current": 5, "max": 80}]

PAGES = {
    # 1. Seite laedt per Bundle eine JSON-API, URL steht im Bundle
    "/static/": ("text/html", '<html><body><div id="app"></div><script src="/static/app.js"></script></body></html>'),
    "/static/app.js": ("application/javascript",
                       'fetch("/api/v1/visits").then(r => r.json()).then(render);'),
    "/api/v1/visits": ("application/json", json.dumps({"data": CLUBS})),
    # 2. API-URL wird zur Laufzeit zusammengebaut, nur ein echter Browser findet sie
    "/spa/": ("text/html", """<html><body><div id="app">lade</div><script>
        const u = ['', 'internal', 'occ' + 'upancy'].join('/');
        fetch(u).then(r => r.json()).then(d => {
          document.getElementById('app').innerHTML = d.items.map(c =>
            `<div class="club"><h3>${c.title}</h3><span>${c.people} Personen</span></div>`).join('');
        });</script></body></html>"""),
    "/internal/occupancy": ("application/json", json.dumps(
        {"items": [{"title": c["name"], "people": c["current"]} for c in CLUBS]})),
    # 3. Einzel-Club-Ansicht mit Dropdown
    "/select/": ("text/html", """<html><body><h1>Visits</h1>
        <select id="club"><option value="">Club waehlen</option>
          <option value="1">HAMBURG ALTONA</option><option value="2">BERLIN MITTE</option>
          <option value="3">KÖLN EHRENFELD</option></select>
        <div id="out"></div><script>
        document.getElementById('club').addEventListener('change', e => {
          fetch('/club-status?id=' + e.target.value).then(r => r.json()).then(d => {
            document.getElementById('out').textContent = d.current + ' Personen';
          });
        });</script></body></html>"""),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/club-status":
            cid = int(self.path.split("id=")[1])
            c = next(c for c in CLUBS if c["id"] == cid)
            body, ctype = json.dumps({"current": c["current"], "max": c["max"]}), "application/json"
        elif path in PAGES:
            ctype, body = PAGES[path]
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FetchE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "source.json"
        self.patch = mock.patch.object(fetch, "SOURCE_CACHE", self.cache)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def cfg(self, path, mode):
        return Config(url=self.base + path, mode=mode, db_path=os.path.join(self.tmp.name, "t.db"),
                      browser_path=os.environ.get("EVO_TEST_CHROMIUM"))

    def test_static_discovers_api_and_caches_it(self):
        res = fetch.fetch_readings(self.cfg("/static/", "static"))
        self.assertEqual({r.gym: r.count for r in res.readings},
                         {"HAMBURG ALTONA": 42, "BERLIN MITTE": 17, "KÖLN EHRENFELD": 5})
        self.assertTrue(res.source.endswith("/api/v1/visits"))
        cached = json.loads(self.cache.read_text())
        self.assertEqual(cached["url"], self.base + "/api/v1/visits")
        # Zweiter Lauf geht direkt ueber den Cache
        res2 = fetch.fetch_readings(self.cfg("/static/", "static"))
        self.assertEqual(len(res2.readings), 3)

    def test_static_fails_cleanly_on_spa(self):
        with self.assertRaises(fetch.FetchError):
            fetch.fetch_readings(self.cfg("/spa/", "static"))

    @unittest.skipUnless(fetch.playwright_available(), "Playwright nicht installiert")
    def test_browser_captures_runtime_api(self):
        res = fetch.fetch_readings(self.cfg("/spa/", "browser"))
        self.assertEqual({r.gym: r.count for r in res.readings}["BERLIN MITTE"], 17)
        self.assertIn("/internal/occupancy", res.source)

    @unittest.skipUnless(fetch.playwright_available(), "Playwright nicht installiert")
    def test_browser_iterates_dropdown(self):
        res = fetch.fetch_readings(self.cfg("/select/", "browser"))
        self.assertEqual(res.source, "browser:select")
        got = {r.gym: (r.count, r.capacity) for r in res.readings}
        self.assertEqual(got, {"HAMBURG ALTONA": (42, 120), "BERLIN MITTE": (17, 90), "KÖLN EHRENFELD": (5, 80)})


if __name__ == "__main__":
    unittest.main()
