import asyncio
import json
from urllib.parse import urlsplit

import pytest

from praxis.client import Client, ClientAPIError, Response
from praxis.kernel.spec import ProcessSpec
from praxis.transport.http import TransportError
from test_api import make_kernel, trusted_app
from praxis.api.service import ControlPlane


class ASGITransport:
    def __init__(self, app):
        self.app = app

    async def messages(self, method, path, body=None, headers=None):
        incoming = asyncio.Queue()
        await incoming.put({"type": "http.request", "body": json.dumps(body or {}).encode()})
        sent = []
        async def send(message):
            sent.append(message)
        url = urlsplit(path)
        await self.app({"type": "http", "method": method, "path": url.path, "query_string": url.query.encode(),
                       "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}, incoming.get, send)
        return sent

    async def request(self, method, path, body=None, headers=None):
        messages = await self.messages(method, path, body, headers)
        return Response(messages[0]["status"], json.loads(messages[1]["body"]))

    async def stream(self, path):
        messages = await self.messages("GET", path)
        for message in messages[1:]:
            if message.get("body"):
                yield json.loads(message["body"].decode().split("data: ", 1)[1])


def test_sdk_against_api(tmp_path):
    async def exercise():
        kernel = make_kernel(tmp_path)
        client = Client(ASGITransport(trusted_app(ControlPlane(kernel))))
        spec = ProcessSpec("fail verification", "fake", contract={"required_outputs": ["missing"]})
        submitted = await client.submit(spec, idempotency_key="one")
        assert (await client.submit(spec, idempotency_key="one")).duplicate
        await kernel.tasks[submitted.process_id]
        view = await client.inspect(submitted.process_id)
        assert view.result.error.code == "contract_rejected"
        events = [e async for e in client.events(submitted.process_id)]
        resumed = [e async for e in client.events(submitted.process_id, after=events[0].cursor)]
        assert resumed == events[1:]
        control = await client.control(view.process_id, view.attempt_id, "cancel")
        assert control.process_id == view.process_id
        with pytest.raises(ClientAPIError) as error:
            await client.control(view.process_id, "stale", "cancel")
        assert error.value.status == 409
        with pytest.raises(ClientAPIError) as error:
            await client.inspect("missing")
        assert error.value.status == 404
    asyncio.run(exercise())


def test_transport_failure_is_not_process_failure():
    class Offline:
        async def request(self, *args):
            raise TransportError("offline")
    with pytest.raises(TransportError):
        asyncio.run(Client(Offline()).inspect("p"))


def test_http_and_sse_wire_transport():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from praxis.client.http import ClientHTTPTransport
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if self.path == "/events" else "application/json")
            self.end_headers()
            self.wfile.write(b'data: {"cursor":1}\n\n' if self.path == "/events" else b'{"ok":true}')
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def exercise():
        transport = ClientHTTPTransport(f"http://127.0.0.1:{server.server_port}")
        assert (await transport.request("GET", "/")).body == {"ok": True}
        assert [e async for e in transport.stream("/events")] == [{"cursor": 1}]
    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
