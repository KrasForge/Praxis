"""TM-1/TM-4 regression corpus: protocol data never grants authority."""
import asyncio
import json
from dataclasses import replace

import pytest

from praxis.executors.fake import FakeExecutor
from praxis.kernel.authority import Authority, AuthorizationError
from praxis.kernel.capabilities import Capability, Resource
from praxis.kernel.lifecycle import State
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.storage.memory import MemoryStore
from praxis.workspaces.local import LocalWorkspaces


def test_executor_cannot_mutate_kernel_policy_or_self_assert(tmp_path):
    class MaliciousExecutor(FakeExecutor):
        async def start(self, request):
            request.spec.contract.clear()
            request.spec.capabilities.append({"issuer": "kernel", "resource": "secret", "scope": "*"})
            return await super().start(request)
    async def exercise():
        authority = Authority(execution_defaults=frozenset({"fake"}))
        kernel = Kernel(MemoryStore(), LocalWorkspaces(tmp_path), {"fake": MaliciousExecutor()}, authority=authority)
        process = kernel.create(ProcessSpec("attack", "fake", contract={"required_outputs": ["required"]}))
        before = process.spec.to_json()
        grants = dict(authority.grants)
        kernel.start(process.process_id)
        await kernel.tasks[process.process_id]
        assert process.state == State.FAILED
        assert process.spec.to_json() == before
        assert authority.grants == grants
        assert not authority.authorize(process.process_id, Resource.SECRET, "read", "private").allowed
        with pytest.raises(ValueError):
            kernel.create(ProcessSpec("forged", "fake", capabilities=[{"issuer": "kernel"}]))
    asyncio.run(exercise())


@pytest.mark.parametrize("scope,actions", [("/other", frozenset({"read"})), ("/service", frozenset({"read", "write"}))])
def test_delegation_escalation_fixture(scope, actions):
    authority = Authority()
    authority.configure_process("p")
    authority.configure_process("c", "p")
    cap = authority.issue("p", Resource.FILESYSTEM, frozenset({"read"}), "/service")
    with pytest.raises((AuthorizationError, ValueError)):
        authority.delegate("p", "c", cap.capability_id, actions=actions, scope=scope)
    assert len(authority.grants) == 1


def test_forged_provenance_and_missing_ancestor():
    authority = Authority()
    cap = Capability(Resource.SECRET, frozenset({"read"}), "private", "attacker", "p")
    # Parsing a claim does not install it.
    parsed = Capability.from_json(cap.to_json())
    assert not authority.authorize("p", Resource.SECRET, "read", "private").allowed
    # Even corrupt host records with a non-kernel issuer fail provenance checks.
    authority.grants[parsed.capability_id] = parsed
    assert not authority.authorize("p", Resource.SECRET, "read", "private").allowed
    forged = replace(parsed, parent_id="00000000-0000-0000-0000-000000000001")
    authority.grants[forged.capability_id] = forged
    assert not authority.authorize("p", Resource.SECRET, "read", "private").allowed
    assert "allowed\": true" not in json.dumps([e.to_json() for e in authority.events])
