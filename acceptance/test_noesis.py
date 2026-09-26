"""Noesis through a locally built praxis.host: context search, allowlist refusal, publication.

Needs PRAXIS_ACCEPTANCE_NOESIS_URL, PRAXIS_ACCEPTANCE_NOESIS_TOKEN and
PRAXIS_ACCEPTANCE_NOESIS_DOMAIN (a domain the key can read). Optional TLS material:
PRAXIS_ACCEPTANCE_NOESIS_CA, PRAXIS_ACCEPTANCE_NOESIS_CLIENT_CERT and
PRAXIS_ACCEPTANCE_NOESIS_CLIENT_KEY. Publication writes, so it also needs
PRAXIS_ACCEPTANCE_NOESIS_PUBLISH=1 and PRAXIS_ACCEPTANCE_NOESIS_INGEST_PATH naming a
scratch ingest route.
"""

import asyncio
import os
from pathlib import Path

import pytest

from asgi import call
from praxis.host.app import build_host
from praxis.host.auth import new_token, token_digest
from praxis.host.config import parse_host_config

NOT_ALLOWED = "praxis-acceptance-not-allowed"


def host(root: Path, *, publish: bool = False):
    url = os.environ.get("PRAXIS_ACCEPTANCE_NOESIS_URL")
    domain = os.environ.get("PRAXIS_ACCEPTANCE_NOESIS_DOMAIN")
    if not url or not domain or not os.environ.get("PRAXIS_ACCEPTANCE_NOESIS_TOKEN"):
        pytest.skip("PRAXIS_ACCEPTANCE_NOESIS_URL, _TOKEN and _DOMAIN not all set")
    noesis = {"base_url": url, "token_env": "PRAXIS_ACCEPTANCE_NOESIS_TOKEN", "context_domains": [domain],
              "publication": "manual" if publish else "off"}
    for key, variable in (("ca_file", "CA"), ("client_certfile", "CLIENT_CERT"), ("client_keyfile", "CLIENT_KEY")):
        if os.environ.get("PRAXIS_ACCEPTANCE_NOESIS_" + variable):
            noesis[key] = os.environ["PRAXIS_ACCEPTANCE_NOESIS_" + variable]
    if publish:
        if os.environ.get("PRAXIS_ACCEPTANCE_NOESIS_PUBLISH") != "1" or not os.environ.get(
                "PRAXIS_ACCEPTANCE_NOESIS_INGEST_PATH"):
            pytest.skip("publication writes: set PRAXIS_ACCEPTANCE_NOESIS_PUBLISH=1 and _INGEST_PATH (scratch)")
        noesis["ingest_path"] = os.environ["PRAXIS_ACCEPTANCE_NOESIS_INGEST_PATH"]
    token, _ = new_token()
    config = parse_host_config({"data_dir": str(root / "data"), "noesis": noesis, "clients": [
        {"id": "acceptance", "token_sha256": token_digest(token), "roles": ["admin"]}]})
    return build_host(config), token, domain


def context_spec(domain: str, *, required: bool) -> dict[str, object]:
    query = os.environ.get("PRAXIS_ACCEPTANCE_NOESIS_QUERY", "praxis")
    return {"objective": "praxis acceptance", "executor": "fake", "context": [{
        "context_id": "kb", "provider": "noesis", "required": required,
        "request": {"query": query, "filters": [["domain", domain]], "max_results": 3}}]}


async def submit_and_wait(app, kernel, token: str, spec: dict[str, object]) -> str:
    status, body = await call(app, "POST", "/v1/processes", token, spec)
    assert status == 202, body
    await kernel.tasks[body["process_id"]]
    return str(body["process_id"])


def resolved(kernel, process_id: str) -> dict[str, object]:
    return next(e.payload for e in kernel.events if e.process_id == process_id and e.type == "context.resolved")


def test_noesis_context_search(scanned):
    built, token, domain = host(scanned)
    process_id = asyncio.run(submit_and_wait(built.app, built.kernel, token, context_spec(domain, required=False)))
    response = resolved(built.kernel, process_id)["response"]
    assert isinstance(response, dict) and response["status"] == "available", response
    assert built.kernel.processes[process_id].state.value == "completed"
    built.close()


def test_noesis_domain_allowlist_refuses_without_contacting_noesis(scanned):
    built, token, _ = host(scanned)
    process_id = asyncio.run(submit_and_wait(built.app, built.kernel, token, context_spec(NOT_ALLOWED, required=True)))
    response = resolved(built.kernel, process_id)["response"]
    # This reason is produced only by the host's local allowlist, never by Noesis.
    assert isinstance(response, dict) and response["reason"] == "noesis_domain_not_allowed"
    assert built.kernel.result(process_id).outcome.reason == "required_context_unavailable"
    built.close()


def test_noesis_publication_is_idempotent(scanned):
    built, token, _ = host(scanned, publish=True)

    async def exercise():
        process_id = await submit_and_wait(built.app, built.kernel, token,
                                           {"objective": "praxis acceptance publication", "executor": "fake"})
        first = await call(built.app, "POST", f"/v1/processes/{process_id}/publication", token)
        again = await call(built.app, "POST", f"/v1/processes/{process_id}/publication", token)
        return first, again
    (first_status, first), (again_status, again) = asyncio.run(exercise())
    assert first_status == 200 and first["publication"]["status"] == "accepted", first
    assert again_status == 200 and again["publication"]["status"] == "duplicate", again
    assert again["publication"]["document_id"] == first["publication"]["document_id"]
    built.close()
