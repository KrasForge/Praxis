"""Authenticated ASGI endpoint for worker dispatch and event/result polling."""

import json
from typing import Any

from praxis.api.asgi import Receive, Send
from praxis.remote.node import WorkerNode
from praxis.remote.workers import WorkerError


class WorkerApplication:
    def __init__(self, node: WorkerNode):
        self.node = node

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        try:
            if scope["method"] != "POST" or scope["path"] != "/v1/worker":
                raise WorkerError("worker_route_not_found")
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 16 * 1024 * 1024:
                    raise WorkerError("worker_request_too_large")
                if not message.get("more_body", False):
                    break
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            credential = headers.get(b"authorization", b"").decode()
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("object required")
            response = await self.node.rpc(data, credential)
            status = 200
        except WorkerError as exc:
            status = 403 if "unauthorized" in exc.code else 409
            response = {"error": {"code": exc.code}}
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            status, response = 422, {"error": {"code": "invalid_worker_request"}}
        raw = json.dumps(response).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": raw})
