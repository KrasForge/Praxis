"""Typed asynchronous v1 client. Process failures are result data, not transport errors."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import quote

from praxis.kernel.events import Event
from praxis.kernel.lifecycle import State
from praxis.kernel.results import ProcessResult
from praxis.kernel.spec import ProcessSpec
from praxis.storage.protocol import StoredEvent
from praxis.transport.http import TransportError


class ClientAPIError(ValueError):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


@dataclass(frozen=True)
class Response:
    status: int
    body: dict[str, Any]


class ClientTransport(Protocol):
    async def request(self, method: str, path: str, body: dict[str, Any] | None = None,
                      headers: dict[str, str] | None = None) -> Response: ...
    def stream(self, path: str) -> AsyncIterator[dict[str, Any]]: ...


@dataclass(frozen=True)
class Submission:
    process_id: str
    state: State
    duplicate: bool


@dataclass(frozen=True)
class ProcessView:
    process_id: str
    attempt_id: str
    state: State
    result: ProcessResult | None
    data: dict[str, Any]


@dataclass(frozen=True)
class ControlReceipt:
    process_id: str
    attempt_id: str
    control: dict[str, Any]


def process_path(identity: str) -> str:
    if not isinstance(identity, str) or not identity or "/" in identity or identity in {".", ".."}:
        raise ValueError("invalid_process_identity")
    return "/v1/processes/" + quote(identity, safe="")


class Client:
    def __init__(self, transport: ClientTransport):
        self.transport = transport

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                       headers: dict[str, str] | None = None) -> dict[str, Any]:
        response = await self.transport.request(method, path, body, headers)
        if response.status >= 400:
            raise ClientAPIError(response.status, response.body.get("error", {}).get("code", "api_error"))
        return response.body

    async def submit(self, spec: ProcessSpec, *, idempotency_key: str | None = None) -> Submission:
        data = await self._request("POST", "/v1/processes", json.loads(spec.to_json()),
                                   None if idempotency_key is None else {"Idempotency-Key": idempotency_key})
        try:
            if not isinstance(data["process_id"], str) or type(data["duplicate"]) is not bool:
                raise ValueError()
            return Submission(data["process_id"], State(data["state"]), data["duplicate"])
        except (ValueError, KeyError, TypeError):
            raise TransportError("invalid_api_response") from None

    async def inspect(self, process_id: str) -> ProcessView:
        data = await self._request("GET", process_path(process_id))
        try:
            if data["process_id"] != process_id or not isinstance(data["attempt_id"], str):
                raise ValueError()
            result = None if data["result"] is None else ProcessResult.from_json(json.dumps(data["result"]))
            if result is not None and (result.process_id, result.attempt_id) != (process_id, data["attempt_id"]):
                raise ValueError()
            return ProcessView(process_id, data["attempt_id"], State(data["state"]), result, data)
        except (ValueError, KeyError, TypeError):
            raise TransportError("invalid_api_response") from None

    async def control(self, process_id: str, attempt_id: str,
                      operation: Literal["suspend", "resume", "signal", "cancel", "retry"],
                      *, signal: str | None = None, policy: Literal["self", "tree"] = "self",
                      retry: dict[str, Any] | None = None) -> ControlReceipt:
        data = await self._request("POST", process_path(process_id) + "/control", {
            "attempt_id": attempt_id, "operation": operation, "signal": signal, "policy": policy, "retry": retry or {}})
        try:
            if data["process_id"] != process_id or not isinstance(data["attempt_id"], str) or not isinstance(data["control"], dict):
                raise ValueError()
            return ControlReceipt(process_id, data["attempt_id"], data["control"])
        except (ValueError, KeyError, TypeError):
            raise TransportError("invalid_api_response") from None

    async def events(self, process_id: str, *, after: int = 0, tree: bool = False) -> AsyncIterator[StoredEvent]:
        if type(after) is not int or after < 0:
            raise ValueError("invalid_event_cursor")
        path = process_path(process_id) + f"/events?after={after}&tree={str(tree).lower()}"
        async for data in self.transport.stream(path):
            try:
                cursor = data["cursor"]
                event = Event.from_json(json.dumps(data["event"]))
                if type(cursor) is not int or cursor <= after or (not tree and event.process_id != process_id):
                    raise ValueError()
                after = cursor
            except (ValueError, KeyError, TypeError):
                raise TransportError("invalid_event_stream") from None
            yield StoredEvent(cursor, event)
