import asyncio

from praxis.executors.fake import FakeExecutor
from praxis.executors.features import ExecutorFeatures
from praxis.executors.protocol import Checkpoint, CheckpointResult, ControlResult, ExecutionRequest
from praxis.kernel.authority import Authority
from praxis.kernel.checkpoints import CheckpointManager
from praxis.kernel.lifecycle import State
from praxis.kernel.process import Process
from praxis.kernel.spec import ProcessSpec
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces


class RestorableFake(FakeExecutor):
    @property
    def descriptor(self):
        return ExecutorFeatures("fake", frozenset({"checkpoint", "restore"}))

    async def checkpoint(self, attempt_id):
        request = self.requests[attempt_id]
        return CheckpointResult(ControlResult(True, True, "saved"),
                                Checkpoint("fake", request.process_id, attempt_id, b"state"))

    async def restore(self, request, checkpoint):
        assert checkpoint.payload == b"state"
        return await self.start(request)


def test_checkpoint_reopen_and_restore(tmp_path):
    async def exercise():
        process = Process(ProcessSpec("work", "fake"))
        process.move(State.RUNNING)
        authority = Authority(execution_defaults=frozenset({"fake"}))
        authority.configure_process(process.process_id)
        provider = LocalWorkspaces(tmp_path / "ws")
        handle = provider.create(process.process_id)
        path = provider.path_for(handle, process.process_id)
        (path / "file").write_bytes(b"checkpoint")
        store = SQLiteStore(tmp_path / "runtime.db")
        store.save(process)
        executor = RestorableFake()
        request = ExecutionRequest(process.process_id, process.attempt_id, process.spec, handle.workspace_id, path)
        await executor.start(request)
        manager = CheckpointManager(store, provider, authority)
        assert (await manager.capture(process, executor, handle)).control.applied
        store.close()
        (path / "file").write_bytes(b"later")
        reopened = SQLiteStore(tmp_path / "runtime.db")
        manager = CheckpointManager(reopened, provider, authority)
        assert (await manager.restore(process, RestorableFake(), handle)).applied
        assert (path / "file").read_bytes() == b"checkpoint"
        assert not (await manager.restore(process, FakeExecutor(), handle)).supported
        reopened.close()
    asyncio.run(exercise())
