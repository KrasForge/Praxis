"""Production host wiring Praxis to Modulo (HTTP clients) and Noesis (knowledge).

``build_host`` owns every host decision the kernel deliberately leaves out: the executor
registry, execution defaults, the Noesis transport and its TLS material, the publication
trigger, and client authentication. ``create_app`` is the ASGI factory for servers.
"""

import asyncio
import json
import os
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from praxis.api.asgi import Application, Receive, Send
from praxis.api.auth import Actor
from praxis.api.service import APIError, ControlPlane
from praxis.executors.fake import FakeExecutor
from praxis.executors.local import LocalProcessExecutor
from praxis.executors.protocol import Executor
from praxis.host.auth import HostSecurity
from praxis.host.config import HostConfig, NoesisConfig, load_host_config
from praxis.host.publisher import TARGET, Publisher, PublicationWorker, result_payload
from praxis.kernel.authority import Authority
from praxis.kernel.effect_service import EffectService
from praxis.kernel.lifecycle import TERMINAL
from praxis.kernel.runtime import Kernel
from praxis.knowledge.context import ContextProvider
from praxis.knowledge.noesis import NoesisContextProvider
from praxis.knowledge.policy import PublicationService
from praxis.knowledge.publication import NoesisExporter
from praxis.observability.redaction import RedactionPolicy
from praxis.storage.sqlite import SQLiteStore
from praxis.transport.http import HTTPTransport, JSONTransport
from praxis.workspaces.local import LocalWorkspaces

PUBLISH_METADATA = "praxis_host"
PUBLICATION_STATUS = {"accepted": 200, "duplicate": 200, "rejected": 409, "unavailable": 503}


class HostControlPlane(ControlPlane):
    """Submission also records publication intent for clients allowed to publish."""

    def __init__(self, kernel: Kernel, effects: EffectService, config: HostConfig,
                 publisher: Publisher | None):
        super().__init__(kernel, effects)
        self.config = config
        self.publisher = publisher

    def submit(self, data: dict[str, Any], idempotency_key: str | None = None, *,
               actor: str = "kernel") -> dict[str, Any]:
        response = super().submit(data, idempotency_key, actor=actor)
        if response["duplicate"] or self.publisher is None or self.config.noesis is None:
            return response
        if self.config.noesis.publication != "auto":
            return response
        client = self.config.client(actor.split("/", 1)[0])
        requested = self.kernel.processes[response["process_id"]].spec.metadata.get(PUBLISH_METADATA, {})
        # Metadata only states intent; the grant comes from the authenticated client's role.
        if (client is not None and client.roles & {"publish", "admin"}
                and isinstance(requested, dict) and requested.get("publish") is True):
            self.publisher.grant(response["process_id"])
        return response


class HostApplication:
    """Adds lifespan-managed publication and the publication route to the control plane."""

    def __init__(self, app: Application, security: HostSecurity, publisher: Publisher | None,
                 worker: PublicationWorker | None):
        self.app = app
        self.security = security
        self.publisher = publisher
        self.worker = worker
        self.worker_task: asyncio.Task[None] | None = None

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.lifespan(receive, send)
            return
        parts = scope.get("path", "").strip("/").split("/") if scope["type"] == "http" else []
        if len(parts) == 4 and parts[:2] == ["v1", "processes"] and parts[3] == "publication":
            await self.publication(scope, parts[2], receive, send)
            return
        await self.app(scope, receive, send)

    async def lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                if self.worker is not None:
                    self.worker_task = asyncio.create_task(self.worker.run())
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.shutdown()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def shutdown(self) -> None:
        if self.worker is not None and self.worker_task is not None:
            self.worker.stop()
            await asyncio.gather(self.worker_task, return_exceptions=True)
            self.worker_task = None

    async def publication(self, scope: dict[str, Any], process_id: str, receive: Receive, send: Send) -> None:
        try:
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            try:
                actor = self.security.authenticate(headers)
            except Exception:
                actor = None
            if not isinstance(actor, Actor):
                raise APIError(401, "authentication_required")
            if not self.security.authorize(actor, "publication", process_id):
                raise APIError(403, "action_denied")
            if self.publisher is None:
                raise APIError(404, "route_not_found")
            if scope["method"] != "POST":
                raise APIError(405, "method_not_allowed")
            process = self.publisher.kernel.processes.get(process_id)
            if process is None:
                raise APIError(404, "process_not_found")
            if process.state not in TERMINAL:
                raise APIError(409, "process_not_terminal")
            receipt = await self.publisher.request(process_id, actor.identity)
            status, response = PUBLICATION_STATUS[receipt.status], {
                "process_id": process_id, "attempt_id": process.attempt_id, "target": TARGET,
                "publication": result_payload(receipt)}
        except APIError as exc:
            status, response = exc.status, exc.to_dict()
        raw = json.dumps(self.app.redaction.clean(response), allow_nan=False).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]})
        await send({"type": "http.response.body", "body": raw})


@dataclass
class Host:
    config: HostConfig
    store: SQLiteStore
    kernel: Kernel
    authority: Authority
    security: HostSecurity
    app: HostApplication
    publisher: Publisher | None = None
    worker: PublicationWorker | None = None
    context_providers: dict[str, ContextProvider] = field(default_factory=dict)

    def close(self) -> None:
        self.store.close()


def noesis_token(config: NoesisConfig, environ: Mapping[str, str]) -> str:
    if config.token_env is not None:
        token = environ.get(config.token_env, "")
    else:
        assert config.token_file is not None
        token = Path(config.token_file).read_text().strip()
    if not token:
        raise ValueError("noesis credential is empty or missing")
    return token


def noesis_ssl_context(config: NoesisConfig) -> ssl.SSLContext | None:
    if not config.base_url.startswith("https://"):
        return None
    context = ssl.create_default_context(cafile=config.ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if config.client_certfile is not None:
        context.load_cert_chain(config.client_certfile, config.client_keyfile)
    return context


def build_host(config: HostConfig, *, environ: Mapping[str, str] | None = None,
               executors: Mapping[str, Executor] | None = None,
               noesis_transport: JSONTransport | None = None) -> Host:
    """Assemble a recovered kernel and its ASGI app. Extra ``executors`` (agent adapters
    inside isolated workers) are registered alongside the configured built-ins."""
    env = os.environ if environ is None else environ
    root = Path(config.data_dir).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspaces = LocalWorkspaces(root / "workspaces")
    registry: dict[str, Executor] = {}
    if "fake" in config.executors:
        registry["fake"] = FakeExecutor()
    if "local" in config.executors:
        registry["local"] = LocalProcessExecutor(workspaces)
    for name, executor in (executors or {}).items():
        if name in registry:
            raise ValueError(f"executor {name!r} is already configured")
        registry[name] = executor
    authority = Authority(execution_defaults=frozenset(registry))
    store = SQLiteStore(root / "runtime.db")
    redaction = RedactionPolicy()
    providers: dict[str, ContextProvider] = {}
    transport = noesis_transport
    if config.noesis is not None and transport is None:
        token = noesis_token(config.noesis, env)
        redaction.register(token)
        # Noesis accepts its API keys as bearer credentials (src/api/auth/api_key_middleware.py).
        transport = HTTPTransport(config.noesis.base_url, headers={"Authorization": "Bearer " + token},
                                  timeout=config.noesis.timeout_seconds,
                                  ssl_context=noesis_ssl_context(config.noesis))
    if config.noesis is not None and transport is not None:
        providers["noesis"] = NoesisContextProvider(transport)
    kernel = Kernel(store, workspaces, registry, authority=authority, context_providers=providers)
    kernel.recover_records()
    publisher = worker = None
    if config.noesis is not None and transport is not None and config.noesis.publication != "off":
        exporter = NoesisExporter(transport, config.noesis.ingest_path)
        publisher = Publisher(kernel, store, authority, PublicationService(store, authority, exporter, target=TARGET))
        if config.noesis.publication == "auto":
            worker = PublicationWorker(publisher)
    security = HostSecurity(config, kernel)
    control = HostControlPlane(kernel, EffectService(store, authority), config, publisher)
    app = HostApplication(Application(control, security.hooks(), redaction), security, publisher, worker)
    return Host(config, store, kernel, authority, security, app, publisher, worker, providers)


def create_app() -> HostApplication:
    """ASGI factory: ``uvicorn praxis.host.app:create_app --factory`` with PRAXIS_HOST_CONFIG set."""
    path = os.environ.get("PRAXIS_HOST_CONFIG")
    if not path:
        raise ValueError("PRAXIS_HOST_CONFIG must name the host TOML file")
    return build_host(load_host_config(Path(path))).app
