import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from tieout.server import MAX_BODY_BYTES, create_server


DATA = {
    "schema_version": 1, "entity": "Test", "period": "2026", "currency": "USD",
    "facts": [
        {"key": "A", "value": "1000.01", "unit": "1", "source": "fs.json", "location": "A1"},
        {"key": "B", "value": "1000", "unit": "1", "source": "fs.json", "location": "B1"},
    ], "tb": [], "mapping": [],
}
RULES = [{"id": "TEST-01", "name": "Equality", "category": "Test", "description": "Test arithmetic", "lhs": [{"key": "A", "coefficient": "1"}], "rhs": [{"key": "B", "coefficient": "1"}]}]
SETTINGS = {"tolerance": "0.01", "reviewer": "Reviewer", "reason": "Test setting", "overrides": {}, "conditions": {}, "not_applicable": {}}


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        for name in ("rules", "examples", "web"):
            (cls.root / name).mkdir()
        (cls.root / "rules/core.json").write_text(json.dumps(RULES))
        (cls.root / "examples/demo.json").write_text(json.dumps(DATA))
        (cls.root / "web/index.html").write_text("<!doctype html><title>Test</title>")
        cls.server = create_server(port=0, audit_dir=cls.root / "audit", root_dir=cls.root)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = "127.0.0.1:%s" % cls.server.server_port
        conn = http.client.HTTPConnection("127.0.0.1", cls.server.server_port, timeout=3)
        conn.request("GET", "/api/bootstrap")
        cls.csrf = json.loads(conn.getresponse().read())["csrf"]
        conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, path, body=None, headers=None, method=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        defaults = {"Host": self.host}
        if body is not None:
            defaults.update({"Content-Type": "application/json", "Origin": "http://" + self.host, "X-Tieout-CSRF": self.csrf})
            if not isinstance(body, (str, bytes)):
                body = json.dumps(body)
        defaults.update(headers or {})
        conn.request(method or ("POST" if body is not None else "GET"), path, body=body, headers=defaults)
        response = conn.getresponse()
        status, content, response_headers = response.status, response.read(), dict(response.getheaders())
        conn.close()
        return status, content, response_headers

    def test_bootstrap_html_and_loopback_binding(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, body, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_review_persists_exact_record_and_downloads_server_result(self):
        status, body, _ = self.request("/api/review", {"data": DATA, "settings": SETTINGS})
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        row = next(row for row in result["results"] if row["rule_id"] == "TEST-01")
        self.assertEqual(row["status"], "WITHIN_TOLERANCE")
        self.assertEqual(row["difference"], "0.01")
        stored = json.loads((self.root / "audit" / (result["run_id"] + ".json")).read_text())
        self.assertEqual(stored, result)
        status, downloaded, _ = self.request("/api/runs/%s/json" % result["run_id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(downloaded), result)
        status, downloaded, headers = self.request("/api/runs/%s/xlsx" % result["run_id"])
        self.assertEqual(status, 200)
        self.assertTrue(downloaded.startswith(b"PK"))
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_foreign_host_and_origin_and_missing_csrf_rejected(self):
        status, _, _ = self.request("/api/bootstrap", headers={"Host": "evil.test"})
        self.assertEqual(status, 403)
        for headers in ({"Origin": "https://evil.test"}, {"X-Tieout-CSRF": ""}):
            status, _, _ = self.request("/api/review", {"data": DATA, "settings": SETTINGS}, headers)
            self.assertEqual(status, 403)

    def test_oversized_and_non_json_payload_rejected(self):
        status, _, _ = self.request("/api/review", "{}", {"Content-Length": str(MAX_BODY_BYTES + 1)})
        self.assertEqual(status, 413)
        status, _, _ = self.request("/api/review", "{}", {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_uploaded_numbers_rejected_before_browser_precision_loss(self):
        for raw in ('9007199254740993', '1.234567890123456789'):
            body = '{"facts":[{"key":"A","value":' + raw + ',"unit":"1"}],"tb":[]}'
            status, reply, _ = self.request("/api/input", body)
            self.assertEqual(status, 400)
            self.assertIn(b"decimal string", reply)
        data = json.loads(json.dumps(DATA))
        data["facts"][0]["value"] = "9007199254740993.123456789"
        status, reply, _ = self.request("/api/input", data)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(reply)["facts"][0]["value"], "9007199254740993.123456789")

    def test_duplicate_json_keys_and_nonfinite_values_rejected(self):
        for raw in ('{"facts":[],"facts":[]}', '{"facts":[],"number":NaN}'):
            status, _, _ = self.request("/api/input", raw)
            self.assertEqual(status, 400)

    def test_note_numeric_amounts_rejected(self):
        data = json.loads(json.dumps(DATA))
        data["notes"] = [{"id": "N1", "rows": [{"value": 9007199254740993, "unit": "1"}]}]
        status, reply, _ = self.request("/api/input", data)
        self.assertEqual(status, 400)
        self.assertIn(b"decimal string", reply)

    def test_paths_and_untrusted_result_uploads_are_not_served(self):
        for path in ("/../../CONTRACT.md", "/api/runs/fake/json", "/api/runs/../json", "/rules/core.json"):
            status, _, _ = self.request(path)
            self.assertEqual(status, 404)
        status, _, _ = self.request("/api/review", {"data": DATA, "settings": SETTINGS, "results": []})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
