"""Minimal in-process ASGI caller for acceptance checks against a locally built host."""

import json
from typing import Any


async def call(app: Any, method: str, path: str, token: str, data: dict[str, Any] | None = None,
               headers: tuple[tuple[bytes, bytes], ...] = ()) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": json.dumps(data or {}).encode()}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    raw = [(b"authorization", b"Bearer " + token.encode()), *headers]
    await app({"type": "http", "method": method, "path": path, "headers": raw}, receive, send)
    return messages[0]["status"], json.loads(messages[1]["body"])
