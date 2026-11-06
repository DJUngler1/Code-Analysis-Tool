import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scheduler.ollama import check_ollama


class _FakeOllama(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (name given by BaseHTTPRequestHandler)
        body = json.dumps({"models": [{"name": "qwen3:8b"}, {"name": "llama3.2:3b"}]}).encode()
        self.send_response(200 if self.path == "/api/tags" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_ollama_url():
    server = HTTPServer(("127.0.0.1", 0), _FakeOllama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_reachable(fake_ollama_url):
    status = check_ollama(fake_ollama_url)
    assert status.reachable
    assert status.models == ("qwen3:8b", "llama3.2:3b")


def test_unreachable():
    status = check_ollama(f"http://127.0.0.1:{unused_port()}", timeout=1)
    assert not status.reachable
    assert status.error
