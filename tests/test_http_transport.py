import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from praxis.transport.http import HTTPTransport, TransportError


def test_bounded_http_transport():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/ok")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}' if self.path == "/ok" else b"x" * 200)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def exercise():
        transport = HTTPTransport(f"http://127.0.0.1:{server.server_port}", max_bytes=100)
        assert await transport.request("GET", "/ok") == {"ok": True}
        with pytest.raises(TransportError, match="response_too_large"):
            await transport.request("GET", "/large")
        with pytest.raises(TransportError, match="redirect_rejected"):
            await transport.request("GET", "/redirect")
    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
