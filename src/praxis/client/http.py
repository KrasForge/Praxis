"""Bounded stdlib HTTP/SSE client with explicit connection errors and cursor resume."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from praxis.client import ClientAPIError, Response
from praxis.kernel.parsing import load_object
from praxis.transport.http import HTTPTransport, NoRedirect, TransportError


class ClientHTTPTransport:
    def __init__(self, base_url: str, *, headers: dict[str, str] | None = None,
                 timeout: float = 15, max_bytes: int = 1048576):
        config = HTTPTransport(base_url, headers=headers, timeout=timeout, max_bytes=max_bytes)
        self.base_url, self.headers = config.base_url, config.headers
        self.timeout, self.max_bytes = config.timeout, config.max_bytes

    async def request(self, method: str, path: str, body: dict[str, Any] | None = None,
                      headers: dict[str, str] | None = None) -> Response:
        return await asyncio.to_thread(self._client_request, method, path, body, headers)

    def _open(self, method: str, path: str, body: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise TransportError("invalid_transport_path")
        request = Request(self.base_url + path, method=method,
            data=None if body is None else json.dumps(body, allow_nan=False).encode(),
            headers={**self.headers, "Content-Type": "application/json", **(headers or {})})
        try:
            return build_opener(NoRedirect()).open(request, timeout=self.timeout)
        except HTTPError as exc:
            return exc
        except (URLError, TimeoutError, OSError):
            raise TransportError("transport_unavailable") from None

    def _client_request(self, method: str, path: str, body: dict[str, Any] | None,
                        headers: dict[str, str] | None) -> Response:
        try:
            with self._open(method, path, body, headers) as response:
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise TransportError("response_too_large")
                return Response(response.status, load_object(raw.decode()))
        except (TransportError, ClientAPIError):
            raise
        except (ValueError, UnicodeError):
            raise TransportError("invalid_response") from None
        except (TimeoutError, OSError):
            raise TransportError("transport_unavailable") from None

    async def stream(self, path: str) -> AsyncIterator[dict[str, Any]]:
        response = await asyncio.to_thread(self._open, "GET", path, None, {"Accept": "text/event-stream"})
        try:
            if response.status >= 400:
                raise ClientAPIError(response.status, "event_stream_rejected")
            if not response.headers.get("Content-Type", "").startswith("text/event-stream"):
                raise TransportError("invalid_event_stream")
            frame: list[str] = []
            size = 0
            while True:
                line = await asyncio.to_thread(response.readline, self.max_bytes + 1)
                if not line:
                    if frame:
                        raise TransportError("truncated_event_stream")
                    return
                size += len(line)
                if size > self.max_bytes:
                    raise TransportError("event_too_large")
                text = line.decode().rstrip("\r\n")
                if not text:
                    if frame:
                        yield load_object("\n".join(frame))
                    frame, size = [], 0
                elif text.startswith("data:"):
                    frame.append(text[5:].lstrip(" "))
        except (TransportError, ClientAPIError):
            raise
        except (ValueError, UnicodeError):
            raise TransportError("invalid_event_stream") from None
        except (TimeoutError, OSError):
            raise TransportError("transport_unavailable") from None
        finally:
            response.close()
