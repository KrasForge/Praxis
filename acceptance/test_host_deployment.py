"""A deployed praxis.host, reached the way Modulo reaches it.

Needs PRAXIS_ACCEPTANCE_HOST_URL and PRAXIS_ACCEPTANCE_HOST_TOKEN (a client with
delegate = true and the submit and read roles). Optional: PRAXIS_ACCEPTANCE_HOST_CA,
PRAXIS_ACCEPTANCE_HOST_CLIENT_CERT and PRAXIS_ACCEPTANCE_HOST_CLIENT_KEY for mutual TLS,
PRAXIS_ACCEPTANCE_HOST_USER (default praxis-acceptance) and
PRAXIS_ACCEPTANCE_HOST_EXECUTOR (default fake).
"""

import asyncio
import os
import ssl
from uuid import uuid4

import pytest

from praxis.client import Client
from praxis.client.http import ClientHTTPTransport
from praxis.kernel.lifecycle import TERMINAL
from praxis.kernel.spec import ProcessSpec


def client() -> Client:
    url, token = os.environ.get("PRAXIS_ACCEPTANCE_HOST_URL"), os.environ.get("PRAXIS_ACCEPTANCE_HOST_TOKEN")
    if not url or not token:
        pytest.skip("PRAXIS_ACCEPTANCE_HOST_URL and _TOKEN not set")
    context = None
    if url.startswith("https://"):
        context = ssl.create_default_context(cafile=os.environ.get("PRAXIS_ACCEPTANCE_HOST_CA"))
        if os.environ.get("PRAXIS_ACCEPTANCE_HOST_CLIENT_CERT"):
            context.load_cert_chain(os.environ["PRAXIS_ACCEPTANCE_HOST_CLIENT_CERT"],
                                    os.environ.get("PRAXIS_ACCEPTANCE_HOST_CLIENT_KEY"))
    user = os.environ.get("PRAXIS_ACCEPTANCE_HOST_USER", "praxis-acceptance")
    return Client(ClientHTTPTransport(url, headers={"Authorization": f"Bearer {token}",
                                                    "X-Praxis-On-Behalf-Of": user}, ssl_context=context))


def test_submit_stream_inspect_on_behalf_of_a_user(versions):
    api = client()
    executor = os.environ.get("PRAXIS_ACCEPTANCE_HOST_EXECUTOR", "fake")
    key = f"praxis-acceptance-{uuid4()}"

    async def exercise():
        receipt = await api.submit(ProcessSpec("praxis acceptance", executor), idempotency_key=key)
        seen, cursor = [], 0
        async with asyncio.timeout(120):
            while True:
                async for stored in api.events(receipt.process_id, after=cursor):
                    cursor = stored.cursor
                    seen.append(stored.event.type)
                    if stored.event.type == "process.result":
                        break
                if "process.result" in seen:
                    break
                await asyncio.sleep(1)  # the stream ended before the result; resume from the cursor
        view = await api.inspect(receipt.process_id)
        replay = await api.submit(ProcessSpec("praxis acceptance", executor), idempotency_key=key)
        return receipt, seen, view, replay
    receipt, seen, view, replay = asyncio.run(exercise())
    assert seen[0] == "process.created" and "process.result" in seen
    assert view.state in TERMINAL and view.result is not None
    assert replay.process_id == receipt.process_id and replay.duplicate is True
    versions["host"] = os.environ["PRAXIS_ACCEPTANCE_HOST_URL"].split("://", 1)[0] + " " + executor
