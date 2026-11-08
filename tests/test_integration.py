"""Behavioral acceptance tests using a complete synthetic engagement."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tieout.engine import run_review

ROOT = Path(__file__).resolve().parent.parent


class EngagementTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "examples/demo.json").read_text())
        self.rules = json.loads((ROOT / "rules/core.json").read_text())
        self.settings = copy.deepcopy(self.data["demo_settings"])

    def results(self, settings=None):
        report = run_review(self.data, self.rules, settings or self.settings)
        return {r["rule_id"]: r for r in report["results"]}, report

    def test_deliberate_note_difference_and_scope_decisions(self):
        rows, report = self.results()
        self.assertEqual(rows["N-01"]["status"], "FAIL")
        self.assertEqual(rows["N-01"]["difference"], "15000")
        self.assertEqual(rows["FS-09"]["status"], "REVIEW_REQUIRED")
        self.assertEqual(rows["FS-10"]["status"], "NOT_APPLICABLE")
        self.assertTrue(rows["N-01"]["evidence"])
        self.assertEqual(report["input_snapshot"], self.data)
        self.assertEqual(report["rules_snapshot"], self.rules)
        self.assertGreater(len(rows), len(self.rules))

    def test_inclusive_tolerance_and_zero_override(self):
        self.settings["tolerance"] = "15000"
        rows, _ = self.results()
        self.assertEqual(rows["N-01"]["status"], "WITHIN_TOLERANCE")
        self.settings["overrides"]["N-01"] = "0"
        rows, _ = self.results()
        self.assertEqual(rows["N-01"]["status"], "FAIL")
        self.assertEqual(rows["N-01"]["tolerance"], "0")

    def test_ppe_gross_net_requires_confirmed_components(self):
        rows, report = self.results()
        self.assertEqual(rows["N-11"]["status"], "EXACT_MATCH")
        self.assertEqual(report["summary"]["total"], 44)
        self.assertEqual(report["summary"]["counts"], {
            "EXACT_MATCH": 41, "WITHIN_TOLERANCE": 0, "FAIL": 1,
            "REVIEW_REQUIRED": 1, "NOT_APPLICABLE": 1, "NOT_TESTED": 0,
        })
        for missing in (True, False):
            with self.subTest(condition="absent" if missing else "null"):
                settings = copy.deepcopy(self.settings)
                if missing:
                    settings["conditions"].pop("ppe_gross_net_components_confirmed")
                else:
                    settings["conditions"]["ppe_gross_net_components_confirmed"] = None
                rows, _ = self.results(settings)
                self.assertEqual(rows["N-11"]["status"], "REVIEW_REQUIRED")

    def test_missing_restricted_cash_is_not_an_implicit_zero(self):
        self.data["facts"] = [f for f in self.data["facts"]
                              if f["key"] != "BS.restricted_cash_current"]
        rows, _ = self.results()
        self.assertEqual(rows["FS-01"]["status"], "NOT_TESTED")

    def test_cli_creates_auditable_output_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "run.json"
            command = [sys.executable, "-m", "tieout", "review",
                       "examples/demo.json", "--reviewer", "Test reviewer",
                       "--reason", "Acceptance test", "--output", str(output)]
            first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            report = json.loads(output.read_text())
            self.assertTrue(report["input_sha256"])
            original = output.read_bytes()
            second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(second.returncode, 2)
            self.assertEqual(original, output.read_bytes())


if __name__ == "__main__":
    unittest.main()
