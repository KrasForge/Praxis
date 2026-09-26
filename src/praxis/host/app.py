"""Production host wiring Praxis to Modulo (HTTP clients) and Noesis (knowledge).

``build_host`` owns every host decision the kernel deliberately leaves out: the executor
registry, execution defaults, the Noesis transport and its TLS material, the publication
trigger, and client authentication. ``create_app`` is the ASGI factory for servers.
"""

import asyncio
import fcntl
import json
import logging
import os
import signal
import ssl
from collections.abc import Awaitable, Callable, Iterable, Mapping
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
from praxis.host.config import HostConfig, HostConfigError, NoesisConfig, load_host_config
from praxis.host.effects import HostEffects, build_adapters
from praxis.host.limits import Limiter, Rejection
from praxis.host.publisher import TARGET, Publisher, PublicationWorker, result_payload
from praxis.kernel.authority import Authority
from praxis.effects import require_reconcilable
from praxis.kernel.effect_service import EffectAdapter, EffectService
from praxis.kernel.capabilities import Resource
from praxis.kernel.effects import Effect, EffectKind
from praxis.kernel.planning import PlanMaterializer, PlanningError
from praxis.storage.graphs import GraphStore
from praxis.validators.plan import AllowedGrant, PlanValidator
from praxis.kernel.lifecycle import TERMINAL
from praxis.kernel.runtime import Kernel
from praxis.knowledge.context import ContextProvider, ContextRequest, ContextResponse
from praxis.knowledge.noesis import NoesisContextProvider
from praxis.knowledge.policy import PublicationService
from praxis.knowledge.publication import NoesisExporter
from praxis.observability.redaction import RedactionPolicy
from praxis.storage.sqlite import SQLiteStore
from praxis.transport.http import HTTPTransport, JSONTransport
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.retention import RetentionPolicy, RetentionReport, sweep

logger = logging.getLogger("praxis.host")
PUBLISH_METADATA = "praxis_host"
PUBLICATION_STATUS = {"accepted": 200, "duplicate": 200, "rejected": 409, "unavailable": 503}


class HostControlPlane(ControlPlane):
    """Submission also records publication intent for clients allowed to publish."""

    def __init__(self, kernel: Kernel, effects: EffectService, config: HostConfig,
                 publisher: Publisher | None, policy: HostEffects | None = None,
                 planning: PlanMaterializer | None = None, owner: Callable[[str], str | None] | None = None):
        super().__init__(kernel, effects)
        self.config = config
        self.publisher = publisher
        self.policy = policy
        self.planning = planning
        self.owner = owner

    async def materialize_plan(self, process_id: str, actor: str) -> dict[str, Any]:
        self.inspect(process_id)
        if self.planning is None:
            raise APIError(404, "route_not_found")
        try:
            async with self.operation_locks.setdefault(process_id, asyncio.Lock()):
                owner = None if self.owner is None else self.owner(process_id)
                result = self.planning.materialize(process_id, actor, owner)
                started = await self.planning.advance(process_id)
        except PlanningError as exc:
            raise APIError(exc.status, exc.code) from None
        return {"process_id": process_id, **result, "started": started}

    def approval_expiry(self, effect: Effect) -> str | None:
        return None if self.policy is None else self.policy.expires_at(effect)

    def may_see(self, actor: str | None, effect: Effect) -> bool:
        """The owner's side sees every pending effect; an approver only those they decide."""
        owner = None if self.owner is None else self.owner(effect.process_id)
        if actor is None or owner is None:
            return True
        client = self.config.client(actor.split("/", 1)[0])
        if client is not None and "admin" in client.roles:
            return True
        if actor == owner or ("/" not in actor and owner.startswith(actor + "/")):
            return True  # the submitter, or their client acting as itself
        return self.policy is not None and self.policy.may_decide(actor, effect)

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


class SwappableTransport:
    """Noesis transport replaced as one reference on reload, so a request never mixes
    old credentials with a new TLS context."""

    def __init__(self, current: JSONTransport):
        self.current = current

    async def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.current.request(method, path, body)


class DomainScopedProvider:
    """Refuses context requests for Noesis domains outside the host allowlist."""

    def __init__(self, provider: ContextProvider, domains: frozenset[str] | None):
        self.provider = provider
        self.domains = domains

    async def query(self, request: ContextRequest) -> ContextResponse:
        if self.domains is not None and dict(request.filters).get("domain") not in self.domains:
            return ContextResponse("unavailable", reason="noesis_domain_not_allowed")
        return await self.provider.query(request)


class HostApplication:
    """Adds admission limits, publication, lifespan-managed work and reload to the control plane."""

    def __init__(self, app: Application, security: HostSecurity, publisher: Publisher | None,
                 worker: PublicationWorker | None, limiter: Limiter | None = None):
        self.app = app
        self.security = security
        self.publisher = publisher
        self.worker = worker
        self.limiter = limiter
        self.worker_task: asyncio.Task[None] | None = None
        self.reloader: Callable[[], None] | None = None
        self.retention: Callable[[], object] | None = None  # one applied sweep
        self.recovery: list[Callable[[], Awaitable[None]]] = []  # continued once the loop runs
        self.retention_interval: float | None = None
        self.retention_task: asyncio.Task[None] | None = None
        self.background: set[asyncio.Task[None]] = set()
        self._sighup = False

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.lifespan(receive, send)
        elif scope["type"] == "http" and self.limiter is not None:
            await self.limited(scope, receive, send, self.limiter)
        else:
            await self.route(scope, receive, send)

    async def route(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        parts = scope.get("path", "").strip("/").split("/") if scope["type"] == "http" else []
        if len(parts) == 4 and parts[:2] == ["v1", "processes"] and parts[3] == "publication":
            await self.publication(scope, parts[2], receive, send)
            return
        await self.app(scope, receive, send)

    def actor(self, scope: dict[str, Any]) -> Actor | None:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        try:
            actor = self.security.authenticate(headers)
        except Exception:
            return None
        return actor if isinstance(actor, Actor) else None

    async def limited(self, scope: dict[str, Any], receive: Receive, send: Send, limiter: Limiter) -> None:
        actor = self.actor(scope)
        client = None if actor is None else self.security.client_of(actor)
        if actor is None or client is None or (limiter.config.exempt_admin and "admin" in client.roles):
            # Unauthenticated requests are answered 401 by the control plane without work.
            await self.route(scope, receive, send)
            return
        parts = scope.get("path", "").strip("/").split("/")
        submit = scope["method"] == "POST" and parts == ["v1", "processes"]
        stream = scope["method"] == "GET" and len(parts) == 4 and parts[:2] == ["v1", "processes"] \
            and parts[3] == "events"
        rejection = limiter.admit(actor.identity, client.id, submit=submit)
        if rejection is None and stream:
            rejection = limiter.open_stream(actor.identity)
            if rejection is None:
                try:
                    await self.route(scope, receive, send)
                finally:
                    limiter.close_stream(actor.identity)
                return
        if rejection is not None:
            logger.warning("%s: %s", rejection.code, actor.identity)
            await self.respond(send, rejection.status, APIError(rejection.status, rejection.code).to_dict(), rejection)
            return
        timeout = limiter.config.request_timeout_seconds
        if timeout is None:
            await self.route(scope, receive, send)
            return
        await self.with_deadline(scope, receive, send, timeout, limiter)

    async def with_deadline(self, scope: dict[str, Any], receive: Receive, send: Send, timeout: float,
                            limiter: Limiter) -> None:
        started = abandoned = False

        async def guarded(message: dict[str, Any]) -> None:
            nonlocal started
            if not abandoned:
                started = True
                await send(message)
        task = asyncio.create_task(self.route(scope, receive, guarded))
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if task in done or started:
            await task
            return
        # The operation keeps running (cancelling a control or submission midway could strand
        # kernel state); only the response is abandoned. Clients re-inspect the process.
        abandoned = True
        self.background.add(task)
        task.add_done_callback(self.finished)
        limiter.rejections["request_timeout"] += 1
        await self.respond(send, 503, APIError(503, "request_timeout").to_dict())

    def finished(self, task: asyncio.Task[None]) -> None:
        self.background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("abandoned request failed", exc_info=task.exception())

    async def respond(self, send: Send, status: int, response: dict[str, Any], rejection: Rejection | None = None) -> None:
        raw = json.dumps(self.app.redaction.clean(response), allow_nan=False).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
        if rejection is not None and rejection.retry_after is not None:
            headers.append((b"retry-after", str(rejection.retry_after).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": raw})

    async def lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                for recover in self.recovery:
                    try:
                        await recover()
                    except Exception:
                        logger.exception("startup recovery step failed")
                if self.worker is not None:
                    self.worker_task = asyncio.create_task(self.worker.run())
                if self.retention is not None and self.retention_interval is not None:
                    self.retention_task = asyncio.create_task(self.sweep_periodically(
                        self.retention, self.retention_interval))
                if self.reloader is not None:
                    try:
                        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, self.reloader)
                        self._sighup = True
                    except (NotImplementedError, RuntimeError, ValueError, AttributeError):
                        logger.warning("SIGHUP reload unavailable in this server; restart to reload")
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.shutdown()
                await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    async def sweep_periodically(run: Callable[[], object], interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                run()
            except Exception:
                # A failed sweep removes nothing further; the next interval tries again.
                logger.exception("retention sweep failed")

    async def shutdown(self) -> None:
        if self.retention_task is not None:
            self.retention_task.cancel()
            await asyncio.gather(self.retention_task, return_exceptions=True)
            self.retention_task = None
        if self._sighup:
            asyncio.get_running_loop().remove_signal_handler(signal.SIGHUP)
            self._sighup = False
        if self.worker is not None and self.worker_task is not None:
            self.worker.stop()
            await asyncio.gather(self.worker_task, return_exceptions=True)
            self.worker_task = None

    async def publication(self, scope: dict[str, Any], process_id: str, receive: Receive, send: Send) -> None:
        try:
            actor = self.actor(scope)
            if actor is None:
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
        await self.respond(send, status, response)


def restart_key(config: HostConfig) -> tuple[Any, ...]:
    """Settings a running host cannot change; everything else reloads on SIGHUP."""
    noesis = config.noesis
    return (config.data_dir, config.executors, config.server.host, config.server.port, config.server.tls,
            config.server.tls_client_ca is not None, config.server.tls_terminated_upstream, config.limits,
            config.retention, config.effects,
            None if noesis is None else (noesis.base_url, noesis.ingest_path, noesis.publication))


@dataclass
class Host:
    config: HostConfig
    store: SQLiteStore
    kernel: Kernel
    authority: Authority
    security: HostSecurity
    app: HostApplication
    control: HostControlPlane
    redaction: RedactionPolicy
    publisher: Publisher | None = None
    worker: PublicationWorker | None = None
    context_providers: dict[str, ContextProvider] = field(default_factory=dict)
    noesis_transport: SwappableTransport | None = None
    rebuild_noesis: bool = True
    config_path: Path | None = None
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    server_ssl: ssl.SSLContext | None = None
    # New handshakes switch to this context, built from the current configuration.
    current_ssl: ssl.SSLContext | None = None
    # Open server transports, so a reload can close clients of a CA that was removed.
    connections: Callable[[], Iterable[asyncio.BaseTransport]] | None = None
    lock: Any = None
    live: dict[str, HostConfig] = field(default_factory=dict)

    def close(self) -> None:
        self.store.close()
        release_controller(self.lock)
        self.lock = None

    def sweep(self, *, apply: bool) -> RetentionReport:
        """Run retention against this host's data with its live journal."""
        return retention_sweep(self.config, self.store, self.kernel.workspaces, apply=apply,
                               journal=self.kernel.events)


    def reload(self, config: HostConfig | None = None) -> list[str]:
        """Apply client tokens, TLS material, Noesis credentials and context domains.

        Everything is prepared before anything changes, so an invalid file, unreadable
        certificate or missing credential leaves the running configuration untouched.
        """
        if config is None:
            if self.config_path is None:
                raise HostConfigError("reload", "host was not started from a config file")
            config = load_host_config(self.config_path)
        if restart_key(config) != restart_key(self.config):
            raise HostConfigError("reload", "data_dir, executors, bind, TLS mode, limits, retention, effect "
                                  "adapters and Noesis origin/publication changes require a restart")
        changes = []
        transport, token = None, None
        if config.noesis is not None and self.noesis_transport is not None and self.rebuild_noesis:
            token = noesis_token(config.noesis, self.environ)
            transport = noesis_http_transport(config.noesis, token)
        fresh = None
        if self.server_ssl is not None and config.server.tls_certfile is not None:
            fresh = server_ssl_context(config)  # built first: a bad file changes nothing
        if self.server_ssl is not None and fresh is not None and config.server.tls_certfile is not None:
            # A context cannot forget a CA, so every new handshake switches to a fresh one.
            self.server_ssl.load_cert_chain(config.server.tls_certfile, config.server.tls_keyfile)
            self.current_ssl = fresh
            self.server_ssl.sni_callback = self._select_context
            changes.append("server certificate")
            if config.server.tls_client_ca is not None and self.connections is not None:
                closed = close_untrusted(self.connections(), fresh)
                if closed:
                    changes.append(f"closed {closed} connection(s) from removed client CAs")
        if transport is not None and token is not None and self.noesis_transport is not None:
            self.redaction.register(token)
            self.noesis_transport.current = transport
            changes.append("noesis credentials")
        if config.clients != self.config.clients:
            changes.append("clients")
        if config.effect_policy != self.config.effect_policy:
            changes.append("effect policy")
        new_domains = None if config.noesis is None else config.noesis.context_domains
        scoped = self.context_providers.get("noesis")
        if isinstance(scoped, DomainScopedProvider) and scoped.domains != new_domains:
            scoped.domains = new_domains
            changes.append("context domains")
        if config.planning != self.config.planning:
            changes.append("planning allowlist")
        self.security.config = self.control.config = self.config = config
        self.live["config"] = config
        if self.control.policy is not None:
            self.control.policy.config = config
        logger.info("host configuration reloaded: %s", ", ".join(changes) or "no changes")
        return changes

    def _select_context(self, connection: ssl.SSLObject, name: str | None, context: ssl.SSLContext) -> None:
        if self.current_ssl is not None:
            connection.context = self.current_ssl

    def reload_from_signal(self) -> None:
        try:
            self.reload()
        except Exception as exc:
            logger.error("host configuration reload rejected; keeping the running configuration: %s", exc)


class ControllerLocked(RuntimeError):
    code = "controller_locked"


def acquire_controller(data_dir: Path) -> Any:
    """Hold the data directory for this process (ADR 0004). The kernel releases a
    ``flock`` when the process dies, so a crash never blocks the next start."""
    handle = (data_dir / "controller.lock").open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ControllerLocked(f"another controller holds {data_dir}; one controller owns one store") from None
    return handle


def release_controller(handle: Any) -> None:
    if handle is not None and not handle.closed:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def controller_running(data_dir: Path) -> bool:
    if not (data_dir / "controller.lock").exists():
        return False
    try:
        release_controller(acquire_controller(data_dir))
    except ControllerLocked:
        return True
    return False


def server_ssl_context(config: HostConfig) -> ssl.SSLContext:
    server = config.server
    assert server.tls_certfile is not None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(server.tls_certfile, server.tls_keyfile)
    if server.tls_client_ca is not None:
        context.load_verify_locations(cafile=server.tls_client_ca)
        context.verify_mode = ssl.CERT_REQUIRED
    return context


def close_untrusted(transports: Iterable[asyncio.BaseTransport], context: ssl.SSLContext) -> int:
    """Close connections whose client certificate issuer is no longer a trusted CA."""
    trusted = {tuple(tuple(part) for part in ca["subject"]) for ca in context.get_ca_certs()}
    closed = 0
    for transport in list(transports):
        connection = transport.get_extra_info("ssl_object")
        peer = None if connection is None else connection.getpeercert()
        if not peer or "issuer" not in peer:
            continue
        if tuple(tuple(part) for part in peer["issuer"]) not in trusted:
            transport.close()
            closed += 1
    return closed


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


def noesis_http_transport(config: NoesisConfig, token: str) -> HTTPTransport:
    # Noesis accepts its API keys as bearer credentials (src/api/auth/api_key_middleware.py).
    return HTTPTransport(config.base_url, headers={"Authorization": "Bearer " + token},
                         timeout=config.timeout_seconds, ssl_context=noesis_ssl_context(config))


def retention_sweep(config: HostConfig, store: SQLiteStore, workspaces: LocalWorkspaces, *, apply: bool,
                    journal: list[Any] | None = None) -> RetentionReport:
    """Apply the [retention] policy once with only the store and workspaces, never a
    recovered kernel. Beside a live host, report only; apply while it is stopped."""
    retention = config.retention
    report = sweep(store, workspaces, RetentionPolicy(retention.workspace_days, retention.canonical_revisions),
                   canonical=[Path(root) for root in retention.canonical_roots], dry_run=not apply,
                   journal=journal)
    if apply:
        logger.info("retention removed %d item(s), %d bytes; kept %d", len(report.removed),
                    report.removed_bytes, len(report.kept))
    return report


def allowlist(config: HostConfig) -> tuple[AllowedGrant, ...]:
    return tuple((Resource(entry.resource), entry.actions, entry.scope) for entry in config.planning.grant_allowlist)


def build_host(config: HostConfig, *, environ: Mapping[str, str] | None = None,
               executors: Mapping[str, Executor] | None = None,
               noesis_transport: JSONTransport | None = None,
               config_path: Path | None = None,
               effect_adapters: Mapping[EffectKind, EffectAdapter] | None = None) -> Host:
    """Assemble a recovered kernel and its ASGI app. Extra ``executors`` (agent adapters
    inside isolated workers) are registered alongside the configured built-ins. With
    ``config_path`` the app reloads that file on SIGHUP. Extra ``effect_adapters`` join
    the configured ones under the same rule: an irreversible kind needs a lookup."""
    env = os.environ if environ is None else environ
    root = Path(config.data_dir).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = acquire_controller(root)
    try:
        return _build_host(config, root, lock, env, executors, noesis_transport, config_path, effect_adapters)
    except BaseException:
        release_controller(lock)
        raise


def _build_host(config: HostConfig, root: Path, lock: Any, env: Mapping[str, str],
                executors: Mapping[str, Executor] | None, noesis_transport: JSONTransport | None,
                config_path: Path | None, effect_adapters: Mapping[EffectKind, EffectAdapter] | None) -> Host:
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
    transport = None
    if config.noesis is not None:
        if noesis_transport is None:
            token = noesis_token(config.noesis, env)
            redaction.register(token)
            transport = SwappableTransport(noesis_http_transport(config.noesis, token))
        else:
            transport = SwappableTransport(noesis_transport)
        providers["noesis"] = DomainScopedProvider(NoesisContextProvider(transport), config.noesis.context_domains)
    live: dict[str, HostConfig] = {"config": config}  # reload replaces the policy it reads
    kernel = Kernel(store, workspaces, registry, authority=authority, context_providers=providers,
                    validators={"plan": PlanValidator(lambda: frozenset(registry),
                                                      lambda pid: kernel.budgets.limits.get(pid),
                                                      lambda: allowlist(live["config"]))})
    kernel.recover_records()
    security = HostSecurity(config, kernel)
    policy = HostEffects(kernel, config, security.owner)
    effects = EffectService(store, authority, approvers=policy)
    policy.service = effects
    adapters = build_adapters(config, kernel, lambda: effects, env, redaction)
    for kind, adapter in (effect_adapters or {}).items():
        if kind in adapters:
            raise ValueError(f"effect adapter for {kind.value} is already configured")
        adapters[kind] = adapter
    require_reconcilable(adapters)
    effects.adapters = adapters
    kernel.proposal_sink = policy
    for process_id, proposed in kernel.proposals():
        # Idempotent: effects staged before the restart are found by their key.
        policy.propose(process_id, proposed)
    for effect in effects.in_flight():
        logger.warning("effect %s of process %s is uncertain; reconcile it, it is never retried",
                       effect.effect_id, effect.process_id)
    publisher = worker = None
    if config.noesis is not None and transport is not None and config.noesis.publication != "off":
        exporter = NoesisExporter(transport, config.noesis.ingest_path)
        publisher = Publisher(kernel, store, authority, PublicationService(store, authority, exporter, target=TARGET))
        if config.noesis.publication == "auto":
            worker = PublicationWorker(publisher)
    planning = PlanMaterializer(kernel, GraphStore(store), lambda: allowlist(live["config"]))
    control = HostControlPlane(kernel, effects, config, publisher, policy, planning, security.owner)
    limits = config.limits
    limited = any(v is not None for v in (limits.requests_per_minute, limits.client_requests_per_minute,
                                         limits.submit_per_minute, limits.max_streams,
                                         limits.request_timeout_seconds))
    app = HostApplication(Application(control, security.hooks(), redaction), security, publisher, worker,
                          Limiter(limits) if limited else None)
    host = Host(config, store, kernel, authority, security, app, control, redaction, publisher, worker, providers,
                transport, noesis_transport is None, config_path, env)
    host.live = live
    host.lock = lock
    app.recovery.append(planning.recover)
    if config_path is not None:
        app.reloader = host.reload_from_signal
    if config.retention.enabled and config.retention.interval_hours is not None:
        app.retention = lambda: host.sweep(apply=True)
        app.retention_interval = config.retention.interval_hours * 3600
    return host


def create_app() -> HostApplication:
    """ASGI factory: ``uvicorn praxis.host.app:create_app --factory`` with PRAXIS_HOST_CONFIG set.

    Behind a generic ASGI server, SIGHUP reloads clients and Noesis credentials; the server's
    own TLS context is only reloadable under ``python -m praxis.host serve``.
    """
    path = os.environ.get("PRAXIS_HOST_CONFIG")
    if not path:
        raise ValueError("PRAXIS_HOST_CONFIG must name the host TOML file")
    return build_host(load_host_config(Path(path)), config_path=Path(path)).app
