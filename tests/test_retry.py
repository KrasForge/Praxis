import asyncio

import pytest

from praxis.executors.fake import FakeExecutor
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.authority import Authority
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import State
from praxis.kernel.retry import RetryError, RetryPolicy
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces


def test_retry_after_restart_preserves_identity_and_lineage(tmp_path):
    async def exercise():
        path = tmp_path / "runtime.db"
        store = SQLiteStore(path)
        provider = LocalWorkspaces(tmp_path / "ws")
        fake = FakeExecutor(Outcome(OutcomeStatus.FAILED, "transient", retryable=True))
        kernel = Kernel(store, provider, {"fake": fake}, authority=Authority(execution_defaults=frozenset({"fake"})))
        process = kernel.create(ProcessSpec("work", "fake"))
        kernel.start(process.process_id)
        await kernel.tasks[process.process_id]
        previous = process.attempt_id
        store.close()
        reopened = SQLiteStore(path)
        recovered = Kernel(reopened, provider, {"fake": FakeExecutor()})
        recovered.recover_records()
        current = await recovered.retry(process.process_id, RetryPolicy())
        assert current != previous
        await recovered.tasks[process.process_id]
        restored = reopened.load(process.process_id)
        assert restored.state == State.COMPLETED
        assert restored.created_at == process.created_at
        assert {entry.attempt_id for entry in restored.history} == {previous, current}
        reopened.close()
    asyncio.run(exercise())


def test_retry_exhaustion_and_effect_replay_guard(tmp_path):
    async def exercise():
        store = SQLiteStore(tmp_path / "runtime.db")
        kernel = Kernel(store, LocalWorkspaces(tmp_path / "ws"), {
            "fake": FakeExecutor(Outcome(OutcomeStatus.FAILED, "transient", retryable=True)),
        }, authority=Authority(execution_defaults=frozenset({"fake"})))
        process = kernel.create(ProcessSpec("work", "fake"))
        kernel.start(process.process_id)
        await kernel.tasks[process.process_id]
        with pytest.raises(RetryError, match="exhausted"):
            await kernel.retry(process.process_id, RetryPolicy(max_attempts=1))
        kernel.events.append(Event(process.process_id, "effect.applied", {"replay_safe": False}))
        with pytest.raises(RetryError, match="unsafe_effect"):
            await kernel.retry(process.process_id, RetryPolicy())
        store.close()
    asyncio.run(exercise())
