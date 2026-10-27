"""MetaMuseClient against a local HTTP server: request shape, retries, errors, no key leakage."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from shipcube_data.drafting import MuseDrafter
from shipcube_data.muse import MetaMuseClient, MuseError, muse_from_env


class FakeMeta:
    def __init__(self, script):
        self.script, self.requests = list(script), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "auth": self.headers["Authorization"], "body": body})
                status, payload = outer.script.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()


def ok(content):
    return 200, {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def test_request_shape_and_success():
    fake = FakeMeta([ok('{"ops": []}')])
    try:
        client = MetaMuseClient("sk-test", base_url=fake.url, attempts=1)
        assert client("sys", "user") == '{"ops": []}'
        req = fake.requests[0]
        assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer sk-test"
        assert req["body"]["model"] == "muse-spark-1.3" and req["body"]["reasoning_effort"] == "low"
        assert req["body"]["max_tokens"] >= 1000 and [m["role"] for m in req["body"]["messages"]] == ["system", "user"]
    finally:
        fake.close()


def test_retries_transient_errors_then_succeeds(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = FakeMeta([(503, {}), (429, {}), ok("hi")])
    try:
        assert MetaMuseClient("k", base_url=fake.url, attempts=3)("s", "u") == "hi"
        assert len(fake.requests) == 3
    finally:
        fake.close()


def test_client_errors_are_not_retried():
    fake = FakeMeta([(400, {"error": "bad"})])
    try:
        with pytest.raises(MuseError, match="HTTP 400"):
            MetaMuseClient("k", base_url=fake.url)("s", "u")
        assert len(fake.requests) == 1
    finally:
        fake.close()


def test_empty_reasoning_only_reply_is_an_error(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = FakeMeta([ok("")])
    try:
        with pytest.raises(MuseError, match="empty"):
            MetaMuseClient("k", base_url=fake.url, attempts=1)("s", "u")
    finally:
        fake.close()


def test_key_never_printed_and_env_wiring(monkeypatch):
    assert "secret-key" not in repr(MetaMuseClient("secret-key"))
    monkeypatch.delenv("META_API_KEY", raising=False)
    assert muse_from_env() is None and MuseDrafter.from_env().muse is None
    monkeypatch.setenv("META_API_KEY", "x")
    monkeypatch.setenv("META_MUSE_MODEL", "muse-spark-1.2")
    assert muse_from_env().model == "muse-spark-1.2"


def test_drafter_falls_back_when_muse_is_down(runner, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = FakeMeta([(503, {})] * 3)
    try:
        d = MuseDrafter(MetaMuseClient("k", base_url=fake.url)).draft(
            runner.catalog.load(), "duckdb", "add measure buyers as count distinct of customer_id on orders", "")
        assert d.explained_by == "rules" and "MuseError" in d.notes[0]
    finally:
        fake.close()
