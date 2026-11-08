import copy
import hashlib
import json
import unittest
from decimal import Decimal

from tieout.engine import run_review, validate_tolerance


def fact(key, value, unit="1", **kwargs):
    return {"key": key, "value": value, "unit": unit,
            "source": "financial-statements.xlsx", "location": "BS!B10",
            "basis": "consolidated", **kwargs}


def dataset():
    return {"schema_version": 1, "entity": "Synthetic Entity", "period": "FY2026",
            "currency": "USD", "facts": [fact("BS.cash", "210000"), fact("CF.cash", "210000")],
            "tb": [], "mapping": []}


def rule(**kwargs):
    return {"id": "FS-01", "name": "Cash tie-out", "category": "CROSS_STATEMENT",
            "description": "Compare two explicitly confirmed cash totals.",
            "lhs": [{"key": "BS.cash", "coefficient": "1"}],
            "rhs": [{"key": "CF.cash", "coefficient": "1"}], **kwargs}


def settings(**kwargs):
    return {"tolerance": "0", "reviewer": "Test Reviewer",
            "reason": "Synthetic test case", **kwargs}


def result(data=None, rules=None, config=None):
    return run_review(data or dataset(), [rule()] if rules is None else rules,
                      settings() if config is None else config)


def find(report, rid):
    return next(row for row in report["results"] if row["rule_id"] == rid)


def with_tb():
    data = dataset()
    data["facts"].append(fact("BS.equity", "210000"))
    data["tb"] = [
        {"account": "1001", "name": "Bank A", "balance": "120000", "source": "TB.xlsx", "location": "TB!B2"},
        {"account": "1002", "name": "Bank B", "balance": "90000", "source": "TB.xlsx", "location": "TB!B3"},
        {"account": "3000", "name": "Equity", "balance": "-210000", "source": "TB.xlsx", "location": "TB!B4"},
    ]
    data["mapping"] = [
        {"account": account, "fsli": fsli, "sign": sign, "confirmed": True,
         "reviewer": "Test Reviewer", "reason": "Confirmed against the TB chart of accounts"}
        for account, fsli, sign in (("1001", "BS.cash", 1), ("1002", "BS.cash", 1), ("3000", "BS.equity", -1))
    ]
    return data


class ToleranceTests(unittest.TestCase):
    def test_valid_boundaries(self):
        for value in (0, "0", "0.01", 50000, "50000.00"):
            self.assertEqual(validate_tolerance(value), Decimal(str(value)))

    def test_invalid_magnitudes_and_nonfinite(self):
        for value in (True, False, None, 0.1, "", "NaN", "Infinity", "-Infinity", "-0.01", "50000.01", [], "1e999999"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_tolerance(value)

    def test_inclusive_tolerance_and_both_directions(self):
        for difference, expected in (("0", "EXACT_MATCH"), ("50000", "WITHIN_TOLERANCE"),
                                     ("-50000", "WITHIN_TOLERANCE"), ("50000.01", "FAIL"),
                                     ("-50000.01", "FAIL")):
            data = dataset()
            data["facts"][0]["value"] = str(Decimal("210000") + Decimal(difference))
            row = find(result(data, config=settings(tolerance="50000")), "FS-01")
            self.assertEqual(row["status"], expected)
            self.assertEqual(Decimal(row["difference"]), Decimal(difference))
            self.assertEqual(row["review_status"], "NO_EXCEPTION" if difference == "0" else "OPEN")

    def test_explicit_zero_override_is_not_falsy(self):
        data = dataset()
        data["facts"][0]["value"] = "210001"
        row = find(result(data, config=settings(tolerance="50000", overrides={"FS-01": "0"})), "FS-01")
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(row["tolerance"], "0")
        self.assertEqual(row["tolerance_source"], "RULE_OVERRIDE")

    def test_decimal_arithmetic_does_not_create_binary_float_residue(self):
        data = dataset()
        data["facts"] = [fact("a", "0.1"), fact("b", "0.2"), fact("c", "0.3")]
        check = rule(lhs=[{"key": "a", "coefficient": "1"}, {"key": "b", "coefficient": "1"}],
                     rhs=[{"key": "c", "coefficient": "1"}])
        row = find(result(data, [check]), "FS-01")
        self.assertEqual(row["status"], "EXACT_MATCH")


class FactAndRuleTests(unittest.TestCase):
    def test_unit_normalization_precedes_tolerance(self):
        data = dataset()
        data["facts"][0].update(value="210", unit="1000")
        row = find(result(data), "FS-01")
        self.assertEqual(row["status"], "EXACT_MATCH")
        self.assertEqual(row["evidence"][0]["normalized_value"], "210000")
        data["facts"][0]["value"] = "211"
        row = find(result(data, config=settings(tolerance="1")), "FS-01")
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(row["difference"], "1000")

    def test_missing_and_explicit_null_are_not_zero(self):
        for kind in ("absent", "null"):
            data = dataset()
            data["facts"][0]["value"] = "0"
            if kind == "absent":
                data["facts"].pop()
            else:
                data["facts"][1]["value"] = None
            row = find(result(data), "FS-01")
            self.assertEqual(row["status"], "NOT_TESTED")
            self.assertIsNone(row["difference"])

    def test_basis_mismatch_requires_review(self):
        data = dataset()
        data["facts"][1]["basis"] = "parent-only"
        self.assertEqual(find(result(data), "FS-01")["status"], "REVIEW_REQUIRED")

    def test_applicability_is_explicit(self):
        check = rule(condition="same_cash_scope")
        for value, expected in ((None, "REVIEW_REQUIRED"), (False, "NOT_APPLICABLE"), (True, "EXACT_MATCH")):
            row = find(result(rules=[check], config=settings(conditions={"same_cash_scope": value})), "FS-01")
            self.assertEqual(row["status"], expected)
        row = find(result(rules=[check]), "FS-01")
        self.assertEqual(row["status"], "REVIEW_REQUIRED")

    def test_manual_not_applicable_preserves_reason(self):
        row = find(result(config=settings(not_applicable={"FS-01": "No comparable cash flow statement"})), "FS-01")
        self.assertEqual(row["status"], "NOT_APPLICABLE")
        self.assertEqual(row["reason"], "No comparable cash flow statement")
        self.assertEqual(row["review_status"], "OPEN")

    def test_rule_validation(self):
        for changes in ({"lhs": []}, {"id": "TB-01"},
                        {"rhs": [{"key": "CF.cash", "coefficient": "0"}]},
                        {"rhs": [{"key": "CF.cash", "coefficient": "NaN"}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                result(rules=[rule(**changes)])
        with self.assertRaises(ValueError):
            result(rules=[rule(), rule()])

    def test_fact_validation(self):
        for changes in ({"unit": "0"}, {"unit": "-1000"}, {"value": "NaN"},
                        {"value": True}, {"source": ""}, {"location": ""},
                        {"currency": "EUR"}, {"period": "FY2025"}, {"entity": "Different Entity"}):
            data = dataset()
            data["facts"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                result(data)
        data = dataset()
        data["facts"].append(copy.deepcopy(data["facts"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate fact"):
            result(data)

    def test_unknown_and_invalid_settings_rejected(self):
        for changes in ({"overrides": {"typo": "1"}}, {"not_applicable": {"typo": "reason"}},
                        {"not_applicable": {"FS-01": " "}}, {"reviewer": ""},
                        {"reason": ""}, {"conditions": {"typo": True}},
                        {"overrides": {"FS-01": "50001"}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                result(config=settings(**changes))
        with self.assertRaises(ValueError):
            result(rules=[rule(condition="scope")], config=settings(conditions={"scope": 1}))


class MappingTests(unittest.TestCase):
    def test_many_accounts_to_one_fsli_and_credit_sign(self):
        report = result(with_tb())
        for rid in ("TB-01", "TB-02", "TB-03", "TB-FSLI:BS.cash", "TB-FSLI:BS.equity"):
            self.assertEqual(find(report, rid)["status"], "EXACT_MATCH")
        cash = next(row for row in report["bridge"] if row["fsli"] == "BS.cash")
        self.assertEqual(cash["tb_total"], "210000")
        self.assertEqual(cash["accounts"], ["1001", "1002"])
        self.assertEqual(len(find(report, "TB-FSLI:BS.cash")["evidence"]), 3)

    def test_unmapped_balances_do_not_cancel_or_pass_under_tolerance(self):
        data = with_tb()
        data["tb"].extend([
            {"account": "u1", "name": "Unmapped debit", "balance": "100", "source": "TB.xlsx", "location": "TB!B5"},
            {"account": "u2", "name": "Unmapped credit", "balance": "-100", "source": "TB.xlsx", "location": "TB!B6"},
        ])
        report = result(data, config=settings(tolerance="50000"))
        self.assertEqual(find(report, "TB-01")["status"], "EXACT_MATCH")
        completeness = find(report, "TB-02")
        self.assertEqual(completeness["status"], "FAIL")
        self.assertEqual(completeness["lhs"], "200")
        self.assertEqual(completeness["tolerance"], "0")
        self.assertEqual(find(report, "TB-FSLI:BS.cash")["status"], "REVIEW_REQUIRED")

    def test_unmapped_zero_account_still_requires_review(self):
        data = with_tb()
        data["tb"].append({"account": "zero", "name": "Unused", "balance": "0", "source": "TB.xlsx", "location": "TB!B5"})
        report = result(data)
        self.assertEqual(find(report, "TB-02")["status"], "REVIEW_REQUIRED")
        self.assertEqual(find(report, "TB-FSLI:BS.cash")["status"], "REVIEW_REQUIRED")

    def test_unconfirmed_mapping_blocks_bridge_even_exact_amounts(self):
        data = with_tb()
        data["mapping"][0]["confirmed"] = False
        report = result(data, config=settings(tolerance="50000"))
        self.assertEqual(find(report, "TB-03")["status"], "REVIEW_REQUIRED")
        self.assertEqual(find(report, "TB-FSLI:BS.cash")["status"], "REVIEW_REQUIRED")
        self.assertEqual(find(report, "TB-FSLI:BS.cash")["difference"], "0")

    def test_na_cannot_bypass_mapping_gate(self):
        for rid in ("TB-02", "TB-03"):
            with self.subTest(rid=rid), self.assertRaisesRegex(ValueError, "Integrity check"):
                result(with_tb(), config=settings(not_applicable={rid: "Not reviewed"}))

    def test_integrity_tolerance_cannot_be_overridden(self):
        for rid in ("TB-02", "TB-03"):
            with self.subTest(rid=rid), self.assertRaisesRegex(ValueError, "fixed zero"):
                result(with_tb(), config=settings(overrides={rid: "50000"}))

    def test_conflicting_tb_metadata_is_rejected(self):
        for changes in ({"currency": "EUR"}, {"period": "FY2025"},
                        {"entity": "Different Entity"}, {"basis": "parent-only"}):
            data = with_tb()
            data["tb"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                result(data)

    def test_fs_only_records_unavailable_tb_checks(self):
        report = result()
        for rid in ("TB-01", "TB-02", "TB-03"):
            self.assertEqual(find(report, rid)["status"], "NOT_TESTED")
        self.assertEqual(find(report, "FS-01")["status"], "EXACT_MATCH")

    def test_missing_fsli_not_replaced_with_zero(self):
        data = with_tb()
        data["facts"][0]["value"] = None
        self.assertEqual(find(result(data), "TB-FSLI:BS.cash")["status"], "NOT_TESTED")

    def test_unknown_duplicate_split_and_inconsistent_sign_mappings_rejected(self):
        variants = []
        data = with_tb()
        data["mapping"][0]["fsli"] = "BS.unknown"
        variants.append(data)
        data = with_tb()
        data["mapping"][0]["account"] = "unknown"
        variants.append(data)
        data = with_tb()
        data["mapping"].append(copy.deepcopy(data["mapping"][0]))
        variants.append(data)
        data = with_tb()
        data["mapping"][0]["sign"] = -1
        variants.append(data)
        data = with_tb()
        data["mapping"][0]["sign"] = True
        variants.append(data)
        data = with_tb()
        data["mapping"][0]["reviewer"] = ""
        variants.append(data)
        data = with_tb()
        data["tb"].append(copy.deepcopy(data["tb"][0]))
        variants.append(data)
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValueError):
                result(data)


class AuditTests(unittest.TestCase):
    def test_lossy_json_float_cannot_become_a_false_exact_match(self):
        data = dataset()
        data["facts"] = json.loads('[{"key":"BS.cash","value":9007199254740993.0,"unit":"1","source":"FS.xlsx","location":"BS!B1"},'
                                   '{"key":"CF.cash","value":"9007199254740992","unit":"1","source":"FS.xlsx","location":"CF!B1"}]')
        with self.assertRaisesRegex(ValueError, "floating-point"):
            result(data)
        data["facts"][0]["value"] = "9007199254740993"
        row = find(result(data), "FS-01")
        self.assertEqual(row["difference"], "1")
        self.assertEqual(row["status"], "FAIL")

    def test_bool_schema_version_is_invalid(self):
        data = dataset()
        data["schema_version"] = True
        with self.assertRaises(ValueError):
            result(data)

    def test_fifty_digit_cancellation_preserves_small_difference(self):
        data = dataset()
        huge = "9" * 50
        data["facts"] = [fact("a", huge), fact("b", huge), fact("c", "0.01"), fact("d", "0")]
        check = rule(lhs=[{"key": "a", "coefficient": "1"}, {"key": "c", "coefficient": "1"},
                          {"key": "b", "coefficient": "-1"}],
                     rhs=[{"key": "d", "coefficient": "1"}])
        row = find(result(data, [check]), "FS-01")
        self.assertEqual(row["difference"], "0.01")
        self.assertEqual(row["status"], "FAIL")

    def test_complete_reproducible_snapshots_and_no_mutation(self):
        data, rules, config = with_tb(), [rule()], settings()
        before = copy.deepcopy((data, rules, config))
        first = run_review(data, rules, config)
        second = run_review(data, rules, config)
        self.assertEqual((data, rules, config), before)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["input_sha256"], second["input_sha256"])
        self.assertEqual(first["rules_sha256"], second["rules_sha256"])
        self.assertEqual(first["results"], second["results"])
        self.assertEqual(first["input_snapshot"], data)
        self.assertEqual(first["rules_snapshot"], rules)
        serialized = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(first["input_sha256"], hashlib.sha256(serialized.encode()).hexdigest())
        self.assertTrue(first["created_at"].endswith("Z"))
        json.dumps(first, allow_nan=False)
        first["input_snapshot"]["entity"] = "Changed snapshot"
        self.assertEqual(data["entity"], "Synthetic Entity")

    def test_hashes_change_with_evidence_rule_and_settings(self):
        data = dataset()
        initial = result(data)
        data["facts"][0]["location"] = "BS!C10"
        changed = result(data, [rule(name="Updated rule")], settings(tolerance="10"))
        self.assertNotEqual(initial["input_sha256"], changed["input_sha256"])
        self.assertNotEqual(initial["rules_sha256"], changed["rules_sha256"])
        self.assertNotEqual(initial["settings_sha256"], changed["settings_sha256"])

    def test_within_tolerance_is_counted_as_exception_and_coverage_keeps_missing(self):
        data = dataset()
        data["facts"][0]["value"] = "210001"
        report = result(data, config=settings(tolerance="1"))
        self.assertEqual(report["summary"]["exception_count"], 4)
        self.assertEqual(report["summary"]["tested"], 1)
        self.assertEqual(report["summary"]["total"], 4)
        self.assertEqual(report["summary"]["coverage_percent"], "25")


class DynamicNoteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.data = dataset()
        self.data["notes"] = [{
            "id": "Note 17", "title": "Cash and cash equivalents", "text": "Cash balances",
            "rows": [{"id": "cash-total", "label": "Cash and cash equivalents", "value": "210000",
                      "unit": "1", "source": "Financial Statements.pdf", "location": "page 18, table 1, row 4",
                      "period": "FY2026", "currency": "USD", "basis": "consolidated"}],
        }]
        self.matchers = [{"key": "NOTE.cash", "fsli": "BS.cash", "topics": ["cash"],
                          "labels": ["cash and cash equivalents"], "exclude_labels": ["restricted"]}]
        self.rules = [rule(id="N-CASH", name="Cash note comparison", category="NOTES",
                           rhs=[{"key": "NOTE.cash", "coefficient": "1"}])]

    def review(self, config=None):
        return run_review(self.data, self.rules, config or settings(), note_matchers=self.matchers)

    def test_dynamic_match_preserves_raw_input_and_note_evidence(self):
        before = copy.deepcopy(self.data)
        report = self.review()
        row = find(report, "N-CASH")
        self.assertEqual(row["status"], "EXACT_MATCH")
        self.assertEqual(report["note_matches"][0]["status"], "AUTO_MATCHED")
        self.assertEqual(report["input_snapshot"], before)
        self.assertEqual(self.data, before)
        self.assertFalse(any(fact["key"] == "NOTE.cash" for fact in report["input_snapshot"]["facts"]))
        self.assertEqual(report["resolved_note_facts"][0]["key"], "NOTE.cash")
        self.assertEqual(report["note_matchers_snapshot"], self.matchers)
        note_evidence = next(item for item in row["evidence"] if item["key"] == "NOTE.cash")
        self.assertEqual(note_evidence["note_id"], "Note 17")
        self.assertEqual(note_evidence["note_row_id"], "cash-total")
        self.assertEqual(note_evidence["location"], "page 18, table 1, row 4")

    def test_number_and_order_changes_do_not_change_semantic_comparison(self):
        original = self.review()
        irrelevant = {"id": "Note 9", "title": "Other matters", "rows": []}
        self.data["notes"].insert(0, irrelevant)
        self.data["notes"][1]["id"] = "Note 2"
        self.data["entity"] = "Different Company"
        changed = self.review()
        self.assertEqual(find(changed, "N-CASH")["status"], find(original, "N-CASH")["status"])
        self.assertEqual(find(changed, "N-CASH")["difference"], "0")
        self.assertEqual(changed["note_matches"][0]["selected"]["note_id"], "Note 2")

    def test_ambiguous_note_requires_review_without_a_silent_selection(self):
        duplicate = copy.deepcopy(self.data["notes"][0])
        duplicate["id"] = "Note 22"
        self.data["notes"].append(duplicate)
        report = self.review()
        self.assertEqual(report["note_matches"][0]["status"], "AMBIGUOUS")
        self.assertEqual(find(report, "N-CASH")["status"], "REVIEW_REQUIRED")
        self.assertEqual(report["resolved_note_facts"], [])
        self.assertIsNone(find(report, "N-CASH")["difference"])
        issue = next(item for item in find(report, "N-CASH")["evidence"] if item["key"] == "NOTE.cash")
        self.assertEqual(len(issue["candidates"]), 2)

    def test_manual_note_override_records_decision_and_settings(self):
        duplicate = copy.deepcopy(self.data["notes"][0])
        duplicate["id"] = "Note 22"
        duplicate["rows"][0]["value"] = "209999"
        self.data["notes"].append(duplicate)
        config = settings(note_overrides={"NOTE.cash": {"note_id": "Note 22", "row_id": "cash-total",
                                                       "reason": "Reviewed source table for this consolidated balance"}})
        report = self.review(config)
        self.assertEqual(report["note_matches"][0]["status"], "MANUAL_MATCHED")
        self.assertEqual(find(report, "N-CASH")["difference"], "1")
        self.assertEqual(find(report, "N-CASH")["status"], "FAIL")
        self.assertEqual(report["settings"]["note_overrides"], config["note_overrides"])

    def test_scope_mismatch_or_missing_scope_requires_review(self):
        for field, value in (("currency", "EUR"), ("period", "FY2025"), ("basis", "parent-only"), ("period", None)):
            with self.subTest(field=field, value=value):
                previous = copy.deepcopy(self.data)
                if value is None:
                    self.data["notes"][0]["rows"][0].pop(field)
                else:
                    self.data["notes"][0]["rows"][0][field] = value
                report = self.review()
                self.assertEqual(report["note_matches"][0]["status"], "SCOPE_MISMATCH")
                self.assertEqual(find(report, "N-CASH")["status"], "REVIEW_REQUIRED")
                self.data = previous

    def test_no_matching_note_is_not_tested(self):
        self.data["notes"][0]["title"] = "Other matters"
        self.data["notes"][0]["text"] = "No applicable information"
        self.data["notes"][0]["rows"][0]["label"] = "Other amount"
        report = self.review()
        self.assertEqual(report["note_matches"][0]["status"], "NOT_FOUND")
        self.assertEqual(find(report, "N-CASH")["status"], "NOT_TESTED")

    def test_note_missing_amount_is_not_zero(self):
        self.data["notes"][0]["rows"][0]["value"] = None
        report = self.review()
        self.assertEqual(find(report, "N-CASH")["status"], "NOT_TESTED")
        self.assertIsNone(find(report, "N-CASH")["difference"])

    def test_manual_na_still_records_reason(self):
        self.data["notes"] = []
        report = self.review(settings(not_applicable={"N-CASH": "This note is not required for this entity"}))
        self.assertEqual(find(report, "N-CASH")["status"], "NOT_APPLICABLE")
        self.assertIn("not required", find(report, "N-CASH")["reason"])

    def test_matching_config_changes_are_hashed(self):
        initial = self.review()
        self.matchers[0]["topics"].append("cash balances")
        changed = self.review()
        self.assertNotEqual(initial["note_matchers_sha256"], changed["note_matchers_sha256"])
        self.assertEqual(initial["input_sha256"], changed["input_sha256"])

    def test_existing_manual_semantic_fact_cannot_be_silently_overwritten(self):
        self.data["facts"].append(fact("NOTE.cash", "210000"))
        with self.assertRaises(ValueError):
            self.review()

    def test_note_override_requires_notes(self):
        with self.assertRaisesRegex(ValueError, "requires a notes"):
            result(config=settings(note_overrides={"NOTE.cash": {"note_id": "Note 17", "row_id": "cash-total", "reason": "Confirmed"}}))


if __name__ == "__main__":
    unittest.main()
