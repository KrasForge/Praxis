"""Closed TOML schema for the Modulo/Noesis host; secrets are referenced, never stored."""

import ipaddress
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROLES = frozenset({"submit", "read", "control", "approve", "publish", "health", "admin"})
EXECUTORS = frozenset({"fake", "local"})
PUBLICATION_MODES = frozenset({"off", "manual", "auto"})
CLIENT_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
DIGEST = re.compile(r"[0-9a-f]{64}")


class HostConfigError(ValueError):
    code = "invalid_host_config"

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{self.code}: {field}: {reason}")


@dataclass(frozen=True)
class ClientConfig:
    id: str
    token_sha256: str
    roles: frozenset[str]
    delegate: bool = False


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8443
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    tls_client_ca: str | None = None
    tls_terminated_upstream: bool = False

    @property
    def tls(self) -> bool:
        return self.tls_certfile is not None


@dataclass(frozen=True)
class NoesisConfig:
    base_url: str
    token_env: str | None = None
    token_file: str | None = None
    ca_file: str | None = None
    client_certfile: str | None = None
    client_keyfile: str | None = None
    ingest_path: str = "/documents/ingest"
    publication: str = "manual"
    timeout_seconds: float = 15.0


@dataclass(frozen=True)
class HostConfig:
    data_dir: str
    clients: tuple[ClientConfig, ...]
    executors: frozenset[str] = frozenset({"fake"})
    server: ServerConfig = ServerConfig()
    noesis: NoesisConfig | None = None

    def client(self, identity: str) -> ClientConfig | None:
        return next((client for client in self.clients if client.id == identity), None)


def load_host_config(path: Path) -> HostConfig:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, ValueError) as exc:
        raise HostConfigError("file", "cannot read valid TOML") from exc
    return parse_host_config(data)


def parse_host_config(data: Mapping[str, Any]) -> HostConfig:
    _closed("root", data, {"data_dir", "executors", "server", "clients", "noesis"})
    data_dir = _path("data_dir", data.get("data_dir"))
    if data_dir is None:
        raise HostConfigError("data_dir", "required")
    executors = data.get("executors", ["fake"])
    if (not isinstance(executors, list) or not executors or len(set(executors)) != len(executors)
            or any(name not in EXECUTORS for name in executors)):
        raise HostConfigError("executors", "expected unique names from " + ", ".join(sorted(EXECUTORS)))
    server = _server(data.get("server", {}))
    clients = _clients(data.get("clients"))
    noesis = None if "noesis" not in data else _noesis(data["noesis"])
    return HostConfig(data_dir, clients, frozenset(executors), server, noesis)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _server(data: Any) -> ServerConfig:
    _closed("server", data, {"host", "port", "tls_certfile", "tls_keyfile", "tls_client_ca",
                             "tls_terminated_upstream"})
    host = data.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host or any(c.isspace() for c in host):
        raise HostConfigError("server.host", "expected host name or address")
    port = data.get("port", 8443)
    if type(port) is not int or not 1 <= port <= 65535:
        raise HostConfigError("server.port", "expected 1..65535")
    upstream = data.get("tls_terminated_upstream", False)
    if type(upstream) is not bool:
        raise HostConfigError("server.tls_terminated_upstream", "expected boolean")
    server = ServerConfig(host, port, _path("server.tls_certfile", data.get("tls_certfile")),
                          _path("server.tls_keyfile", data.get("tls_keyfile")),
                          _path("server.tls_client_ca", data.get("tls_client_ca")), upstream)
    if (server.tls_certfile is None) != (server.tls_keyfile is None):
        raise HostConfigError("server.tls_keyfile", "certificate and key must be configured together")
    if server.tls_client_ca is not None and not server.tls:
        raise HostConfigError("server.tls_client_ca", "client verification requires server TLS")
    if server.tls and upstream:
        raise HostConfigError("server.tls_terminated_upstream", "conflicts with local TLS")
    if not server.tls and not upstream and not is_loopback(host):
        raise HostConfigError("server.host", "non-loopback binding requires TLS")
    return server


def _clients(data: Any) -> tuple[ClientConfig, ...]:
    if not isinstance(data, list) or not data:
        raise HostConfigError("clients", "at least one client required")
    clients = []
    for index, item in enumerate(data):
        field = f"clients[{index}]"
        _closed(field, item, {"id", "token_sha256", "roles", "delegate"})
        identity, digest, roles = item.get("id"), item.get("token_sha256"), item.get("roles")
        if not isinstance(identity, str) or not CLIENT_ID.fullmatch(identity):
            raise HostConfigError(field + ".id", "expected lowercase identifier")
        if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            raise HostConfigError(field + ".token_sha256", "expected lowercase SHA-256 hex digest")
        if (not isinstance(roles, list) or not roles or len(set(roles)) != len(roles)
                or any(role not in ROLES for role in roles)):
            raise HostConfigError(field + ".roles", "expected unique roles from " + ", ".join(sorted(ROLES)))
        delegate = item.get("delegate", False)
        if type(delegate) is not bool:
            raise HostConfigError(field + ".delegate", "expected boolean")
        clients.append(ClientConfig(identity, digest, frozenset(roles), delegate))
    if len({c.id for c in clients}) != len(clients):
        raise HostConfigError("clients", "duplicate client id")
    if len({c.token_sha256 for c in clients}) != len(clients):
        raise HostConfigError("clients", "duplicate client token")
    return tuple(clients)


def _noesis(data: Any) -> NoesisConfig:
    _closed("noesis", data, {"base_url", "token_env", "token_file", "ca_file", "client_certfile",
                             "client_keyfile", "ingest_path", "publication", "timeout_seconds"})
    base_url = data.get("base_url")
    parsed = urlsplit(base_url) if isinstance(base_url, str) else None
    if (parsed is None or parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise HostConfigError("noesis.base_url", "expected http(s) origin")
    if parsed.scheme == "http" and not is_loopback(parsed.hostname):
        raise HostConfigError("noesis.base_url", "non-loopback Noesis requires https")
    token_env, token_file = data.get("token_env"), _path("noesis.token_file", data.get("token_file"))
    if token_env is not None and (not isinstance(token_env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", token_env)):
        raise HostConfigError("noesis.token_env", "expected environment variable name")
    if (token_env is None) == (token_file is None):
        raise HostConfigError("noesis.token_env", "configure exactly one of token_env or token_file")
    ingest_path = data.get("ingest_path", "/documents/ingest")
    if not isinstance(ingest_path, str) or not ingest_path.startswith("/") or ingest_path.startswith("//"):
        raise HostConfigError("noesis.ingest_path", "expected absolute path")
    publication = data.get("publication", "manual")
    if publication not in PUBLICATION_MODES:
        raise HostConfigError("noesis.publication", "expected one of " + ", ".join(sorted(PUBLICATION_MODES)))
    timeout = data.get("timeout_seconds", 15.0)
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise HostConfigError("noesis.timeout_seconds", "expected 0 < seconds <= 300")
    client_cert = _path("noesis.client_certfile", data.get("client_certfile"))
    client_key = _path("noesis.client_keyfile", data.get("client_keyfile"))
    if client_key is not None and client_cert is None:
        raise HostConfigError("noesis.client_keyfile", "requires client_certfile")
    if (client_cert is not None or data.get("ca_file") is not None) and parsed.scheme != "https":
        raise HostConfigError("noesis.base_url", "TLS settings require https")
    return NoesisConfig(base_url.rstrip("/"), token_env, token_file, _path("noesis.ca_file", data.get("ca_file")),
                        client_cert, client_key, ingest_path, publication, float(timeout))


def _closed(field: str, data: Any, allowed: set[str]) -> None:
    if not isinstance(data, Mapping):
        raise HostConfigError(field, "expected table")
    unknown = set(data) - allowed
    if unknown:
        raise HostConfigError(field, "unknown field " + sorted(unknown)[0])


def _path(field: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise HostConfigError(field, "expected nonempty path")
    return value
