import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from tieout.export import export_csv, export_json, export_xlsx, write_audit_run


def example_result():
    return {
        "schema_version": 1, "engine_version": "0.1.0", "run_id": "test-run",
        "created_at": "2026-01-01T00:00:00+00:00", "entity": '=HYPERLINK("bad")',
        "period": "2026", "currency": "USD", "input_sha256": "abc",
        "rules_sha256": "def", "settings_sha256": "ghi",
        "settings": {"tolerance": "0", "reviewer": "user", "reason": "test"},
        "summary": {"total": 2},
        "results": [
            {"rule_id": "R-1", "name": "Exact", "status": "EXACT_MATCH", "lhs": "1", "rhs": "1", "evidence": []},
            {"rule_id": "R-2", "name": "  @SUM(A1)", "status": "WITHIN_TOLERANCE", "lhs": "12345678901234567890.12", "rhs": "12345678901234567890.13", "difference": "-0.01", "absolute_difference": "0.01", "tolerance": "1", "evidence": [{"source": "a.xlsx", "location": "TB!A1", "value": "12345678901234567890.12"}]},
        ],
        "bridge": [],
        "input_snapshot": {"facts": [{"key": "cash", "value": "12345678901234567890.12", "unit": "1", "source": "a.xlsx", "location": "A1"}], "tb": [], "mapping": []},
        "rules_snapshot": [{"id": "R-1", "lhs": [{"key": "a", "coefficient": 1}], "rhs": [{"key": "b", "coefficient": 1}]}],
    }


class ExportTests(unittest.TestCase):
    def test_json_is_complete_lossless_snapshot(self):
        result = example_result()
        self.assertEqual(json.loads(export_json(result)), result)

    def test_csv_keeps_within_tolerance_and_defangs_formulas(self):
        rows = list(csv.DictReader(io.StringIO(export_csv(example_result()).decode("utf-8-sig"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "WITHIN_TOLERANCE")
        self.assertEqual(rows[0]["entity"], '\'=HYPERLINK("bad")')
        self.assertEqual(rows[0]["name"], "'  @SUM(A1)")
        self.assertEqual(rows[0]["difference"], "'-0.01")
        self.assertEqual(rows[0]["lhs"], "12345678901234567890.12")
        self.assertEqual(json.loads(rows[0]["evidence"])[0]["location"], "TB!A1")

    def test_csv_can_include_all_checks(self):
        rows = list(csv.reader(io.StringIO(export_csv(example_result(), False).decode("utf-8-sig"))))
        self.assertEqual(len(rows), 3)

    def test_xlsx_is_valid_xml_and_literal_precision_is_preserved(self):
        with zipfile.ZipFile(io.BytesIO(export_xlsx(example_result()))) as archive:
            self.assertEqual(archive.testzip(), None)
            self.assertIn("[Content_Types].xml", archive.namelist())
            self.assertEqual(len([p for p in archive.namelist() if p.startswith("xl/worksheets/")]), 13)
            all_xml = []
            for name in archive.namelist():
                raw = archive.read(name)
                ElementTree.fromstring(raw)
                all_xml.append(raw.decode())
            content = "\n".join(all_xml)
            self.assertNotIn("<f>", content)
            self.assertIn("12345678901234567890.12", content)
            self.assertIn('=HYPERLINK("bad")', content)
            self.assertIn("settings_sha256", content)
            self.assertIn("engine_version", content)

    def test_note_matching_lineage_is_exported(self):
        result = example_result()
        result["note_matches"] = [{"key": "NOTE.ppe_net", "fsli": "BS.ppe_net", "status": "AUTO_MATCHED", "selected": {"note_id": "Note 14", "row_id": "ppe-net"}}]
        result["resolved_note_facts"] = [{"key": "NOTE.ppe_net", "value": "600", "note_id": "Note 14", "match_method": "AUTO_MATCHED"}]
        with zipfile.ZipFile(io.BytesIO(export_xlsx(result))) as archive:
            self.assertIn(b"Note 14", archive.read("xl/worksheets/sheet10.xml"))
            self.assertIn(b"AUTO_MATCHED", archive.read("xl/worksheets/sheet11.xml"))

    def test_excel_long_text_is_explicitly_marked_and_json_is_complete(self):
        result = example_result()
        result["results"][1]["name"] = "z" * 40000
        with zipfile.ZipFile(io.BytesIO(export_xlsx(result))) as archive:
            self.assertIn(b"truncated for Excel", archive.read("xl/worksheets/sheet2.xml"))
        self.assertEqual(len(json.loads(export_json(result))["results"][1]["name"]), 40000)

    def test_write_once_and_safe_ids(self):
        result = example_result()
        with tempfile.TemporaryDirectory() as folder:
            path = write_audit_run(result, folder)
            self.assertEqual(json.loads(path.read_text()), result)
            with self.assertRaises(FileExistsError):
                write_audit_run(result, folder)
            result["run_id"] = "../escape"
            with self.assertRaises(ValueError):
                write_audit_run(result, folder)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
