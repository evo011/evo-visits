import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class BilkWorkflowTests(unittest.TestCase):
    def test_workflow_collects_and_exports_bilk(self):
        workflow = (ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8")
        self.assertIn("evo_visits.py bilk-collect", workflow)
        self.assertIn("evo_visits.py bilk-export --out site", workflow)
        self.assertIn("data/bilk-*.csv", workflow)

    def test_gitignore_keeps_bilk_csvs(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("!data/bilk-*.csv", gitignore.splitlines())


if __name__ == "__main__":
    unittest.main()
