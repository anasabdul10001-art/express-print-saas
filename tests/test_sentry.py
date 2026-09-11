"""
app/core/sentry.py - the "off by default" behavior is tested in-process
(cheap, safe). The "actually sends an event once a DSN is set" behavior is
tested in an ISOLATED SUBPROCESS instead of in-process: sentry_sdk.init()
mutates global SDK state that doesn't cleanly reset, and this is exactly how
init_sentry() actually gets called for real anyway - once, at process
startup (see app/main.py) - so a fresh subprocess is a more faithful test
than trying to reset global state inside the shared pytest process.
"""

import os
import subprocess
import sys

from app.core.sentry import init_sentry
import sentry_sdk


def test_init_sentry_is_a_noop_without_a_dsn():
    from app.config import settings
    assert settings.sentry_dsn == ""  # test env never sets one (see conftest.py)

    init_sentry()
    assert sentry_sdk.get_client().is_active() is False


def test_configured_sentry_actually_sends_an_event_to_the_dsn_host():
    script = """
import http.server
import json
import os
import threading

received = []

class FakeSentry(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        received.append(self.rfile.read(length))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass

server = http.server.HTTPServer(("127.0.0.1", 0), FakeSentry)
port = server.server_address[1]
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()

os.environ["SENTRY_DSN"] = f"http://fakepublickey@127.0.0.1:{port}/1"
os.environ["SENTRY_ENVIRONMENT"] = "test"

from app.core.sentry import init_sentry
init_sentry()

import sentry_sdk
assert sentry_sdk.get_client().is_active(), "client should be active once a DSN is set"

try:
    raise ValueError("deliberate test error")
except ValueError:
    sentry_sdk.capture_exception()

sentry_sdk.flush(timeout=5)
server.shutdown()

print("EVENTS_RECEIVED=" + str(len(received)))
"""
    # Inherits DATABASE_URL/SECRET_KEY/etc. that tests/conftest.py already set
    # on this process's own os.environ - app.config.Settings() needs them to
    # validate (it never actually connects to the database here). cwd is the
    # repo root so `from app.core.sentry import ...` resolves (python -c
    # puts the CWD on sys.path, same as running any script from there).
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=30, env=os.environ.copy(), cwd=repo_root,
    )
    assert "EVENTS_RECEIVED=" in result.stdout, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    count = int(result.stdout.strip().split("EVENTS_RECEIVED=")[1])
    assert count >= 1, f"expected the fake Sentry server to receive at least one event, got {count}"
