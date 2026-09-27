"""``message_send`` effects over an HTTP webhook, with a status lookup (ADR 0001).

A message cannot be taken back, so the receiver must deduplicate by the idempotency key
sent in the body and answer ``GET <status_path>/<key>``. A lost response leaves the
effect ``applying``; ``lookup`` then resolves it. The adapter never re-sends by itself.
"""

from typing import Any
from urllib.parse import quote

from praxis.kernel.effect_service import EffectReceipt
from praxis.kernel.effects import Effect, EffectKind
from praxis.transport.http import JSONTransport, TransportError


class WebhookMessageAdapter:
    kind = EffectKind.MESSAGE_SEND

    def __init__(self, transport: JSONTransport, *, send_path: str = "/messages",
                 status_path: str = "/messages"):
        for path in (send_path, status_path):
            if not path.startswith("/") or path.startswith("//"):
                raise ValueError("webhook paths must be absolute")
        self.transport = transport
        self.send_path = send_path
        self.status_path = status_path.rstrip("/")

    async def apply(self, effect: Effect) -> EffectReceipt:
        body: dict[str, Any] = {"destination": effect.target, "text": effect.payload["text"],
                                "idempotency_key": effect.idempotency_key}
        try:
            response = await self.transport.request("POST", self.send_path, body)
        except TransportError as exc:
            if exc.status is not None and 400 <= exc.status < 500 and exc.status not in (408, 429):
                # The receiver refused the message; it was not delivered.
                return EffectReceipt(False, f"rejected_{exc.status}")
            raise
        return EffectReceipt(True, "delivered", _identity(response))

    async def lookup(self, idempotency_key: str) -> EffectReceipt | None:
        try:
            response = await self.transport.request("GET", f"{self.status_path}/{quote(idempotency_key, safe='')}")
        except TransportError as exc:
            if exc.status == 404:
                return EffectReceipt(False, "not_delivered")
            return None
        if response.get("delivered") is True:
            return EffectReceipt(True, "delivered", _identity(response))
        return None


def _identity(response: dict[str, Any]) -> str | None:
    identity = response.get("id")
    return identity if isinstance(identity, str) and identity else None
