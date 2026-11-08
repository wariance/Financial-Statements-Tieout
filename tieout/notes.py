"""Content-based note matching with explicit scope and preserved provenance.

Note identifiers are source references only. Automatic matches use a topic
phrase plus a conservative, normalized caption alias; uncertain matches are
never selected silently. Decimal source values must remain exact.
"""

from decimal import Decimal, InvalidOperation
import re
import unicodedata


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonblank string")
    return value.strip()


def _list(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{label} must be an exact decimal string or integer; floats are unsupported")
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise ValueError(f"{label} must be a finite decimal number") from None
    if not number.is_finite():
        raise ValueError(f"{label} must be finite")
    if len(number.as_tuple().digits) > 50 or abs(number.as_tuple().exponent) > 50:
        raise ValueError(f"{label} exceeds supported precision")
    return number


def _normalize(text):
    # NFKC covers fullwidth Latin/punctuation without transliterating semantics.
    text = unicodedata.normalize("NFKC", text).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^\w\s]", " ", text).replace("_", " ").split())


def _contains(text, phrase):
    text, phrase = _normalize(text), _normalize(phrase)
    if re.search(r"[\u3400-\u9fff]", phrase):
        return phrase in text
    return bool(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text))


def _aliases(raw, field, required=True):
    aliases = _list(raw.get(field, []), f"Matcher {raw.get('key')}.{field}")
    if required and not aliases:
        raise ValueError(f"Matcher {raw.get('key')}.{field} cannot be empty")
    result = [_text(alias, f"Matcher {raw.get('key')}.{field} alias") for alias in aliases]
    if any(not _normalize(alias) for alias in result):
        raise ValueError(f"Matcher {raw.get('key')}.{field} contains an empty normalized alias")
    return result


def _matchers(config):
    result = []
    keys = set()
    for raw in _list(config, "note matchers"):
        _object(raw, "note matcher")
        key = _text(raw.get("key"), "Matcher key")
        if not key.startswith("NOTE.") or key in keys:
            raise ValueError(f"Duplicate or invalid note matcher key: {key}")
        keys.add(key)
        result.append({
            "key": key, "fsli": _text(raw.get("fsli"), f"Matcher {key}.fsli"),
            "topics": _aliases(raw, "topics"), "labels": _aliases(raw, "labels"),
            "exclude_labels": _aliases(raw, "exclude_labels", required=False),
        })
    return result


def _rows(data):
    rows = []
    note_ids = set()
    for note in _list(data["notes"], "notes"):
        _object(note, "note")
        note_id = _text(note.get("id"), "Note id")
        if note_id in note_ids:
            raise ValueError(f"Duplicate note id: {note_id}")
        note_ids.add(note_id)
        title = _text(note.get("title"), f"Note {note_id}.title")
        body = note.get("text", "")
        if not isinstance(body, str):
            raise ValueError(f"Note {note_id}.text must be a string")
        row_ids = set()
        for row in _list(note.get("rows"), f"Note {note_id}.rows"):
            _object(row, f"Note {note_id} row")
            row_id = _text(row.get("id"), f"Note {note_id} row id")
            if row_id in row_ids:
                raise ValueError(f"Duplicate row id {row_id} in note {note_id}")
            row_ids.add(row_id)
            label = _text(row.get("label"), f"Note {note_id}/{row_id}.label")
            if "value" not in row:
                raise ValueError(f"Note {note_id}/{row_id} requires value; use null for missing")
            value = row["value"]
            if value is not None:
                _number(value, f"Note {note_id}/{row_id}.value")
                _text(row.get("source"), f"Note {note_id}/{row_id}.source")
                _text(row.get("location"), f"Note {note_id}/{row_id}.location")
            unit = _number(row.get("unit"), f"Note {note_id}/{row_id}.unit")
            if unit <= 0:
                raise ValueError(f"Note {note_id}/{row_id}.unit must be positive")
            candidate = {
                "note_id": note_id, "title": title, "row_id": row_id,
                "label": label, "value": value, "unit": row["unit"],
                "source": row.get("source"), "location": row.get("location"),
                "period": row.get("period"), "currency": row.get("currency"),
                "basis": row.get("basis"),
            }
            if "entity" in row:
                candidate["entity"] = row["entity"]
            if "entity" in note:
                candidate["note_entity"] = note["entity"]
            rows.append((candidate, title + "\n" + body))
    return rows


def _scope_issues(candidate, data):
    expected = {"period": data["period"], "currency": data["currency"], "basis": "consolidated"}
    issues = []
    for field, wanted in expected.items():
        actual = candidate.get(field)
        if not isinstance(actual, str) or not actual.strip():
            issues.append(f"{field} is missing")
        elif actual != wanted:
            issues.append(f"{field} {actual!r} does not match {wanted!r}")
    for field in ("entity", "note_entity"):
        if field in candidate and candidate[field] != data["entity"]:
            issues.append(f"{field} {candidate[field]!r} does not match {data['entity']!r}")
    return issues


def _derived(key, candidate, method):
    return {
        "key": key, "value": candidate["value"], "unit": candidate["unit"],
        "source": candidate["source"], "location": candidate["location"],
        "period": candidate["period"], "currency": candidate["currency"],
        "basis": candidate["basis"], "note_id": candidate["note_id"],
        "note_title": candidate["title"], "note_row_id": candidate["row_id"],
        "match_method": method,
    }


def resolve_notes(data, matchers, overrides=None):
    """Return derived facts, reviewable match decisions and unresolved reasons.

    A semantic candidate with incomplete or conflicting scope blocks automatic
    selection. An explicit reviewed override may select any row with correct
    scope, even outside the configured aliases. It cannot bypass scope checks.
    """
    _object(data, "Input")
    overrides = {} if overrides is None else _object(overrides, "note_overrides")
    if "notes" not in data:
        if overrides:
            raise ValueError("note_overrides requires structured notes input")
        return {"facts": [], "matches": [], "issues": {}}
    for field in ("entity", "period", "currency"):
        _text(data.get(field), field)
    config = _matchers(matchers)
    matcher_keys = {m["key"] for m in config}
    for fact in _list(data.get("facts", []), "facts"):
        _object(fact, "fact")
        if isinstance(fact.get("key"), str) and fact["key"].strip() in matcher_keys:
            raise ValueError(f"Manual fact {fact['key']} duplicates a structured note target")
    rows = _rows(data)
    by_id = {(c["note_id"], c["row_id"]): c for c, _ in rows}
    reviewed = {}
    for key, override in overrides.items():
        if key not in matcher_keys:
            raise ValueError(f"Unknown note override target: {key}")
        _object(override, f"Note override {key}")
        ids = (_text(override.get("note_id"), f"Override {key}.note_id"),
               _text(override.get("row_id"), f"Override {key}.row_id"))
        reason = _text(override.get("reason"), f"Override {key}.reason")
        if ids not in by_id:
            raise ValueError(f"Note override {key} references unknown note/row: {ids}")
        reviewed[key] = (by_id[ids], reason)

    facts, decisions, issues = [], [], {}
    for matcher in config:
        key = matcher["key"]
        labels = {_normalize(label) for label in matcher["labels"]}
        candidates = [candidate for candidate, topic_text in rows
                      if any(_contains(topic_text, topic) for topic in matcher["topics"])
                      and _normalize(candidate["label"]) in labels
                      and not any(_contains(candidate["label"], excluded)
                                  for excluded in matcher["exclude_labels"])]
        selected = None
        if key in reviewed:
            chosen, rationale = reviewed[key]
            if chosen not in candidates:
                candidates.append(chosen)
            scope = _scope_issues(chosen, data)
            if scope:
                status = "SCOPE_MISMATCH"
                reason = "Override cannot bypass scope: " + "; ".join(scope)
            else:
                selected = chosen
                status = "MANUAL_MATCHED"
                reason = "Reviewer selected this source row: " + rationale
        elif not candidates:
            status = "NOT_FOUND"
            reason = "No row matches both a note topic and an eligible caption alias."
        else:
            scope = [f"{c['note_id']}/{c['row_id']}: {', '.join(_scope_issues(c, data))}"
                     for c in candidates if _scope_issues(c, data)]
            if scope:
                status = "SCOPE_MISMATCH"
                reason = "Candidate scope must be reviewed: " + "; ".join(scope)
            elif len(candidates) != 1:
                status = "AMBIGUOUS"
                reason = "Multiple eligible source rows; select one explicitly after review."
            else:
                selected = candidates[0]
                status = "AUTO_MATCHED"
                reason = "Unique topic and caption match with the same period, currency and basis."
        if selected is not None:
            facts.append(_derived(key, selected, status))
        else:
            issues[key] = reason
        decisions.append({"key": key, "fsli": matcher["fsli"], "status": status,
                          "reason": reason, "candidates": candidates, "selected": selected})
    # A generic caption in a combined-topic note must not supply different
    # concepts merely because each matcher sees the same single candidate.
    uses = {}
    for decision in decisions:
        if decision["selected"] is not None:
            selected = decision["selected"]
            uses.setdefault((selected["note_id"], selected["row_id"]), []).append(decision)
    collisions = set()
    for related in uses.values():
        if len(related) > 1:
            keys = ", ".join(d["key"] for d in related)
            for decision in related:
                if decision["status"] == "AUTO_MATCHED":
                    key = decision["key"]
                    decision.update(status="AMBIGUOUS", selected=None,
                                    reason="The same source row matches multiple semantic targets: " + keys)
                    issues[key] = decision["reason"]
                    collisions.add(key)
    if collisions:
        facts = [fact for fact in facts if fact["key"] not in collisions]
    return {"facts": facts, "matches": decisions, "issues": issues}
