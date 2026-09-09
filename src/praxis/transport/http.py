"""JSON HTTP transport with bounded reads, deadlines and no redirects."""

import asyncio
import json
import math
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class TransportError(OSError):
    def __init__(self, code: str, status: int | None = None):
        self.code = code
        self.status = status
        super().__init__(code)


class JSONTransport(Protocol):
    async def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise TransportError("redirect_rejected", code)


class HTTPTransport:
    def __init__(self, base_url: str, *, headers: dict[str, str] | None = None,
                 timeout: float = 15, max_bytes: int = 1048576):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid transport origin")
        if not math.isfinite(timeout) or timeout <= 0 or type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("positive transport bounds required")
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.max_bytes = max_bytes

    async def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//") or urlsplit(path).scheme:
            raise TransportError("invalid_transport_path")
        return await asyncio.to_thread(self._request, method, path, body)

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        try:
            request = Request(self.base_url + path, method=method,
                              data=None if body is None else json.dumps(body, allow_nan=False).encode(),
                              headers={**self.headers, "Content-Type": "application/json", "Accept": "application/json"})
            with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
                raw = response.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise TransportError("response_too_large")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("object response required")
            return data
        except HTTPError as exc:
            raise TransportError("http_error", exc.code) from None
        except (URLError, TimeoutError) as exc:
            raise TransportError("transport_unavailable") from exc
        except (ValueError, UnicodeError):
            raise TransportError("invalid_response") from None
