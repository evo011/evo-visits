import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "web" / "bilk" / "index.html"
MANIFEST = ROOT / "web" / "bilk" / "manifest.webmanifest"
INDEX = ROOT / "web" / "index.html"


class BilkPageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")

    def test_page_exists(self):
        self.assertTrue(PAGE.is_file())

    def test_references_data_and_assets(self):
        self.assertIn("api/bilk.json", self.html)
        self.assertIn("../fonts/archivo.woff2", self.html)
        self.assertIn("../sw.js", self.html)
        self.assertIn("evo.theme", self.html)
        self.assertIn('lang="de"', self.html)

    def test_no_external_scripts_or_stylesheets(self):
        self.assertNotRegex(self.html, r'<script[^>]+src="http')
        self.assertNotRegex(self.html, r'<link[^>]+rel="stylesheet"[^>]+href="http')
        self.assertNotRegex(self.html, r'<link[^>]+href="http[^"]*"[^>]+rel="stylesheet"')

    def test_manifest_is_valid(self):
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(data["short_name"], "Bilk")
        self.assertEqual(data["name"], "Schwimm' in Bilk")
        for icon in data["icons"]:
            self.assertTrue(icon["src"].startswith("../icons/"), icon["src"])

    def test_index_links_to_bilk(self):
        self.assertIn('href: "bilk/"', INDEX.read_text(encoding="utf-8"))

    def test_index_reads_gym_from_url(self):
        self.assertIn(
            'URLSearchParams(location.search).get("gym")',
            INDEX.read_text(encoding="utf-8"),
        )

    def test_page_has_quick_selection(self):
        for text in ('id="quickChips"', "Unterbilk", "LeFlair", "../?gym="):
            self.assertIn(text, self.html)

    def test_visible_text_uses_real_umlauts(self):
        self.assertIsNone(re.search(r"ge(oe|ae|ue)ffnet", self.html))


if __name__ == "__main__":
    unittest.main()
