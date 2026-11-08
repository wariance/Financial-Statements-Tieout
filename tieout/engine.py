"""Decimal-only tie-out checks with source evidence and reproducible snapshots.

The engine compares explicitly supplied facts; it does not infer missing facts,
decide accounting classifications, or equate a tolerance with materiality.
"""

import copy
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from uuid import uuid4


STATUSES = (
    "EXACT_MATCH", "WITHIN_TOLERANCE", "FAIL", "REVIEW_REQUIRED",
    "NOT_APPLICABLE", "NOT_TESTED",
)
TESTED_STATUSES = {"EXACT_MATCH", "WITHIN_TOLERANCE", "FAIL"}
ZERO = Decimal("0")
MAX_TOLERANCE = Decimal("50000")


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _list(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonblank string")
    return value.strip()


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{label} must be an exact decimal string or integer; floating-point numbers are unsupported")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be a finite decimal number") from None
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    # Bound input complexity so hostile exponents cannot create huge exports.
    if len(result.as_tuple().digits) > 50 or abs(result.as_tuple().exponent) > 50:
        raise ValueError(f"{label} exceeds supported precision (50 digits/exponent)")
    return result


def _amount(value):
    if value is None:
        return None
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def validate_tolerance(value):
    """Return a Decimal magnitude in [0, 50000], or raise ValueError."""
    result = _number(value, "Tolerance")
    if not ZERO <= result <= MAX_TOLERANCE:
        raise ValueError("Tolerance must be between 0 and 50000 inclusive")
    return result


def _snapshot(value, label):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must contain JSON-serializable finite values") from None
    return json.loads(encoded), hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _check_context(raw, data, label):
    for dimension in ("entity", "currency", "period"):
        if dimension in raw and raw[dimension] != data[dimension]:
            raise ValueError(f"{label} {dimension} differs from engagement {dimension}; use explicit PY keys for prior-period comparisons")


def _validate_data(data):
    _object(data, "Input")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Input schema_version must be 1")
    for key in ("entity", "period", "currency"):
        _text(data.get(key), key)
    facts = {}
    for index, raw in enumerate(_list(data.get("facts"), "facts")):
        _object(raw, f"facts[{index}]")
        key = _text(raw.get("key"), f"facts[{index}].key")
        if key in facts:
            raise ValueError(f"Duplicate fact key: {key}")
        if "value" not in raw:
            raise ValueError(f"Fact {key} requires value (use null for missing)")
        unit = _number(raw.get("unit"), f"{key}.unit")
        if unit <= ZERO:
            raise ValueError(f"{key}.unit must be positive")
        basis = _text(raw.get("basis", "consolidated"), f"{key}.basis")
        value = None if raw["value"] is None else _number(raw["value"], f"{key}.value")
        if value is not None:
            _text(raw.get("source"), f"{key}.source")
            _text(raw.get("location"), f"{key}.location")
        _check_context(raw, data, f"Fact {key}")
        facts[key] = {**raw, "key": key, "basis": basis,
                      "value_decimal": value, "unit_decimal": unit,
                      "normalized": None if value is None else value * unit}
    tb = {}
    for index, raw in enumerate(_list(data.get("tb", []), "tb")):
        _object(raw, f"tb[{index}]")
        account = _text(raw.get("account"), f"tb[{index}].account")
        if account in tb:
            raise ValueError(f"Duplicate TB account: {account}")
        for field in ("name", "source", "location"):
            _text(raw.get(field), f"TB {account}.{field}")
        _check_context(raw, data, f"TB {account}")
        if raw.get("basis", "consolidated") != "consolidated":
            raise ValueError(f"TB {account}.basis must be consolidated in this version")
        balance = _number(raw.get("balance"), f"TB {account}.balance")
        tb[account] = {**raw, "account": account, "balance_decimal": balance}
    mapping = {}
    signs = {}
    for index, raw in enumerate(_list(data.get("mapping", []), "mapping")):
        _object(raw, f"mapping[{index}]")
        account = _text(raw.get("account"), f"mapping[{index}].account")
        if account in mapping:
            raise ValueError(f"Duplicate/split mapping for account {account} is unsupported")
        if account not in tb:
            raise ValueError(f"Mapping references unknown TB account: {account}")
        fsli = _text(raw.get("fsli"), f"Mapping {account}.fsli")
        if fsli not in facts:
            raise ValueError(f"Mapping references unknown FSLI fact: {fsli}")
        sign = raw.get("sign")
        if type(sign) is not int or sign not in (-1, 1):
            raise ValueError(f"Mapping {account}.sign must be integer 1 or -1")
        if fsli in signs and signs[fsli] != sign:
            raise ValueError(f"Mappings for {fsli} must use the same presentation sign")
        signs[fsli] = sign
        if type(raw.get("confirmed")) is not bool:
            raise ValueError(f"Mapping {account}.confirmed must be true or false")
        if raw["confirmed"]:
            _text(raw.get("reviewer"), f"Mapping {account}.reviewer")
            _text(raw.get("reason"), f"Mapping {account}.reason")
        mapping[account] = {**raw, "account": account, "fsli": fsli}
    return facts, tb, mapping


def _validate_rules(rules):
    ids = set()
    conditions = set()
    validated = []
    for index, rule in enumerate(_list(rules, "rules")):
        _object(rule, f"rules[{index}]")
        rid = _text(rule.get("id"), f"rules[{index}].id")
        if rid in ids or rid in ("TB-01", "TB-02", "TB-03") or rid.startswith("TB-FSLI:"):
            raise ValueError(f"Duplicate or reserved rule id: {rid}")
        ids.add(rid)
        for field in ("name", "category", "description"):
            _text(rule.get(field), f"Rule {rid}.{field}")
        parsed = {**rule, "id": rid}
        for side in ("lhs", "rhs"):
            terms = _list(rule.get(side), f"Rule {rid}.{side}")
            if not terms:
                raise ValueError(f"Rule {rid}.{side} cannot be empty")
            parsed[side] = []
            for term in terms:
                _object(term, f"Rule {rid}.{side} term")
                key = _text(term.get("key"), f"Rule {rid} term key")
                coef = _number(term.get("coefficient"), f"Rule {rid} coefficient")
                if coef == 0:
                    raise ValueError(f"Rule {rid} coefficient cannot be zero")
                parsed[side].append({"key": key, "coefficient": coef})
        if "condition" in rule:
            conditions.add(_text(rule["condition"], f"Rule {rid}.condition"))
        validated.append(parsed)
    return validated, ids, conditions


def _validate_settings(settings, ids, condition_ids):
    _object(settings, "settings")
    global_tolerance = validate_tolerance(settings.get("tolerance", "0"))
    reviewer = _text(settings.get("reviewer"), "settings.reviewer")
    reason = _text(settings.get("reason"), "settings.reason")
    overrides = _object(settings.get("overrides", {}), "settings.overrides")
    not_applicable = _object(settings.get("not_applicable", {}), "settings.not_applicable")
    for rid in set(overrides) | set(not_applicable):
        if rid not in ids:
            raise ValueError(f"Unknown rule id in settings: {rid}")
    parsed_overrides = {rid: validate_tolerance(value) for rid, value in overrides.items()}
    for rid in ("TB-02", "TB-03"):
        if rid in not_applicable:
            raise ValueError(f"Integrity check {rid} cannot be marked not applicable")
        if parsed_overrides.get(rid, ZERO) != ZERO:
            raise ValueError(f"Integrity check {rid} has fixed zero tolerance")
    for rid, explanation in not_applicable.items():
        _text(explanation, f"N/A reason for {rid}")
    conditions = _object(settings.get("conditions", {}), "settings.conditions")
    note_overrides = _object(settings.get("note_overrides", {}), "settings.note_overrides")
    for condition, value in conditions.items():
        if condition not in condition_ids:
            raise ValueError(f"Unknown rule condition: {condition}")
        if value is not None and type(value) is not bool:
            raise ValueError(f"Condition {condition} must be true, false, or null")
    snapshot = {
        "tolerance": _amount(global_tolerance),
        "overrides": {key: _amount(value) for key, value in parsed_overrides.items()},
        "reviewer": reviewer, "reason": reason,
        "conditions": copy.deepcopy(conditions),
        "not_applicable": copy.deepcopy(not_applicable),
        "note_overrides": copy.deepcopy(note_overrides),
    }
    return global_tolerance, parsed_overrides, snapshot


def _evidence(fact):
    evidence = {
        "key": fact["key"], "value": _amount(fact["value_decimal"]),
        "unit": _amount(fact["unit_decimal"]), "normalized_value": _amount(fact["normalized"]),
        "source": fact.get("source"), "location": fact.get("location"), "basis": fact["basis"],
    }
    for key in ("note_id", "note_title", "note_row_id", "match_method", "period", "currency"):
        if key in fact:
            evidence[key] = copy.deepcopy(fact[key])
    return evidence


def _tb_evidence(row, mapping=None):
    result = {key: row[key] for key in ("account", "name", "source", "location")}
    result["balance"] = _amount(row["balance_decimal"])
    if mapping is not None:
        result["mapping"] = copy.deepcopy(mapping)
    return result


def _formula(terms):
    return " + ".join(f"({_amount(term['coefficient'])} × {term['key']})" for term in terms)


def _row(rid, name, category, tolerance, tolerance_source, formula,
         lhs=None, rhs=None, status=None, reason="", evidence=None):
    difference = None if lhs is None or rhs is None else lhs - rhs
    if status is None:
        if difference == 0:
            status = "EXACT_MATCH"
            reason = reason or "Both sides are equal in base currency units."
        elif abs(difference) <= tolerance:
            status = "WITHIN_TOLERANCE"
            reason = reason or "Nonzero difference is within the configured tolerance; review remains open."
        else:
            status = "FAIL"
            reason = reason or "Absolute difference exceeds the configured tolerance."
    return {
        "rule_id": rid, "name": name, "category": category, "status": status,
        "lhs": _amount(lhs), "rhs": _amount(rhs), "difference": _amount(difference),
        "absolute_difference": _amount(abs(difference)) if difference is not None else None,
        "tolerance": _amount(tolerance), "tolerance_source": tolerance_source,
        "formula": formula, "reason": reason, "evidence": evidence or [],
        "review_status": "NO_EXCEPTION" if status == "EXACT_MATCH" else "OPEN",
    }


def run_review(data, rules, settings, *, note_matchers=None):
    """Validate and run deterministic checks. Inputs are never mutated.

    Decimal inputs should be serialized as strings. Results include complete
    input/rule/settings snapshots and hashes. Coverage is tested rows divided
    by ALL rows, including explicit not-applicable and not-tested rows.
    """
    with localcontext() as ctx:
        ctx.prec = 512
        return _run_review(data, rules, settings, note_matchers=note_matchers)


def _run_review(data, rules, settings, *, note_matchers=None):
    _object(data, "Input")
    _object(settings, "settings")
    input_snapshot, input_hash = _snapshot(data, "Input")
    note_overrides = _object(settings.get("note_overrides", {}), "settings.note_overrides")
    note_resolution = {"facts": [], "matches": [], "issues": {}}
    active_matchers = []
    calculation_data = data
    if "notes" in data:
        from .notes import resolve_notes

        if note_matchers is None:
            matcher_path = Path(__file__).resolve().parent.parent / "rules" / "note_matchers.json"
            try:
                active_matchers = json.loads(matcher_path.read_text(encoding="utf-8"), parse_float=str)
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Cannot load default note matcher configuration: {exc}") from exc
        else:
            active_matchers = note_matchers
        note_resolution = resolve_notes(data, active_matchers, note_overrides)
        calculation_data = copy.deepcopy(data)
        calculation_data["facts"] = _list(calculation_data.get("facts"), "facts") + note_resolution["facts"]
    elif note_overrides:
        raise ValueError("settings.note_overrides requires a notes input list")
    matcher_snapshot, matcher_hash = _snapshot(active_matchers, "Note matchers")
    facts, tb, mapping = _validate_data(calculation_data)
    validated_rules, rule_ids, condition_ids = _validate_rules(rules)
    groups = defaultdict(list)
    for account, entry in mapping.items():
        groups[entry["fsli"]].append(account)
    generated_ids = {"TB-01", "TB-02", "TB-03"} | {"TB-FSLI:" + key for key in groups}
    global_tolerance, overrides, config = _validate_settings(
        settings, rule_ids | generated_ids, condition_ids)
    rules_snapshot, rules_hash = _snapshot(rules, "Rules")
    _, settings_hash = _snapshot(config, "Settings")
    rows = []
    note_decisions = {record["key"]: record for record in note_resolution["matches"]}

    def tolerance(rid, integrity=False):
        if integrity:
            return ZERO, "INTEGRITY_CHECK_FIXED_ZERO"
        return (overrides[rid], "RULE_OVERRIDE") if rid in overrides else (global_tolerance, "GLOBAL")

    def add(rid, name, category, formula, integrity=False, **kwargs):
        tol, origin = tolerance(rid, integrity)
        if rid in config["not_applicable"]:
            kwargs["status"] = "NOT_APPLICABLE"
            kwargs["reason"] = config["not_applicable"][rid]
        rows.append(_row(rid, name, category, tol, origin, formula, **kwargs))

    for rule in validated_rules:
        rid = rule["id"]
        keys = list(dict.fromkeys(term["key"] for side in ("lhs", "rhs") for term in rule[side]))
        evidence = [_evidence(facts[key]) for key in keys if key in facts]
        issue_keys = [key for key in keys if key in note_resolution["issues"]]
        for key in issue_keys:
            decision = note_decisions[key]
            evidence.append({"key": key, "note_match_status": decision["status"],
                             "reason": decision["reason"],
                             "candidates": copy.deepcopy(decision["candidates"]),
                             "selected": None})
        formula = _formula(rule["lhs"]) + " = " + _formula(rule["rhs"])
        kwargs = {"evidence": evidence}
        condition = rule.get("condition")
        missing = [key for key in keys if key not in facts or facts[key]["normalized"] is None]
        bases = {facts[key]["basis"] for key in keys if key in facts}
        if condition and config["conditions"].get(condition) is False:
            kwargs.update(status="NOT_APPLICABLE", reason=f"Reviewer marked condition {condition} false; see settings rationale: {config['reason']}")
        elif issue_keys:
            issue_status = ("REVIEW_REQUIRED" if any(note_decisions[key]["status"] != "NOT_FOUND" for key in issue_keys)
                            else "NOT_TESTED")
            kwargs.update(status=issue_status, reason="Note matching: " + "; ".join(
                f"{key}: {note_resolution['issues'][key]}" for key in issue_keys))
        elif condition and config["conditions"].get(condition) is None:
            kwargs.update(status="REVIEW_REQUIRED", reason=f"Confirm applicability/basis condition: {condition}")
        elif missing:
            kwargs.update(status="NOT_TESTED", reason="Missing facts: " + ", ".join(missing))
        elif len(bases) != 1:
            kwargs.update(status="REVIEW_REQUIRED", reason="Incompatible reporting bases: " + ", ".join(sorted(bases)))
        else:
            for side in ("lhs", "rhs"):
                kwargs[side] = sum((facts[term["key"]]["normalized"] * term["coefficient"]
                                    for term in rule[side]), ZERO)
        add(rid, rule["name"], rule["category"], formula, **kwargs)

    tb_evidence = [_tb_evidence(row, mapping.get(account)) for account, row in tb.items()]
    if not tb:
        for rid, name, formula in (
            ("TB-01", "Trial balance balance", "sum(signed TB balances) = 0"),
            ("TB-02", "Mapping completeness", "sum(abs(unmapped balances)) = 0; every account mapped"),
            ("TB-03", "Mapping confirmation", "unconfirmed mapping count = 0"),
        ):
            add(rid, name, "TB", formula, integrity=rid != "TB-01",
                status="NOT_TESTED", reason="No trial balance was supplied.")
    else:
        add("TB-01", "Trial balance balance", "TB", "sum(signed TB balances) = 0",
            lhs=sum((row["balance_decimal"] for row in tb.values()), ZERO), rhs=ZERO,
            evidence=tb_evidence)
        unmapped = [account for account in tb if account not in mapping]
        unconfirmed = [account for account, entry in mapping.items() if not entry["confirmed"]]
        gross = sum((abs(tb[account]["balance_decimal"]) for account in unmapped), ZERO)
        add("TB-02", "Mapping completeness", "TB",
            "sum(abs(unmapped balances)) = 0; every account mapped", integrity=True,
            lhs=gross, rhs=ZERO,
            status=("FAIL" if gross else "REVIEW_REQUIRED") if unmapped else "EXACT_MATCH",
            reason=("Unmapped accounts (gross absolute balances, no netting): " + ", ".join(unmapped))
                   if unmapped else "Every supplied TB account has one mapping.",
            evidence=[_tb_evidence(tb[account]) for account in unmapped])
        add("TB-03", "Mapping confirmation", "TB", "unconfirmed mapping count = 0",
            integrity=True, lhs=Decimal(len(unconfirmed)), rhs=ZERO,
            status="REVIEW_REQUIRED" if unconfirmed or unmapped else "EXACT_MATCH",
            reason=("All accounts must be mapped and confirmed. Unconfirmed: " + (", ".join(unconfirmed) or "none")
                    + "; unmapped: " + (", ".join(unmapped) or "none"))
                   if unconfirmed or unmapped else "Every mapping is confirmed with reviewer and rationale.",
            evidence=[_tb_evidence(tb[account], mapping[account]) for account in unconfirmed])

    bridge = []
    incomplete = bool(set(tb) - set(mapping))
    unconfirmed = any(not entry["confirmed"] for entry in mapping.values())
    for fsli in sorted(groups):
        accounts = sorted(groups[fsli])
        total = sum((tb[account]["balance_decimal"] * mapping[account]["sign"] for account in accounts), ZERO)
        fact = facts[fsli]
        evidence = [_tb_evidence(tb[account], mapping[account]) for account in accounts] + [_evidence(fact)]
        kwargs = {"lhs": total, "rhs": fact["normalized"], "evidence": evidence}
        if incomplete or unconfirmed:
            kwargs.update(status="REVIEW_REQUIRED", reason="Bridge comparison is blocked until every TB account is mapped and every mapping is confirmed.")
        elif fact["normalized"] is None:
            kwargs.update(status="NOT_TESTED", reason=f"Reported FSLI value is missing: {fsli}")
        elif fact["basis"] != "consolidated":
            kwargs.update(status="REVIEW_REQUIRED", reason="TB is on the engagement consolidated basis; confirm the FSLI reporting basis.")
        add("TB-FSLI:" + fsli, "TB to " + fsli, "TB_TO_FSLI",
            " + ".join(f"({mapping[account]['sign']} × TB[{account}])" for account in accounts) + " = " + fsli,
            **kwargs)
        bridge.append({"fsli": fsli, "tb_total": _amount(total),
                       "presented_total": _amount(fact["normalized"]), "accounts": accounts,
                       "mapping_complete": not incomplete, "mapping_confirmed": not unconfirmed,
                       "evidence": evidence})

    counts = Counter(row["status"] for row in rows)
    tested = sum(counts[status] for status in TESTED_STATUSES)
    summary = {"counts": {status: counts[status] for status in STATUSES},
               "total": len(rows), "exception_count": len(rows) - counts["EXACT_MATCH"],
               "tested": tested,
               "coverage_percent": _amount((Decimal(tested) * 100 / len(rows)).quantize(Decimal("0.01"))) if rows else "0"}
    return {"schema_version": 1, "engine_version": "0.1.0", "run_id": str(uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "entity": data["entity"], "period": data["period"], "currency": data["currency"],
            "settings": config, "input_sha256": input_hash, "rules_sha256": rules_hash,
            "settings_sha256": settings_hash, "results": rows, "bridge": bridge,
            "summary": summary, "input_snapshot": input_snapshot, "rules_snapshot": rules_snapshot,
            "resolved_note_facts": copy.deepcopy(note_resolution["facts"]),
            "note_matches": copy.deepcopy(note_resolution["matches"]),
            "note_matchers_snapshot": matcher_snapshot, "note_matchers_sha256": matcher_hash}
