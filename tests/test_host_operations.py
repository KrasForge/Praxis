"""Host operations: Noesis domain allowlist, admission limits, deadlines and reload."""
import asyncio
import os
import shutil
import signal
import socket
import ssl
import subprocess
import threading

import pytest

from praxis.host.app import build_host
from praxis.host.auth import token_digest
from praxis.host.config import HostConfigError, parse_host_config
from praxis.host.limits import Limiter
from praxis.observability.redaction import RedactionPolicy

from test_host import MODULO, OPS, OTHER, Noesis, as_user, call

NEW = "n" * 40


def data(tmp_path, *, modulo=(MODULO,), limits=None, noesis=None):
    return {
        "data_dir": str(tmp_path / "data"),
        "clients": [
            {"id": "modulo", "token_sha256": [token_digest(t) for t in modulo], "delegate": True,
             "roles": ["submit", "read", "control", "publish"]},
            {"id": "ops", "token_sha256": token_digest(OPS), "roles": ["admin"]},
            {"id": "other", "token_sha256": token_digest(OTHER), "roles": ["submit", "read"]},
        ],
        "noesis": noesis or {"base_url": "https://noesis.example", "token_env": "NOESIS_TOKEN"},
        **({} if limits is None else {"limits": limits}),
    }


class Recording(Noesis):
    def __init__(self):
        super().__init__()
        self.searches = []

    async def request(self, method, path, body=None):
        if method == "GET":
            self.searches.append(path)
        return await super().request(method, path, body)


def context_spec(domain, required=True):
    return {"objective": "answer", "executor": "fake", "context": [{
        "context_id": "docs", "provider": "noesis", "required": required,
        "request": {"query": "refunds", "filters": [["domain", domain]]}}]}


# --- #243: domain allowlist -------------------------------------------------------------

def test_context_domain_allowlist(tmp_path):
    async def exercise():
        noesis = Recording()
        config = parse_host_config(data(tmp_path, noesis={
            "base_url": "https://noesis.example", "token_env": "T", "context_domains": ["docs"]}))
        host = build_host(config, noesis_transport=noesis)
        _, allowed = await call(host.app, "POST", "/v1/processes", MODULO, context_spec("docs"))
        _, denied = await call(host.app, "POST", "/v1/processes", MODULO, context_spec("payroll"))
        for item in (allowed, denied):
            await asyncio.gather(host.kernel.tasks[item["process_id"]], return_exceptions=True)
        assert noesis.searches == ["/api/v1/kb/docs/search?q=refunds&limit=10"]
        assert host.kernel.processes[allowed["process_id"]].state.value == "completed"
        assert host.kernel.processes[denied["process_id"]].state.value == "failed"
        resolved = [e for e in host.kernel.events if e.type == "context.resolved" and e.process_id == denied["process_id"]]
        assert resolved[0].payload["response"]["reason"] == "noesis_domain_not_allowed"
        host.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("domains", [[], ["docs", "docs"], [""], ["a/b"], "docs"])
def test_context_domains_validation(domains):
    with pytest.raises(HostConfigError) as error:
        parse_host_config({"data_dir": "d", "clients": [{"id": "m", "token_sha256": "0" * 64, "roles": ["read"]}],
                           "noesis": {"base_url": "https://n.example", "token_env": "T", "context_domains": domains}})
    assert error.value.field == "noesis.context_domains"


# --- #244: limits and deadlines ----------------------------------------------------------

def limited_host(tmp_path, limits):
    host = build_host(parse_host_config(data(tmp_path, limits=limits)), noesis_transport=Noesis())
    now = [0.0]
    host.app.limiter = Limiter(host.config.limits, clock=lambda: now[0])
    return host, now


def test_request_and_submission_rate_limits(tmp_path):
    async def exercise():
        host, now = limited_host(tmp_path, {"requests_per_minute": 3, "submit_per_minute": 1,
                                            "client_requests_per_minute": 5})
        app = host.app
        spec = {"objective": "t", "executor": "fake"}
        assert (await call(app, "POST", "/v1/processes", MODULO, spec, as_user("alice")))[0] == 202
        status, body = await call(app, "POST", "/v1/processes", MODULO, spec, as_user("alice"))
        assert status == 429 and body["error"]["code"] == "rate_limited"
        # Bob has his own buckets.
        assert (await call(app, "POST", "/v1/processes", MODULO, spec, as_user("bob")))[0] == 202
        assert (await call(app, "GET", "/v1/processes/missing", MODULO, headers=as_user("alice")))[0] == 404
        # Alice has used her 3 requests; the client aggregate (5) is at 4.
        assert (await call(app, "GET", "/v1/processes/missing", MODULO, headers=as_user("alice")))[0] == 429
        assert (await call(app, "GET", "/v1/processes/missing", MODULO, headers=as_user("carol")))[0] == 404
        assert (await call(app, "GET", "/v1/processes/missing", MODULO, headers=as_user("dave")))[0] == 429
        # Admins are exempt; unauthenticated requests are refused without touching a bucket.
        for _ in range(5):
            assert (await call(app, "GET", "/v1/health", OPS))[0] == 200
            assert (await call(app, "GET", "/v1/health"))[0] == 401
        now[0] = 60.0
        assert (await call(app, "GET", "/v1/processes/missing", MODULO, headers=as_user("alice")))[0] == 404
        assert app.limiter.rejections["rate_limited"] == 3
        # Let the admitted submissions finish before the store closes under them.
        await asyncio.gather(*host.kernel.tasks.values(), return_exceptions=True)
        host.close()
    asyncio.run(exercise())


def test_rate_limit_sets_retry_after(tmp_path):
    async def exercise():
        host, _ = limited_host(tmp_path, {"requests_per_minute": 1})
        await call(host.app, "GET", "/v1/processes/missing", OTHER)
        sent = []
        async def receive():
            return {"type": "http.request", "body": b""}
        async def send(message):
            sent.append(message)
        await host.app({"type": "http", "method": "GET", "path": "/v1/processes/missing",
                        "headers": [(b"authorization", b"Bearer " + OTHER.encode())]}, receive, send)
        assert sent[0]["status"] == 429 and (b"retry-after", b"60") in sent[0]["headers"]
        host.close()
    asyncio.run(exercise())


def test_concurrent_stream_limit_is_released_on_disconnect(tmp_path):
    async def exercise():
        from praxis.kernel.spec import ProcessSpec
        host, _ = limited_host(tmp_path, {"max_streams": 1})
        pending = host.kernel.create(ProcessSpec("never started", "fake"), submission_actor="other")
        path = f"/v1/processes/{pending.process_id}/events"

        async def stream(stop):
            sent = []
            async def receive():
                await stop.wait()
                return {"type": "http.disconnect"}
            async def send(message):
                sent.append(message)
            await host.app({"type": "http", "method": "GET", "path": path, "query_string": b"",
                            "headers": [(b"authorization", b"Bearer " + OTHER.encode())]}, receive, send)
            return sent[0]["status"]

        first_stop = asyncio.Event()
        first = asyncio.create_task(stream(first_stop))
        await asyncio.sleep(0.05)
        assert host.app.limiter.streams["other"] == 1
        second_stop = asyncio.Event()
        second_stop.set()
        assert await stream(second_stop) == 429
        first_stop.set()
        assert await first == 200
        assert host.app.limiter.streams == {}
        third_stop = asyncio.Event()
        third_stop.set()
        assert await stream(third_stop) == 200
        host.close()
    asyncio.run(exercise())


def test_request_deadline_answers_503_and_lets_the_operation_finish(tmp_path):
    async def exercise():
        host, _ = limited_host(tmp_path, {"request_timeout_seconds": 0.05})
        finished = asyncio.Event()

        class Slow:
            redaction = RedactionPolicy()
            async def __call__(self, scope, receive, send):
                await asyncio.sleep(0.2)
                finished.set()
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"{}"})
        host.app.app = Slow()
        status, body = await call(host.app, "GET", "/v1/processes/x", MODULO)
        assert status == 503 and body["error"]["code"] == "request_timeout"
        assert not finished.is_set() and len(host.app.background) == 1
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0)
        assert host.app.background == set() and host.app.limiter.rejections["request_timeout"] == 1
        host.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("limits,field", [
    ({"requests_per_minute": 0}, "limits.requests_per_minute"),
    ({"max_streams": 1.5}, "limits.max_streams"),
    ({"request_timeout_seconds": 0}, "limits.request_timeout_seconds"),
    ({"exempt_admin": "yes"}, "limits.exempt_admin"),
    ({"burst": 3}, "limits"),
])
def test_limits_validation(tmp_path, limits, field):
    with pytest.raises(HostConfigError) as error:
        parse_host_config(data(tmp_path, limits=limits))
    assert error.value.field == field


# --- #245: reload without restart ---------------------------------------------------------

def test_token_rotation_and_rejected_reloads(tmp_path):
    async def exercise():
        host = build_host(parse_host_config(data(tmp_path, modulo=(MODULO, NEW))), noesis_transport=Noesis())
        assert (await call(host.app, "GET", "/v1/processes/x", MODULO))[0] == 404
        assert (await call(host.app, "GET", "/v1/processes/x", NEW))[0] == 404
        assert host.reload(parse_host_config(data(tmp_path, modulo=(NEW,)))) == ["clients"]
        assert (await call(host.app, "GET", "/v1/processes/x", MODULO))[0] == 401
        assert (await call(host.app, "GET", "/v1/processes/x", NEW))[0] == 404
        # Changes a live host cannot apply are refused and leave everything as it was.
        restart = data(tmp_path, modulo=(MODULO,))
        restart["server"] = {"port": 9000}
        with pytest.raises(HostConfigError, match="restart"):
            host.reload(parse_host_config(restart))
        assert (await call(host.app, "GET", "/v1/processes/x", NEW))[0] == 404
        assert (await call(host.app, "GET", "/v1/processes/x", MODULO))[0] == 401
        host.close()
    asyncio.run(exercise())


def test_signal_reload_reads_the_file_and_keeps_config_when_invalid(tmp_path):
    def write(modulo):
        body = "\n".join([f'data_dir = "{tmp_path / "data"}"', "", "[[clients]]", 'id = "modulo"',
                          f"token_sha256 = {[token_digest(t) for t in modulo]!r}".replace("'", '"'),
                          'roles = ["read"]', ""])
        path.write_text(body)
    path = tmp_path / "host.toml"
    write((MODULO,))

    async def exercise():
        from praxis.host.config import load_host_config
        host = build_host(load_host_config(path), config_path=path)
        messages = asyncio.Queue()
        sent = []
        async def receive():
            return await messages.get()
        async def send(message):
            sent.append(message["type"])
        lifespan = asyncio.create_task(host.app({"type": "lifespan"}, receive, send))
        await messages.put({"type": "lifespan.startup"})
        while not sent:
            await asyncio.sleep(0.01)
        assert host.app._sighup
        write((NEW,))
        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.sleep(0.05)
        assert (await call(host.app, "GET", "/v1/processes/x", NEW))[0] == 404
        assert (await call(host.app, "GET", "/v1/processes/x", MODULO))[0] == 401
        path.write_text("clients = [")  # invalid TOML: the running config stays
        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.sleep(0.05)
        assert (await call(host.app, "GET", "/v1/processes/x", NEW))[0] == 404
        await messages.put({"type": "lifespan.shutdown"})
        await lifespan
        assert not host.app._sighup
        host.close()
    asyncio.run(exercise())


def test_noesis_credentials_and_domains_reload(tmp_path):
    token_file = tmp_path / "noesis.token"
    token_file.write_text("nn_first_" + "a" * 24)
    noesis = {"base_url": "https://noesis.example", "token_file": str(token_file)}
    host = build_host(parse_host_config(data(tmp_path, noesis=noesis)))
    before = host.noesis_transport.current
    assert before.headers["Authorization"].endswith("a" * 24)
    token_file.write_text("nn_second_" + "b" * 24)
    changes = host.reload(parse_host_config(data(tmp_path, noesis={**noesis, "context_domains": ["docs"]})))
    assert changes == ["noesis credentials", "context domains"]
    assert host.noesis_transport.current is not before
    assert host.noesis_transport.current.headers["Authorization"] == "Bearer nn_second_" + "b" * 24
    assert host.context_providers["noesis"].domains == frozenset({"docs"})
    assert host.redaction.clean("leak nn_second_" + "b" * 24) == "leak [REDACTED]"
    token_file.write_text("")  # a missing credential is refused before anything changes
    with pytest.raises(ValueError, match="credential"):
        host.reload(parse_host_config(data(tmp_path, noesis=noesis)))
    assert host.context_providers["noesis"].domains == frozenset({"docs"})
    host.close()


def openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI required to mint test certificates")
def test_server_certificate_reloads_into_live_context(tmp_path):
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=ca",
            "-keyout", "ca.key", "-out", "ca.pem", cwd=tmp_path)
    (tmp_path / "san.cnf").write_text("subjectAltName=IP:127.0.0.1\n")
    for name in ("old", "new"):
        openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={name}", "-keyout", f"{name}.key",
                "-out", f"{name}.csr", cwd=tmp_path)
        openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
                "-days", "1", "-out", f"{name}.pem", "-extfile", "san.cnf", cwd=tmp_path)

    def tls(name):
        return {"host": "127.0.0.1", "tls_certfile": str(tmp_path / f"{name}.pem"),
                "tls_keyfile": str(tmp_path / f"{name}.key")}
    config = data(tmp_path)
    config["server"] = tls("old")
    host = build_host(parse_host_config(config), noesis_transport=Noesis())
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(tmp_path / "old.pem", tmp_path / "old.key")
    host.server_ssl = server_context
    listener = socket.create_server(("127.0.0.1", 0))

    def serve_once():
        connection, _ = listener.accept()
        with server_context.wrap_socket(connection, server_side=True):
            pass

    def peer_name():
        thread = threading.Thread(target=serve_once)
        thread.start()
        client = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
        with socket.create_connection(listener.getsockname()) as raw:
            with client.wrap_socket(raw, server_hostname="127.0.0.1") as wrapped:
                name = dict(item[0] for item in wrapped.getpeercert()["subject"])["commonName"]
        thread.join()
        return name
    try:
        assert peer_name() == "old"
        config["server"] = tls("new")
        assert host.reload(parse_host_config(config)) == ["server certificate"]
        assert peer_name() == "new"
    finally:
        listener.close()
        host.close()
