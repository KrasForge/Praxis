"""Operator commands: ``python -m praxis.host {token,check,serve,retention}``."""

import argparse
import importlib
import logging
import ssl
import sys
from collections.abc import Sequence
from pathlib import Path

from praxis.host.auth import new_token
from praxis.host.config import HostConfigError, load_host_config


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m praxis.host", description="Praxis host for Modulo and Noesis")
    commands = parser.add_subparsers(dest="command", required=True)
    token = commands.add_parser("token", help="generate a client token and the digest for the config")
    token.add_argument("--client", required=True, help="client id the token is for")
    check = commands.add_parser("check", help="validate the host config and referenced files")
    check.add_argument("--config", type=Path, required=True)
    serve = commands.add_parser("serve", help="serve the control plane with uvicorn")
    serve.add_argument("--config", type=Path, required=True)
    retention = commands.add_parser("retention", help="report or apply the [retention] policy once")
    retention.add_argument("--config", type=Path, required=True)
    retention.add_argument("--apply", action="store_true", help="remove and journal (stop the controller first); without it nothing changes")
    args = parser.parse_args(argv)

    if args.command == "token":
        value, digest = new_token()
        print(f"token for {args.client} (give to the client once; it is not stored):\n  {value}", file=sys.stderr)
        print(f'[[clients]]\nid = "{args.client}"\ntoken_sha256 = "{digest}"\nroles = ["submit", "read"]')
        return 0
    try:
        config = load_host_config(args.config)
    except HostConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    files = [config.server.tls_certfile, config.server.tls_keyfile, config.server.tls_client_ca]
    if config.noesis is not None:
        files += [config.noesis.token_file, config.noesis.ca_file, config.noesis.client_certfile,
                  config.noesis.client_keyfile]
    for entry in config.effects:
        files += [entry.token_file, entry.ca_file, entry.client_certfile, entry.client_keyfile]
    missing = [name for name in dict.fromkeys(files) if name is not None and not Path(name).is_file()]
    missing += [path for entry in config.effects for _, path in entry.repositories if not Path(path).is_dir()]
    if missing:
        print("missing files: " + ", ".join(missing), file=sys.stderr)
        return 2
    if args.command == "retention":
        if not config.retention.enabled:
            print("no [retention] policy: set workspace_days or canonical_revisions", file=sys.stderr)
            return 2
        from praxis.host.app import retention_sweep
        from praxis.storage.sqlite import SQLiteStore
        from praxis.workspaces.local import LocalWorkspaces
        root = Path(config.data_dir).resolve()
        if not (root / "runtime.db").is_file():
            print(f"no runtime database under {root}", file=sys.stderr)
            return 2
        store = SQLiteStore(root / "runtime.db")
        try:
            report = retention_sweep(config, store, LocalWorkspaces(root / "workspaces"), apply=args.apply)
        finally:
            store.close()
        print(report.to_json())
        return 0
    if args.command == "check":
        tls = ("mutual TLS" if config.server.tls_client_ca else "TLS" if config.server.tls
               else "TLS terminated upstream" if config.server.tls_terminated_upstream else "loopback only")
        publication = "disabled" if config.noesis is None else config.noesis.publication
        domains = ("" if config.noesis is None or config.noesis.context_domains is None
                   else f", context domains {', '.join(sorted(config.noesis.context_domains))}")
        effects = (f", {len(config.effect_policy)} effect rule(s), adapters for "
                   f"{', '.join(e.kind for e in config.effects) or 'no kinds'}")
        print(f"ok: {len(config.clients)} client(s), {tls}, Noesis publication {publication}{domains}{effects}")
        return 0
    try:
        uvicorn = importlib.import_module("uvicorn")
    except ImportError:
        print("serve requires an ASGI server: uv run --with uvicorn python -m praxis.host serve ...", file=sys.stderr)
        return 2
    from praxis.host.app import build_host

    # Reloads, publication retries and admission rejections are operator signals.
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    praxis_logger = logging.getLogger("praxis")
    praxis_logger.addHandler(handler)
    praxis_logger.setLevel(logging.INFO)
    praxis_logger.propagate = False
    host = build_host(config, config_path=args.config)
    server = config.server
    settings = uvicorn.Config(host.app, host=server.host, port=server.port, lifespan="on", server_header=False,
                              proxy_headers=server.tls_terminated_upstream,
                              ssl_certfile=server.tls_certfile, ssl_keyfile=server.tls_keyfile,
                              ssl_ca_certs=server.tls_client_ca,
                              ssl_cert_reqs=ssl.CERT_REQUIRED if server.tls_client_ca else ssl.CERT_NONE)
    settings.load()
    # SIGHUP reloads certificates into this live context for new handshakes.
    host.server_ssl = settings.ssl
    try:
        uvicorn.Server(settings).run()
    finally:
        host.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
