import asyncio
import json
import shutil
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from praxis.host.app import build_host
from praxis.host.auth import token_digest
from praxis.host.config import HostConfigError, parse_host_config
from praxis.transport.http import HTTPTransport, TransportError

MODULO, OPS, OTHER = "m" * 40, "o" * 40, "x" * 40


def config(tmp_path, publication="manual", **extra):
    data = {
        "data_dir": str(tmp_path / "data"),
        "clients": [
            {"id": "modulo", "token_sha256": token_digest(MODULO), "delegate": True,
             "roles": ["submit", "read", "control", "approve", "publish"]},
            {"id": "ops", "token_sha256": token_digest(OPS), "roles": ["admin"]},
            {"id": "other", "token_sha256": token_digest(OTHER), "roles": ["submit", "read"]},
        ],
        "noesis": {"base_url": "https://noesis.example", "token_env": "NOESIS_TOKEN", "publication": publication},
    }
    data.update(extra)
    return parse_host_config(data)


class Noesis:
    def __init__(self):
        self.documents = []
        self.fail = None

    async def request(self, method, path, body=None):
        if method == "GET":
            return {"contract": "noesis-kb-v1", "domain": "docs", "as_of_ms": 1784700000000,
                    "data": [{"id": "doc-1", "title": "Refunds", "content": "30 days"}]}
        if self.fail is not None:
            raise self.fail
        assert (method, path) == ("POST", "/documents/ingest")
        self.documents.append(body)
        return {**body, "ingested_at": 1}


async def call(app, method, path, token=None, data=None, headers=()):
    messages = []
    raw = [(b"authorization", b"Bearer " + token.encode())] if token else []
    async def receive():
        return {"type": "http.request", "body": json.dumps(data or {}).encode()}
    async def send(message):
        messages.append(message)
    await app({"type": "http", "method": method, "path": path, "headers": raw + list(headers)}, receive, send)
    return messages[0]["status"], json.loads(messages[1]["body"])


def as_user(name):
    return ((b"x-praxis-on-behalf-of", name.encode()),)


@pytest.mark.parametrize("change,field", [
    ({"data_dir": None}, "data_dir"),
    ({"executors": ["claude"]}, "executors"),
    ({"surprise": 1}, "root"),
    ({"server": {"host": "0.0.0.0"}}, "server.host"),
    ({"server": {"tls_certfile": "c.pem"}}, "server.tls_keyfile"),
    ({"server": {"tls_client_ca": "ca.pem"}}, "server.tls_client_ca"),
    ({"clients": []}, "clients"),
    ({"clients": [{"id": "Modulo", "token_sha256": "0" * 64, "roles": ["read"]}]}, "clients[0].id"),
    ({"clients": [{"id": "m", "token_sha256": "secret", "roles": ["read"]}]}, "clients[0].token_sha256"),
    ({"clients": [{"id": "m", "token_sha256": "0" * 64, "roles": ["root"]}]}, "clients[0].roles"),
    ({"noesis": {"base_url": "http://noesis.example", "token_env": "T"}}, "noesis.base_url"),
    ({"noesis": {"base_url": "https://n.example"}}, "noesis.token_env"),
    ({"noesis": {"base_url": "https://n.example", "token_env": "T", "publication": "always"}}, "noesis.publication"),
])
def test_config_is_closed_and_fails_safe(tmp_path, change, field):
    data = {"data_dir": str(tmp_path), "clients": [{"id": "m", "token_sha256": "0" * 64, "roles": ["read"]}]}
    data.update(change)
    if change.get("data_dir", "") is None:
        del data["data_dir"]
    with pytest.raises(HostConfigError) as error:
        parse_host_config(data)
    assert error.value.field == field


def test_config_accepts_tls_and_upstream_termination(tmp_path):
    clients = [{"id": "m", "token_sha256": "0" * 64, "roles": ["read"]}]
    tls = parse_host_config({"data_dir": "d", "clients": clients, "server": {
        "host": "0.0.0.0", "tls_certfile": "c", "tls_keyfile": "k", "tls_client_ca": "ca"}})
    assert tls.server.tls and tls.server.tls_client_ca == "ca"
    proxied = parse_host_config({"data_dir": "d", "clients": clients, "server": {
        "host": "10.0.0.5", "tls_terminated_upstream": True}})
    assert not proxied.server.tls
    local = parse_host_config({"data_dir": "d", "clients": clients, "noesis": {
        "base_url": "http://127.0.0.1:8000", "token_file": "t"}})
    assert local.noesis.publication == "manual"


def test_authentication_roles_and_ownership(tmp_path):
    async def exercise():
        host = build_host(config(tmp_path), noesis_transport=Noesis())
        app = host.app
        assert (await call(app, "GET", "/v1/health"))[0] == 401
        assert (await call(app, "GET", "/v1/health", "wrong-token"))[0] == 401
        assert (await call(app, "GET", "/v1/health", MODULO))[0] == 403
        assert (await call(app, "GET", "/v1/health", OPS))[0] == 200
        # Only delegating clients may name an end user, and names are constrained.
        assert (await call(app, "GET", "/v1/health", OTHER, headers=as_user("alice")))[0] == 401
        assert (await call(app, "POST", "/v1/processes", MODULO, {"objective": "t", "executor": "fake"},
                           as_user("bad name")))[0] == 401

        spec = {"objective": "task", "executor": "fake"}
        status, alice = await call(app, "POST", "/v1/processes", MODULO, spec, as_user("alice"))
        assert status == 202
        pid = alice["process_id"]
        await host.kernel.tasks[pid]
        assert host.security.owner(pid) == "modulo/alice"
        assert (await call(app, "GET", f"/v1/processes/{pid}", MODULO, headers=as_user("alice")))[0] == 200
        assert (await call(app, "GET", f"/v1/processes/{pid}", MODULO, headers=as_user("bob")))[0] == 403
        assert (await call(app, "GET", f"/v1/processes/{pid}", MODULO))[0] == 200  # service sees its users
        assert (await call(app, "GET", f"/v1/processes/{pid}", OTHER))[0] == 403
        assert (await call(app, "GET", f"/v1/processes/{pid}", OPS))[0] == 200
        assert (await call(app, "GET", "/v1/processes/missing", OTHER))[0] == 404
        # A client without the control role cannot cancel even its own work.
        _, own = await call(app, "POST", "/v1/processes", OTHER, spec)
        await host.kernel.tasks[own["process_id"]]
        assert (await call(app, "POST", f"/v1/processes/{own['process_id']}/control", OTHER,
                           {"operation": "cancel"}))[0] == 403
        audit = [e for e in host.kernel.events if e.process_id == pid and e.type == "api.action"]
        assert audit[0].payload["actor"] == "modulo/alice"
        host.close()
    asyncio.run(exercise())


def test_manual_publication_route(tmp_path):
    async def exercise():
        noesis = Noesis()
        host = build_host(config(tmp_path), noesis_transport=noesis)
        app = host.app
        _, submitted = await call(app, "POST", "/v1/processes", MODULO, {"objective": "answer", "executor": "fake"},
                                  as_user("alice"))
        pid = submitted["process_id"]
        route = f"/v1/processes/{pid}/publication"
        assert (await call(app, "POST", route, OTHER))[0] == 403
        await host.kernel.tasks[pid]
        status, body = await call(app, "POST", route, MODULO, headers=as_user("alice"))
        assert status == 200 and body["publication"]["status"] == "accepted"
        assert noesis.documents[0]["metadata"]["praxis"]["verified"] is True
        assert noesis.documents[0]["document_id"] == body["publication"]["document_id"]
        status, again = await call(app, "POST", route, MODULO, headers=as_user("alice"))
        assert status == 200 and again["publication"]["status"] == "duplicate" and len(noesis.documents) == 1
        assert (await call(app, "GET", route, MODULO))[0] == 405
        requested = [e for e in host.kernel.events if e.type == "host.publication_requested"]
        assert requested[0].payload["actor"] == "modulo/alice"

        # A failed process is never published, even when explicitly requested.
        from praxis.executors.fake import FakeExecutor
        from praxis.executors.outcomes import Outcome, OutcomeStatus
        host.kernel.executors["fake"] = FakeExecutor(Outcome(OutcomeStatus.FAILED, "broken"))
        _, failed = await call(app, "POST", "/v1/processes", MODULO, {"objective": "bad", "executor": "fake"})
        await host.kernel.tasks[failed["process_id"]]
        status, body = await call(app, "POST", f"/v1/processes/{failed['process_id']}/publication", MODULO)
        assert status == 409 and body["publication"]["reason"] == "publication_ineligible"
        host.close()
    asyncio.run(exercise())


def test_publication_off_and_unconfigured(tmp_path):
    async def exercise():
        host = build_host(config(tmp_path, "off"), noesis_transport=Noesis())
        _, submitted = await call(host.app, "POST", "/v1/processes", MODULO, {"objective": "t", "executor": "fake"})
        await host.kernel.tasks[submitted["process_id"]]
        assert host.publisher is None and "noesis" in host.context_providers
        status, _ = await call(host.app, "POST", f"/v1/processes/{submitted['process_id']}/publication", MODULO)
        assert status == 404
        host.close()
    asyncio.run(exercise())


def test_auto_publication_worker_retries_and_survives_restart(tmp_path):
    async def exercise():
        noesis = Noesis()
        now = [1000.0]
        host = build_host(config(tmp_path, "auto"), noesis_transport=noesis)
        host.worker._now = lambda: now[0]
        spec = {"objective": "answer", "executor": "fake"}
        _, opted = await call(host.app, "POST", "/v1/processes", MODULO,
                              {**spec, "metadata": {"praxis_host": {"publish": True}}}, as_user("alice"))
        _, silent = await call(host.app, "POST", "/v1/processes", MODULO, spec)
        # "other" lacks the publish role, so its request for publication grants nothing.
        _, denied = await call(host.app, "POST", "/v1/processes", OTHER,
                               {**spec, "metadata": {"praxis_host": {"publish": True}}})
        for item in (opted, silent, denied):
            await host.kernel.tasks[item["process_id"]]

        noesis.fail = TransportError("transport_unavailable")
        results = await host.worker.run_once()
        assert [(pid, r.status) for pid, r in results] == [(opted["process_id"], "unavailable")]
        assert host.worker.pending()[0][2] == 1
        assert await host.worker.run_once() == []  # backing off
        noesis.fail = None
        host.close()

        # Restart: the queue and cursor are durable, so nothing is lost or repeated.
        restarted = build_host(config(tmp_path, "auto"), noesis_transport=noesis)
        restarted.worker._now = lambda: now[0] + 10
        results = await restarted.worker.run_once()
        assert [(pid, r.status) for pid, r in results] == [(opted["process_id"], "accepted")]
        assert restarted.worker.pending() == () and len(noesis.documents) == 1
        assert await restarted.worker.run_once() == []
        restarted.close()
    asyncio.run(exercise())


def test_lifespan_runs_worker(tmp_path):
    async def exercise():
        host = build_host(config(tmp_path, "auto"), noesis_transport=Noesis())
        host.worker.interval = 0.01
        _, submitted = await call(host.app, "POST", "/v1/processes", MODULO, {"objective": "t", "executor": "fake"})
        await host.kernel.tasks[submitted["process_id"]]
        inbox = asyncio.Queue()
        for kind in ("lifespan.startup", "lifespan.shutdown"):
            inbox.put_nowait({"type": kind})
        sent = []
        async def receive():
            if inbox.qsize() == 1:
                await asyncio.sleep(0.05)
            return await inbox.get()
        async def send(message):
            sent.append(message["type"])
        await host.app({"type": "lifespan"}, receive, send)
        assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
        assert host.app.worker_task is None and host.worker.cursor() > 0
        host.close()
    asyncio.run(exercise())


def test_noesis_context_flows_into_execution(tmp_path):
    async def exercise():
        host = build_host(config(tmp_path), noesis_transport=Noesis())
        spec = {"objective": "answer", "executor": "fake", "context": [{
            "context_id": "docs", "provider": "noesis",
            "request": {"query": "refunds", "filters": [["domain", "docs"]]}}]}
        _, submitted = await call(host.app, "POST", "/v1/processes", MODULO, spec)
        await host.kernel.tasks[submitted["process_id"]]
        resolved = [e for e in host.kernel.events if e.type == "context.resolved"]
        assert resolved[0].payload["response"]["items"][0]["source_id"] == "doc-1"
        host.close()
    asyncio.run(exercise())


def test_noesis_credentials_come_from_environment(tmp_path):
    with pytest.raises(ValueError, match="credential"):
        build_host(config(tmp_path), environ={})
    host = build_host(config(tmp_path), environ={"NOESIS_TOKEN": "nn_" + "k" * 32})
    transport = host.noesis_transport.current
    assert transport.headers["Authorization"] == "Bearer nn_" + "k" * 32 and transport.ssl_context is not None
    host.close()


def openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI required to mint test certificates")
def test_transport_mutual_tls(tmp_path):
    san = tmp_path / "san.cnf"
    san.write_text("subjectAltName=IP:127.0.0.1\n")
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=test-ca",
            "-keyout", "ca.key", "-out", "ca.pem", cwd=tmp_path)
    for name, extra in (("server", ["-extfile", "san.cnf"]), ("client", [])):
        openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={name}", "-keyout", f"{name}.key",
                "-out", f"{name}.csr", cwd=tmp_path)
        openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
                "-days", "1", "-out", f"{name}.pem", *extra, cwd=tmp_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            peer = self.connection.getpeercert()
            subject = dict(item[0] for item in peer["subject"])
            body = json.dumps({"client": subject["commonName"]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=str(tmp_path / "ca.pem"))
    context.load_cert_chain(tmp_path / "server.pem", tmp_path / "server.key")
    context.verify_mode = ssl.CERT_REQUIRED
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"https://127.0.0.1:{server.server_port}"

    from praxis.host.app import noesis_ssl_context
    from praxis.host.config import NoesisConfig

    async def exercise():
        mutual = noesis_ssl_context(NoesisConfig(base, token_env="T", ca_file=str(tmp_path / "ca.pem"),
            client_certfile=str(tmp_path / "client.pem"), client_keyfile=str(tmp_path / "client.key")))
        assert await HTTPTransport(base, ssl_context=mutual).request("GET", "/") == {"client": "client"}
        anonymous = noesis_ssl_context(NoesisConfig(base, token_env="T", ca_file=str(tmp_path / "ca.pem")))
        with pytest.raises(TransportError, match="transport_unavailable"):
            await HTTPTransport(base, ssl_context=anonymous).request("GET", "/")
        with pytest.raises(TransportError, match="transport_unavailable"):  # untrusted server certificate
            await HTTPTransport(base).request("GET", "/")
    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    with pytest.raises(ValueError, match="https"):
        HTTPTransport("http://127.0.0.1:1", ssl_context=ssl.create_default_context())


def test_host_cli(tmp_path):
    def run(*args):
        return subprocess.run([sys.executable, "-m", "praxis.host", *args], capture_output=True, text=True)
    issued = run("token", "--client", "modulo")
    assert issued.returncode == 0 and 'id = "modulo"' in issued.stdout
    token = issued.stderr.split()[-1]
    assert token_digest(token) in issued.stdout
    path = tmp_path / "host.toml"
    path.write_text(f'data_dir = "{tmp_path / "data"}"\n' + issued.stdout)
    checked = run("check", "--config", str(path))
    assert checked.returncode == 0 and "loopback only" in checked.stdout
    path.write_text(path.read_text() + '[server]\nhost = "0.0.0.0"\n')
    assert run("check", "--config", str(path)).returncode == 2
