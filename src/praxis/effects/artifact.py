"""``artifact_publish`` effects: upload verified snapshot bytes with HTTP PUT (ADR 0001).

The kernel binds each artifact to ``<snapshot_id>:<path>`` of the verified snapshot and
records its SHA-256. The adapter resolves those bytes from the snapshot store, checks the
digest again, and uploads them to ``<prefix>/<idempotency_key>``. Because the object key
is the idempotency key, ``lookup`` is a read of that object with a digest check, and a
retry can never create a second copy under another name.
"""

import asyncio
import hashlib
import ssl
from collections.abc import Callable
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import BaseHandler, HTTPSHandler, Request, build_opener

from praxis.kernel.effect_service import EffectReceipt
from praxis.kernel.effects import Effect, EffectKind
from praxis.transport.http import NoRedirect, TransportError
from praxis.validators.policy import VerificationReport
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceError


class ArtifactSource(Protocol):
    def read(self, effect: Effect) -> bytes: ...


class SnapshotArtifacts:
    """Reads an artifact only from the verified snapshot of the effect's process."""

    def __init__(self, workspaces: LocalWorkspaces, verification: Callable[[str], VerificationReport | None]):
        self.workspaces = workspaces
        self.verification = verification

    def read(self, effect: Effect) -> bytes:
        snapshot_id, _, path = effect.payload["artifact_ref"].partition(":")
        report = self.verification(effect.process_id)
        if report is None or report.approved is not True or report.snapshot_id != snapshot_id or not path:
            raise PermissionError("artifact_not_verified")
        try:
            snapshot = self.workspaces.load_snapshot(effect.process_id, snapshot_id)
        except WorkspaceError:
            raise PermissionError("artifact_snapshot_missing") from None
        digest = dict(snapshot.files).get(path)
        if digest is None:
            raise PermissionError("artifact_not_in_snapshot")
        blob = (self.workspaces.root / "blobs" / digest).read_bytes()
        if hashlib.sha256(blob).hexdigest() != digest:
            raise PermissionError("artifact_blob_corrupt")
        content = blob.split(b"\n", 1)[1]
        expected = effect.payload.get("metadata", {}).get("sha256")
        if hashlib.sha256(content).hexdigest() != expected:
            raise PermissionError("artifact_digest_mismatch")
        return content


class HTTPObjectStore:
    """Byte-level PUT/GET with the transport's bounds: no redirects, deadlines, size cap."""

    def __init__(self, base_url: str, *, headers: dict[str, str] | None = None, timeout: float = 30,
                 max_bytes: int = 104857600, ssl_context: ssl.SSLContext | None = None):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.query:
            raise ValueError("invalid object store origin")
        if ssl_context is not None and parsed.scheme != "https":
            raise ValueError("TLS context requires an https origin")
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.ssl_context = ssl_context

    async def put(self, path: str, content: bytes, media_type: str) -> None:
        await asyncio.to_thread(self._call, "PUT", path, content, media_type)

    async def get(self, path: str) -> bytes | None:
        return await asyncio.to_thread(self._call, "GET", path, None, None)

    def _call(self, method: str, path: str, content: bytes | None, media_type: str | None) -> bytes | None:
        headers = dict(self.headers)
        if media_type is not None:
            headers["Content-Type"] = media_type
        handlers: list[BaseHandler] = [NoRedirect()]
        if self.ssl_context is not None:
            handlers.append(HTTPSHandler(context=self.ssl_context))
        try:
            with build_opener(*handlers).open(Request(self.base_url + path, data=content, method=method,
                                                      headers=headers), timeout=self.timeout) as response:
                raw: bytes = response.read(self.max_bytes + 1)
        except HTTPError as exc:
            if method == "GET" and exc.code == 404:
                return None
            raise TransportError("http_error", exc.code) from None
        except (URLError, TimeoutError, ConnectionError, ssl.SSLError) as exc:
            raise TransportError("transport_unavailable") from exc
        if len(raw) > self.max_bytes:
            raise TransportError("response_too_large")
        return raw


class ObjectStore(Protocol):
    async def put(self, path: str, content: bytes, media_type: str) -> None: ...
    async def get(self, path: str) -> bytes | None: ...


class ArtifactPublishAdapter:
    kind = EffectKind.ARTIFACT_PUBLISH

    def __init__(self, store: ObjectStore, source: ArtifactSource,
                 expected: Callable[[str], str | None], *, prefix: str = "/artifacts"):
        """``expected`` maps an idempotency key to the digest the kernel recorded for it."""
        if not prefix.startswith("/") or prefix.startswith("//"):
            raise ValueError("artifact prefix must be absolute")
        self.store = store
        self.source = source
        self.expected = expected
        self.prefix = prefix.rstrip("/")

    def _path(self, idempotency_key: str) -> str:
        return f"{self.prefix}/{quote(idempotency_key, safe='')}"

    async def apply(self, effect: Effect) -> EffectReceipt:
        try:
            content = self.source.read(effect)
        except PermissionError as exc:
            return EffectReceipt(False, str(exc))
        digest = hashlib.sha256(content).hexdigest()
        try:
            await self.store.put(self._path(effect.idempotency_key), content, effect.payload["media_type"])
        except TransportError as exc:
            if exc.status is not None and 400 <= exc.status < 500 and exc.status not in (408, 429):
                return EffectReceipt(False, f"rejected_{exc.status}")
            raise
        return EffectReceipt(True, "published", digest)

    async def lookup(self, idempotency_key: str) -> EffectReceipt | None:
        try:
            content = await self.store.get(self._path(idempotency_key))
        except TransportError:
            return None
        if content is None:
            return EffectReceipt(False, "not_published")
        digest = hashlib.sha256(content).hexdigest()
        if digest != self.expected(idempotency_key):
            # Something else is stored under this key; do not claim it as ours.
            return None
        return EffectReceipt(True, "published", digest)
