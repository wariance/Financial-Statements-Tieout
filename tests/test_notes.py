"""Content and scope acceptance tests for automatic note matching."""
import copy
import json
from pathlib import Path
import unittest

from tieout.notes import resolve_notes

ROOT = Path(__file__).resolve().parent.parent
MATCHERS = json.loads((ROOT / "rules/note_matchers.json").read_text())


def matcher(key="NOTE.ppe_net"):
    return copy.deepcopy(next(item for item in MATCHERS if item["key"] == key))


def row(identifier="net", label="Property, plant and equipment, net", value="285"):
    return {"id": identifier, "label": label, "value": value, "unit": "1000",
            "source": "draft-fs.xlsx", "location": "Notes!D12", "period": "FY2026",
            "currency": "USD", "basis": "consolidated"}


def data():
    return {"schema_version": 1, "entity": "Example Company", "period": "FY2026",
            "currency": "USD", "facts": [], "tb": [], "mapping": [],
            "notes": [{"id": "Note 3", "title": "Property, plant and equipment",
                       "text": "", "rows": [row()]}]}


def resolve(value=None, config=None, overrides=None):
    return resolve_notes(data() if value is None else value,
                         [matcher()] if config is None else config, overrides)


class NoteMatchTests(unittest.TestCase):
    def test_unique_semantic_match_preserves_lineage_and_original_value(self):
        result = resolve()
        self.assertEqual(result["matches"][0]["status"], "AUTO_MATCHED")
        fact = result["facts"][0]
        self.assertEqual((fact["value"], fact["unit"]), ("285", "1000"))
        self.assertEqual(fact["source"], "draft-fs.xlsx")
        self.assertEqual(fact["location"], "Notes!D12")
        self.assertEqual(fact["note_id"], "Note 3")
        self.assertEqual(fact["note_row_id"], "net")
        self.assertEqual(fact["match_method"], "AUTO_MATCHED")

    def test_numbers_and_order_do_not_determine_matching(self):
        original = json.loads((ROOT / "examples/demo.json").read_text())
        changed = copy.deepcopy(original)
        changed["entity"] = "Another Company"
        changed["notes"].reverse()
        for number, note in enumerate(changed["notes"], 41):
            note["id"] = f"Disclosure {number}"
            note["rows"].reverse()
        before = resolve(original, MATCHERS)
        after = resolve(changed, MATCHERS)
        values = lambda result: {f["key"]: (f["value"], f["unit"]) for f in result["facts"]}
        self.assertEqual(values(before), values(after))
        self.assertEqual(len(before["facts"]), 12)
        self.assertTrue(all(m["status"] == "AUTO_MATCHED" for m in after["matches"]))

    def test_note_identifier_is_never_a_topic(self):
        value = data()
        value["notes"][0].update(id="PPE property plant and equipment", title="Miscellaneous")
        self.assertEqual(resolve(value)["matches"][0]["status"], "NOT_FOUND")

    def test_note_body_can_supply_topic(self):
        value = data()
        value["notes"][0].update(title="Supporting schedules", text="Details of property, plant and equipment.")
        self.assertEqual(resolve(value)["matches"][0]["status"], "AUTO_MATCHED")

    def test_simplified_and_traditional_chinese_aliases(self):
        for title, caption in (("固定资产", "账面净值"), ("物業廠房及設備", "賬面淨值")):
            value = data()
            value["notes"][0]["title"] = title
            value["notes"][0]["rows"][0]["label"] = caption
            with self.subTest(title=title):
                self.assertEqual(resolve(value)["matches"][0]["status"], "AUTO_MATCHED")

    def test_gross_and_accumulated_depreciation_cannot_supply_net(self):
        value = data()
        value["notes"][0]["rows"] = [row("gross", "Gross carrying amount", "400"),
                                      row("depr", "Accumulated depreciation", "115")]
        result = resolve(value, [matcher(), matcher("NOTE.ppe_gross"), matcher("NOTE.ppe_accumulated_depreciation")])
        statuses = {m["key"]: m["status"] for m in result["matches"]}
        self.assertEqual(statuses["NOTE.ppe_net"], "NOT_FOUND")
        self.assertEqual(statuses["NOTE.ppe_gross"], "AUTO_MATCHED")
        self.assertEqual(statuses["NOTE.ppe_accumulated_depreciation"], "AUTO_MATCHED")

    def test_current_and_noncurrent_are_distinct_in_both_languages(self):
        for title, current, noncurrent in (("Borrowings", "Current borrowings", "Non-current borrowings"),
                                           ("借款", "流动部分", "非流动部分")):
            value = data()
            value["notes"][0].update(title=title, rows=[row("c", current, "10"), row("nc", noncurrent, "20")])
            result = resolve(value, [matcher("NOTE.debt_current"), matcher("NOTE.debt_noncurrent")])
            values = {f["key"]: f["value"] for f in result["facts"]}
            with self.subTest(title=title):
                self.assertEqual(values, {"NOTE.debt_current": "10", "NOTE.debt_noncurrent": "20"})

    def test_component_caption_does_not_masquerade_as_total(self):
        value = data()
        value["notes"][0].update(title="Accrued expenses", rows=[row("payroll", "Accrued expenses - payroll", "50")])
        self.assertEqual(resolve(value, [matcher("NOTE.accrued_expenses")])["matches"][0]["status"], "NOT_FOUND")

    def test_multiple_candidates_even_equal_amounts_are_ambiguous(self):
        value = data()
        value["notes"][0]["rows"].append(row("net-other"))
        result = resolve(value)
        self.assertEqual(result["matches"][0]["status"], "AMBIGUOUS")
        self.assertEqual(len(result["matches"][0]["candidates"]), 2)
        self.assertEqual(result["facts"], [])

    def test_generic_row_cannot_auto_supply_multiple_concepts(self):
        value = data()
        value["notes"][0].update(title="Property, plant and equipment and intangible assets", rows=[row(label="Net")])
        result = resolve(value, [matcher(), matcher("NOTE.intangibles_net")])
        self.assertTrue(all(m["status"] == "AMBIGUOUS" for m in result["matches"]))
        self.assertEqual(result["facts"], [])

    def test_missing_and_conflicting_scope_never_selects(self):
        for field, wrong in (("period", "FY2025"), ("currency", "EUR"), ("basis", "parent-only")):
            for replacement in (wrong, None):
                value = data()
                value["notes"][0]["rows"][0][field] = replacement
                with self.subTest(field=field, value=replacement):
                    result = resolve(value)
                    self.assertEqual(result["matches"][0]["status"], "SCOPE_MISMATCH")
                    self.assertEqual(result["facts"], [])

    def test_explicit_different_entity_on_note_or_row_is_rejected(self):
        for level in ("note", "row"):
            value = data()
            target = value["notes"][0] if level == "note" else value["notes"][0]["rows"][0]
            target["entity"] = "Different Company"
            with self.subTest(level=level):
                self.assertEqual(resolve(value)["matches"][0]["status"], "SCOPE_MISMATCH")

    def test_missing_amount_remains_null_not_zero(self):
        value = data()
        value["notes"][0]["rows"][0]["value"] = None
        result = resolve(value)
        self.assertEqual(result["matches"][0]["status"], "AUTO_MATCHED")
        self.assertIsNone(result["facts"][0]["value"])
        value["notes"][0]["rows"][0]["value"] = "0"
        self.assertEqual(resolve(value)["facts"][0]["value"], "0")

    def test_reviewed_override_can_resolve_ambiguity_or_unknown_alias(self):
        value = data()
        value["notes"][0]["rows"].append(row("alternate", "Closing carrying balance", "280"))
        override = {"NOTE.ppe_net": {"note_id": "Note 3", "row_id": "alternate", "reason": "Reviewed source caption and scope."}}
        result = resolve(value, overrides=override)
        self.assertEqual(result["matches"][0]["status"], "MANUAL_MATCHED")
        self.assertEqual(result["facts"][0]["value"], "280")
        self.assertIn("Reviewed source", result["matches"][0]["reason"])

    def test_override_cannot_bypass_scope_mismatch(self):
        value = data()
        value["notes"][0]["rows"][0]["period"] = "FY2025"
        override = {"NOTE.ppe_net": {"note_id": "Note 3", "row_id": "net", "reason": "Requested selection"}}
        result = resolve(value, overrides=override)
        self.assertEqual(result["matches"][0]["status"], "SCOPE_MISMATCH")
        self.assertEqual(result["facts"], [])

    def test_one_valid_and_one_wrong_period_requires_explicit_selection(self):
        value = data()
        old = row("prior")
        old["period"] = "FY2025"
        value["notes"][0]["rows"].append(old)
        self.assertEqual(resolve(value)["matches"][0]["status"], "SCOPE_MISMATCH")
        override = {"NOTE.ppe_net": {"note_id": "Note 3", "row_id": "net", "reason": "Select current-period column."}}
        self.assertEqual(resolve(value, overrides=override)["matches"][0]["status"], "MANUAL_MATCHED")

    def test_invalid_selectors_and_blank_override_reason_raise(self):
        for change in ({"note_id": "Unknown"}, {"row_id": "unknown"}, {"reason": " "}):
            selection = {"note_id": "Note 3", "row_id": "net", "reason": "Reviewed", **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                resolve(overrides={"NOTE.ppe_net": selection})
        with self.assertRaises(ValueError):
            resolve(overrides={"NOTE.unknown": {"note_id": "Note 3", "row_id": "net", "reason": "Reviewed"}})

    def test_duplicate_manual_fact_and_structured_target_is_rejected(self):
        value = data()
        value["facts"] = [{"key": "NOTE.ppe_net", "value": "285"}]
        with self.assertRaisesRegex(ValueError, "duplicates"):
            resolve(value)

    def test_duplicate_ids_and_invalid_amounts_are_rejected(self):
        value = data()
        value["notes"].append(copy.deepcopy(value["notes"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate note"):
            resolve(value)
        value = data()
        value["notes"][0]["rows"].append(copy.deepcopy(value["notes"][0]["rows"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate row"):
            resolve(value)
        for field, invalid in (("value", 285.0), ("value", True), ("value", "NaN"),
                               ("unit", "0"), ("unit", "-1"), ("source", ""), ("location", "")):
            value = data()
            value["notes"][0]["rows"][0][field] = invalid
            with self.subTest(field=field, value=invalid), self.assertRaises(ValueError):
                resolve(value)

    def test_existing_manual_fact_workflow_is_unchanged_without_notes(self):
        value = data()
        value.pop("notes")
        value["facts"] = [{"key": "NOTE.ppe_net", "value": "285"}]
        self.assertEqual(resolve(value), {"facts": [], "matches": [], "issues": {}})

    def test_no_input_mutation(self):
        value, config = data(), [matcher()]
        before = copy.deepcopy((value, config))
        resolve(value, config)
        self.assertEqual((value, config), before)


if __name__ == "__main__":
    unittest.main()
