"""Small ASGI JSON API, compatible with standard ASGI deployment servers."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from praxis.api.service import APIError, ControlPlane

Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class Application:
    def __init__(self, service: ControlPlane):
        self.service = service

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        try:
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 1048576:
                    raise APIError(413, "request_too_large")
                if not message.get("more_body", False):
                    break
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            data = json.loads(body) if body else {}
            if not isinstance(data, dict):
                raise APIError(422, "object_request_required")
            key = headers.get(b"idempotency-key")
            status, response = await self.route(scope["method"], scope["path"], data,
                                                None if key is None else key.decode())
        except APIError as exc:
            status, response = exc.status, exc.to_dict()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            status, response = 422, APIError(422, "invalid_request").to_dict()
        raw = json.dumps(response, allow_nan=False).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]})
        await send({"type": "http.response.body", "body": raw})

    async def route(self, method: str, path: str, data: dict[str, Any], key: str | None) -> tuple[int, dict[str, Any]]:
        if method == "POST" and path == "/v1/processes":
            result = self.service.submit(data, key)
            return (200 if result["duplicate"] else 202), result
        parts = path.strip("/").split("/")
        if method == "GET" and len(parts) in (3, 4) and parts[:2] == ["v1", "processes"]:
            if len(parts) == 3:
                return 200, self.service.inspect(parts[2])
            if parts[3] == "tree":
                return 200, self.service.inspect_tree(parts[2])
        raise APIError(404, "route_not_found")
