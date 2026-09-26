"""The live acceptance suite, run against local stand-ins so its own logic is tested in CI.

The stand-ins say nothing about the real providers. They prove that every check skips
cleanly without credentials, passes when a provider behaves, and that the secret canary
fails the run when a credential leaks into stored data.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from praxis.host.app import build_host
from praxis.host.auth import token_digest
from praxis.host.config import parse_host_config

ROOT = Path(__file__).resolve().parent.parent
KEYS = {"CODEX_API_KEY": "codex-canary-" + "1" * 20, "ANTHROPIC_API_KEY": "claude-canary-" + "2" * 20,
        "DEEPSEEK_API_KEY": "deepseek-canary-" + "3" * 20, "PRAXIS_ACCEPTANCE_NOESIS_TOKEN": "nn_canary" + "4" * 20,
        "PRAXIS_ACCEPTANCE_HOST_TOKEN": "host-canary-" + "5" * 30}

CODEX = '''import json, os, sys, time
if "--version" in sys.argv:
    print("codex-standin 0.0.0"); sys.exit(0)
task = json.loads(sys.stdin.read())["objective"]
print(json.dumps({"type": "thread.started"}), flush=True)
if "200000" in task:
    time.sleep(60)
if "answer.txt" in task:
    open("answer.txt", "w").write("accepted")
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}))
print(json.dumps({"type": "turn.completed"}))
'''

CLAUDE = '''class ClaudeAgentOptions:
    def __init__(self, **options):
        self.options = options

class ResultMessage:
    def __init__(self, subtype, is_error, result):
        self.subtype, self.is_error, self.result = subtype, is_error, result

async def query(prompt, options):
    yield ResultMessage("success", False, "ok")
'''

DEEPSEEK = '''import json
import os
from pathlib import Path

class Result:
    finish_reason = "completed"
    final_response = "ok"

class DeepSeekHarness:
    def __init__(self, cwd, **options):
        self.cwd = Path(cwd)
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def run(self, prompt, session_id):
        if "answer.txt" in json.loads(prompt)["objective"]:
            # A misbehaving agent can copy its own credential into an artifact.
            leak = os.environ.get("STANDIN_LEAK")
            (self.cwd / "answer.txt").write_text(os.environ["DEEPSEEK_API_KEY"] if leak else "accepted")
        return Result()
'''


class Noesis(BaseHTTPRequestHandler):
    searched: list[str] = []

    def log_message(self, *args):
        pass

    def reply(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200 if self.headers.get("Authorization") == "Bearer " + KEYS[
            "PRAXIS_ACCEPTANCE_NOESIS_TOKEN"] else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        domain = urlsplit(self.path).path.split("/")[4]
        Noesis.searched.append(domain)
        self.reply({"contract": "noesis-kb-v1", "domain": domain, "as_of_ms": 1_700_000_000_000,
                    "data": [{"id": "doc-1", "text": "praxis"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.reply({"document_id": body["document_id"]})


def bridge(app, loop):
    """Serve an ASGI app over plain HTTP/1.0 for a client in another process."""
    class Bridge(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_one(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            parts = urlsplit(self.path)
            scope = {"type": "http", "method": self.command, "path": parts.path,
                     "query_string": parts.query.encode(),
                     "headers": [(k.lower().encode(), v.encode()) for k, v in self.headers.items()]}
            messages, first = [], [True]

            async def receive():
                if first[0]:
                    first[0] = False
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Event().wait()

            async def send(message):
                messages.append(message)
            asyncio.run_coroutine_threadsafe(app(scope, receive, send), loop).result(60)
            self.send_response(messages[0]["status"])
            for key, value in messages[0].get("headers", []):
                self.send_header(key.decode(), value.decode())
            self.end_headers()
            for message in messages[1:]:
                self.wfile.write(message.get("body", b""))

        do_GET = do_POST = handle_one
    return Bridge


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def clean_environment(tmp_path: Path) -> dict[str, str]:
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith("PRAXIS_ACCEPTANCE_") and k not in KEYS}
    environment["PRAXIS_ACCEPTANCE_RECORD"] = str(tmp_path / "record.json")
    return environment


def run_suite(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "pytest", "acceptance", "-q", "-rs", "-p", "no:cacheprovider"],
                          cwd=ROOT, env=environment, capture_output=True, text=True, timeout=300)


def test_without_credentials_every_check_skips(tmp_path):
    result = run_suite(clean_environment(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((tmp_path / "record.json").read_text())
    assert len(record["checks"]) == 11 and {c["outcome"] for c in record["checks"]} == {"skipped"}
    assert record["secret_canary"] == {"leaks": [], "values_checked": 0}


@pytest.fixture
def standins(tmp_path):
    stand = tmp_path / "stand"
    (stand / "modules").mkdir(parents=True)
    codex = stand / "codex"
    codex.write_text(f"#!{sys.executable}\n" + CODEX)
    codex.chmod(0o755)
    (stand / "modules" / "claude_agent_sdk.py").write_text(CLAUDE)
    (stand / "modules" / "deepseek_harness.py").write_text(DEEPSEEK)
    noesis = serve(Noesis)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    config = parse_host_config({"data_dir": str(tmp_path / "host"), "clients": [{
        "id": "modulo", "token_sha256": token_digest(KEYS["PRAXIS_ACCEPTANCE_HOST_TOKEN"]), "delegate": True,
        "roles": ["submit", "read"]}]})
    host = build_host(config)
    served = serve(bridge(host.app, loop))
    environment = {**clean_environment(tmp_path), **KEYS,
                   "PYTHONPATH": os.pathsep.join(filter(None, [str(stand / "modules"), os.environ.get("PYTHONPATH")])),
                   "PRAXIS_ACCEPTANCE_ISOLATED_WORKER": "1", "PRAXIS_ACCEPTANCE_CODEX_BIN": str(codex),
                   "PRAXIS_ACCEPTANCE_CANCEL_AFTER": "0.5",
                   "PRAXIS_ACCEPTANCE_NOESIS_URL": f"http://127.0.0.1:{noesis.server_port}",
                   "PRAXIS_ACCEPTANCE_NOESIS_DOMAIN": "support", "PRAXIS_ACCEPTANCE_NOESIS_PUBLISH": "1",
                   "PRAXIS_ACCEPTANCE_NOESIS_INGEST_PATH": "/scratch/documents/ingest",
                   "PRAXIS_ACCEPTANCE_HOST_URL": f"http://127.0.0.1:{served.server_port}"}
    Noesis.searched = []
    yield environment
    served.shutdown()
    noesis.shutdown()
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    host.close()


def test_every_check_passes_against_well_behaved_standins(tmp_path, standins):
    result = run_suite(standins)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((tmp_path / "record.json").read_text())
    assert {c["check"]: c["outcome"] for c in record["checks"]} == {c["check"]: "passed" for c in record["checks"]}
    assert len(record["checks"]) == 11
    assert record["versions"]["codex"] == "codex-standin 0.0.0"
    assert record["secret_canary"] == {"leaks": [], "values_checked": 5}
    # The allowlisted domain was searched; the refused one never reached Noesis.
    assert "support" in Noesis.searched and "praxis-acceptance-not-allowed" not in Noesis.searched
    assert not any(value in (tmp_path / "record.json").read_text() for value in KEYS.values())


def test_secret_canary_fails_the_run_on_a_leak(tmp_path, standins):
    result = run_suite({**standins, "STANDIN_LEAK": "1"})
    assert result.returncode != 0
    record = json.loads((tmp_path / "record.json").read_text())
    assert record["secret_canary"]["leaks"], record["secret_canary"]
    assert KEYS["CODEX_API_KEY"] not in (tmp_path / "record.json").read_text()
