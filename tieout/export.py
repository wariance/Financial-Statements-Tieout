"""Literal-value audit exports. JSON is the complete, lossless audit record."""

import csv
import io
import json
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


COLUMNS = [
    "rule_id", "name", "category", "status", "lhs", "rhs", "difference",
    "absolute_difference", "tolerance", "tolerance_source", "formula", "reason",
    "review_status", "evidence",
]


def export_json(result):
    return (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _text(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list, bool)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return str(value)


def _csv_text(value):
    text = _text(value)
    # CSV has no cell type. Protect text even when prefixed with whitespace.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def export_csv(result, exceptions_only=True):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["run_id", "created_at", "entity", "period", "currency"] + COLUMNS)
    for row in result["results"]:
        if exceptions_only and row["status"] == "EXACT_MATCH":
            continue
        writer.writerow([_csv_text(result.get(k)) for k in ("run_id", "created_at", "entity", "period", "currency")]
                        + [_csv_text(row.get(k)) for k in COLUMNS])
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def _column(index):
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _xml_text(value):
    # XML 1.0 excludes control characters; document record remains in JSON.
    return escape(re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "\ufffd", _text(value)))


def _sheet(rows):
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
             '<cols><col min="1" max="32" width="24" customWidth="1"/></cols><sheetData>']
    for number, row in enumerate(rows, 1):
        parts.append('<row r="%s">' % number)
        for col, value in enumerate(row, 1):
            text = _text(value)
            if len(text) > 32767:
                text = text[:32650] + " [truncated for Excel; full content in JSON audit bundle]"
            parts.append('<c r="%s%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (_column(col), number, _xml_text(text)))
        parts.append('</row>')
    parts.append('</sheetData></worksheet>')
    return ''.join(parts)


def _table(items, columns):
    return [columns] + [[item.get(key) for key in columns] for item in items]


def export_xlsx(result):
    """Create an OOXML workbook with all amounts as exact literal text.

    No formulas/macros/links are emitted. Very long cells are visibly truncated;
    the JSON audit bundle is the complete evidence record.
    """
    input_data = result.get("input_snapshot", {})
    exceptions = [r for r in result["results"] if r["status"] != "EXACT_MATCH"]
    metadata = [["Field", "Value"]] + [[key, result.get(key)] for key in
        ("schema_version", "engine_version", "run_id", "created_at", "entity", "period", "currency", "input_sha256", "rules_sha256", "settings_sha256", "note_matchers_sha256", "settings", "summary")]
    metadata += [["Export convention", "Amounts are literal decimal text. JSON audit bundle is the full record. Cells above Excel's limit are marked truncated."],
                 ["Assurance", "Traceable review record; not tamperproof storage or verified reviewer identity."]]
    evidence = []
    for row in result["results"]:
        for item in row.get("evidence", []):
            evidence.append({"rule_id": row["rule_id"], "evidence": item})
    note_rows = []
    for note in input_data.get("notes", []):
        for row in note.get("rows", []):
            note_rows.append(dict(row, note_id=note.get("id"), title=note.get("title"), text=note.get("text")))
    sheets = [
        ("Audit metadata", metadata),
        ("Exceptions", _table(exceptions, COLUMNS)),
        ("All checks", _table(result["results"], COLUMNS)),
        ("Bridge", _table(result.get("bridge", []), ["fsli", "tb_total", "presented_total", "accounts"])),
        ("Evidence", _table(evidence, ["rule_id", "evidence"])),
        ("Input facts", _table(input_data.get("facts", []), ["key", "value", "unit", "source", "location", "basis"])),
        ("TB input", _table(input_data.get("tb", []), ["account", "name", "balance", "source", "location"])),
        ("Mapping", _table(input_data.get("mapping", []), ["account", "fsli", "sign", "confirmed", "reviewer", "reason"])),
        ("Rules", _table(result.get("rules_snapshot", []), ["id", "name", "category", "lhs", "rhs", "condition", "description"])),
        ("Note matches", _table(result.get("note_matches", []), ["key", "fsli", "status", "reason", "selected", "candidates"])),
        ("Derived note facts", _table(result.get("resolved_note_facts", []), ["key", "value", "unit", "source", "location", "basis", "note_id", "note_title", "note_row_id", "match_method"])),
        ("Note inputs", _table(note_rows, ["note_id", "title", "text", "id", "label", "value", "unit", "source", "location", "period", "currency", "basis"])),
        ("Note matchers", _table(result.get("note_matchers_snapshot", []), ["key", "fsli", "topics", "labels", "exclude_labels"])),
    ]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        types = ['<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                 '<Default Extension="xml" ContentType="application/xml"/>',
                 '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
        rels = ['<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
        book = ['<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
        for index, (name, rows) in enumerate(sheets, 1):
            types.append('<Override PartName="/xl/worksheets/sheet%s.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % index)
            rels.append('<Relationship Id="rId%s" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%s.xml"/>' % (index, index))
            book.append('<sheet name="%s" sheetId="%s" r:id="rId%s"/>' % (escape(name), index, index))
            archive.writestr('xl/worksheets/sheet%s.xml' % index, _sheet(rows))
        archive.writestr('[Content_Types].xml', ''.join(types) + '</Types>')
        archive.writestr('xl/_rels/workbook.xml.rels', ''.join(rels) + '</Relationships>')
        archive.writestr('xl/workbook.xml', ''.join(book) + '</sheets></workbook>')
        archive.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    return output.getvalue()


def write_audit_run(result, audit_dir):
    """Write once; never overwrite a run. Caller must protect the audit directory."""
    run_id = result.get("run_id", "")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("Invalid audit run ID")
    target = Path(audit_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / (run_id + ".json")
    with path.open("xb") as handle:
        handle.write(export_json(result))
    return path
