import asyncio
import json
import logging

from praxis.api.asgi import Application
from praxis.api.auth import Actor, SecurityHooks
from praxis.api.service import ControlPlane
from praxis.kernel.spec import ProcessSpec
from praxis.observability.redaction import REDACTED, RedactingLogFilter, RedactionPolicy
from test_api import make_kernel, request


def test_all_export_shapes_and_logs():
    canary = "canary-value-126"
    policy = RedactionPolicy((canary,))
    data = {"secret": canary, "nested": {"capability": {"scope": "private"}},
            "error": canary, "trace": {"name": canary},
            "event": json.dumps({"environment": {"KEY": canary}})}
    emitted = []
    policy.emit(emitted.append, data)
    assert canary not in json.dumps(emitted) and "private" not in json.dumps(emitted)
    assert data["secret"] == canary
    record = logging.LogRecord("test", logging.ERROR, "", 1, "error %s", (canary,), None)
    assert RedactingLogFilter(policy).filter(record)
    assert record.getMessage() == "error " + REDACTED


def test_api_redacts_inspection_without_corrupting_store(tmp_path):
    async def exercise():
        kernel = make_kernel(tmp_path)
        process = kernel.create(ProcessSpec("task", "fake", environment={"KEY": "canary-126"}))
        app = Application(ControlPlane(kernel), SecurityHooks(lambda _: Actor("test"), lambda *_: True))
        status, response = await request(app, "GET", f"/v1/processes/{process.process_id}")
        assert status == 200 and "canary-126" not in json.dumps(response)
        assert kernel.processes[process.process_id].spec.environment["KEY"] == "canary-126"
    asyncio.run(exercise())
