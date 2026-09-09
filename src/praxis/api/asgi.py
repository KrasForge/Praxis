"""Small ASGI JSON API, compatible with standard ASGI deployment servers."""

import asyncio
import json
from urllib.parse import parse_qs
from collections.abc import Awaitable, Callable
from typing import Any

from praxis.api.service import APIError, ControlPlane
from praxis.storage.protocol import StoredEvent

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
            parts = scope["path"].strip("/").split("/")
            if scope["method"] == "GET" and len(parts) == 4 and parts[:2] == ["v1", "processes"] and parts[3] == "events":
                headers = {key.lower(): value for key, value in scope.get("headers", [])}
                query = parse_qs(scope.get("query_string", b"").decode())
                after = int(query.get("after", [headers.get(b"last-event-id", b"0").decode()])[0])
                tree = query.get("tree", ["false"])[0] == "true"
                self.service.events(parts[2], tree=tree, after=after)
                await self.stream_response(parts[2], tree, after, receive, send)
                return
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
        if method == "POST" and len(parts) == 4 and parts[:2] == ["v1", "processes"] and parts[3] == "control":
            return 200, await self.service.control(parts[2], data)
        if len(parts) == 4 and parts[:2] == ["v1", "processes"]:
            if parts[3] == "approvals":
                if method == "GET":
                    return 200, self.service.pending_approvals(parts[2])
                if method == "POST":
                    return 200, self.service.resolve_approval(parts[2], data)
            if parts[3] == "interventions" and method == "POST":
                return 200, await self.service.intervene(parts[2], data)
        if method == "GET" and len(parts) in (3, 4) and parts[:2] == ["v1", "processes"]:
            if len(parts) == 3:
                return 200, self.service.inspect(parts[2])
            if parts[3] == "tree":
                return 200, self.service.inspect_tree(parts[2])
        raise APIError(404, "route_not_found")

    async def stream_response(self, process_id: str, tree: bool, after: int,
                              receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream"), (b"cache-control", b"no-cache")]})
        async def disconnected() -> None:
            while (await receive())["type"] != "http.disconnect":
                pass
        disconnect = asyncio.create_task(disconnected())
        iterator = self.service.stream(process_id, tree=tree, after=after)
        pending: asyncio.Future[StoredEvent] | None = None
        try:
            while True:
                pending = asyncio.ensure_future(anext(iterator))
                done, _ = await asyncio.wait({pending, disconnect}, return_when=asyncio.FIRST_COMPLETED)
                if disconnect in done:
                    break
                try:
                    entry = pending.result()
                except StopAsyncIteration:
                    break
                data = json.dumps({"cursor": entry.cursor, "event": json.loads(entry.event.to_json())})
                await send({"type": "http.response.body", "body": f"id: {entry.cursor}\nevent: praxis\ndata: {data}\n\n".encode(),
                            "more_body": True})
        finally:
            disconnect.cancel()
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await asyncio.gather(disconnect, return_exceptions=True)
            await iterator.aclose()
        await send({"type": "http.response.body", "body": b"", "more_body": False})
