import asyncio

from praxis.executors.fake import FakeExecutor
from praxis.executors.outcomes import OutcomeStatus
from praxis.executors.protocol import ExecutionRequest, Executor
from praxis.kernel.spec import ProcessSpec


def test_fake_conformance(tmp_path):
    async def exercise():
        executor: Executor = FakeExecutor()
        assert isinstance(executor, Executor)
        request = ExecutionRequest("p", "a", ProcessSpec("work", "fake"), "w", tmp_path)
        assert (await executor.start(request)).applied
        assert (await executor.start(request)).reason == "duplicate"
        assert (await executor.collect_result("a")).status == OutcomeStatus.COMPLETED
        assert (await executor.collect_result("missing")).status == OutcomeStatus.UNAVAILABLE
        assert not (await executor.signal("a", "suspend")).supported
        assert not (await executor.checkpoint("a")).control.supported
        assert not (await executor.cancel("a")).applied
    asyncio.run(exercise())
