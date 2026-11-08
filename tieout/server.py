"""Loopback-only prototype web interface; no remote services or uploads."""

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .engine import run_review
from .export import export_csv, export_json, export_xlsx, write_audit_run


MAX_BODY_BYTES = 5 * 1024 * 1024


def _load_json(raw):
    def no_constant(value):
        raise ValueError("Non-finite JSON number: " + value)
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("Duplicate JSON key: " + key)
            obj[key] = value
        return obj
    return json.loads(raw, parse_constant=no_constant, object_pairs_hook=unique)


def _validate_browser_amounts(data):
    """Require strings before a browser can round JSON numeric amounts."""
    if not isinstance(data, dict):
        raise ValueError("Input must be a JSON object")
    for group, keys in (("facts", ("value", "unit")), ("tb", ("balance",))):
        rows = data.get(group, [])
        if not isinstance(rows, list):
            raise ValueError(group + " must be an array")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(group + " entries must be objects")
            for key in keys:
                value = row.get(key)
                if key == "value" and value is None:
                    continue
                if not isinstance(value, str):
                    raise ValueError("%s[%s].%s must be a decimal string, e.g. \"1234.56\"; numeric JSON amounts can lose browser precision" % (group, index, key))
    if "notes" in data:
        if not isinstance(data["notes"], list):
            raise ValueError("notes must be an array")
        for note_index, note in enumerate(data["notes"]):
            if not isinstance(note, dict) or not isinstance(note.get("rows"), list):
                raise ValueError("Each note must contain a rows array")
            for row_index, row in enumerate(note["rows"]):
                if not isinstance(row, dict):
                    raise ValueError("Note rows must be objects")
                for key in ("value", "unit"):
                    value = row.get(key)
                    if key == "value" and value is None:
                        continue
                    if not isinstance(value, str):
                        raise ValueError("notes[%s].rows[%s].%s must be a decimal string" % (note_index, row_index, key))


def create_server(port=8765, audit_dir="audit-runs", root_dir=None):
    root = Path(root_dir) if root_dir else Path(__file__).resolve().parents[1]
    rules = _load_json((root / "rules/core.json").read_text(encoding="utf-8"))
    nonce = secrets.token_urlsafe(32)
    runs = {}

    class Handler(BaseHTTPRequestHandler):
        server_version = "TieoutLocal/0.1"

        def log_message(self, format, *args):
            # Avoid logging uploaded data, paths or financial content.
            return

        def _allowed_host(self):
            return self.headers.get("Host") in {
                "127.0.0.1:%s" % self.server.server_port,
                "localhost:%s" % self.server.server_port,
            }

        def _send(self, code, payload, content_type="application/json; charset=utf-8", filename=None):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if filename:
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self._allowed_host():
                return self._send(403, {"error": "Local access only"})
            path = urlsplit(self.path).path
            if path == "/":
                return self._send(200, (root / "web/index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/bootstrap":
                return self._send(200, {"csrf": nonce, "rules": rules})
            if path == "/api/demo":
                return self._send(200, _load_json((root / "examples/demo.json").read_text(encoding="utf-8")))
            if path.startswith("/api/runs/"):
                pieces = path.split("/")
                if len(pieces) != 5 or pieces[3] not in runs:
                    return self._send(404, {"error": "Run not found in this session"})
                result = runs[pieces[3]]
                formats = {
                    "json": (export_json, "application/json", "json"),
                    "csv": (export_csv, "text/csv; charset=utf-8", "csv"),
                    "xlsx": (export_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
                }
                if pieces[4] not in formats:
                    return self._send(404, {"error": "Unknown export format"})
                function, mime, extension = formats[pieces[4]]
                return self._send(200, function(result), mime, "tieout-%s.%s" % (pieces[3], extension))
            return self._send(404, {"error": "Not found"})

        def do_POST(self):
            if not self._allowed_host():
                return self._send(403, {"error": "Local access only"})
            expected_origin = "http://" + self.headers.get("Host", "")
            if self.headers.get("Origin") != expected_origin or self.headers.get("X-Tieout-CSRF") != nonce:
                return self._send(403, {"error": "Invalid local request. Reload this page."})
            path = urlsplit(self.path).path
            if path not in {"/api/review", "/api/input"}:
                return self._send(404, {"error": "Not found"})
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json" or self.headers.get("Transfer-Encoding"):
                return self._send(415, {"error": "Use a JSON request with Content-Length"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > MAX_BODY_BYTES:
                    return self._send(413, {"error": "Input must be between 1 byte and 5 MB"})
                self.connection.settimeout(15)
                raw = self.rfile.read(size).decode("utf-8-sig")
                # Keep JSON integer/decimal amounts untouched until the string
                # requirement has been enforced for browser-facing inputs.
                body = _load_json(raw)
                if path == "/api/input":
                    _validate_browser_amounts(body)
                    return self._send(200, body)
                if not isinstance(body, dict) or set(body) != {"data", "settings"}:
                    raise ValueError("Expected data and settings only")
                _validate_browser_amounts(body["data"])
                result = run_review(body["data"], rules, body["settings"])
                write_audit_run(result, audit_dir)
                runs[result["run_id"]] = result
                # Limit memory; every completed run remains saved on disk.
                while len(runs) > 100:
                    del runs[next(iter(runs))]
                return self._send(200, result)
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                return self._send(400, {"error": str(exc)})
            except OSError:
                return self._send(500, {"error": "Could not save the audit record. Check the local audit directory."})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port=8765, audit_dir="audit-runs"):
    server = create_server(port=port, audit_dir=audit_dir)
    print("Financial Statements Tie-Out: http://127.0.0.1:%s" % server.server_port, flush=True)
    print("Audit records: %s" % Path(audit_dir).resolve(), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
