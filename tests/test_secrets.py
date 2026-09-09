"""TM-7: denied provider access and no ordinary persistence of injected values."""
import asyncio
import sys

import pytest

from praxis.executors.local import LocalProcessExecutor
from praxis.kernel.authority import Authority, AuthorizationError
from praxis.kernel.capabilities import Resource
from praxis.kernel.runtime import Kernel
from praxis.kernel.secrets import SecretAccess
from praxis.kernel.spec import ProcessSpec
from praxis.observability.redaction import RedactionPolicy
from praxis.storage.memory import MemoryStore
from praxis.workspaces.local import LocalWorkspaces


class Provider:
    def __init__(self):
        self.calls = []

    def resolve(self, name):
        self.calls.append(name)
        return "secret-canary-129"


def test_denied_does_not_contact_provider():
    provider = Provider()
    access = SecretAccess(provider, Authority(), RedactionPolicy())
    with pytest.raises(AuthorizationError):
        access.environment("p", {"TOKEN": "provider/token"})
    assert not provider.calls


def test_injection_output_and_journal_canary(tmp_path):
    async def exercise():
        authority = Authority(execution_defaults=frozenset({"local"}))
        access = SecretAccess(Provider(), authority, RedactionPolicy())
        workspaces = LocalWorkspaces(tmp_path)
        executor = LocalProcessExecutor(workspaces, secrets=access, secret_bindings={"TOKEN": "provider/token"})
        kernel = Kernel(MemoryStore(), workspaces, {"local": executor}, authority=authority)
        process = kernel.create(ProcessSpec("echo", "local", inputs={"argv": [sys.executable, "-c", "import os; print(os.environ['TOKEN'])"]}))
        grant = authority.issue(process.process_id, Resource.SECRET, frozenset({"read"}), "provider/token")
        kernel.start(process.process_id)
        await kernel.tasks[process.process_id]
        assert kernel.result(process.process_id).outcome.stdout.strip() == "[REDACTED]"
        assert "secret-canary-129" not in str([e.to_json() for e in kernel.events])
        assert "secret-canary-129" not in str(executor.requests)
        authority.revoke(grant.capability_id, actor="kernel", reason="test")
        with pytest.raises(AuthorizationError):
            access.environment(process.process_id, {"TOKEN": "provider/token"})
    asyncio.run(exercise())
