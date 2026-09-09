import asyncio

from praxis.executors.fake import FakeExecutor
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.authority import Authority
from praxis.kernel.process import ProcessRecords
from praxis.kernel.runtime import Kernel
from praxis.kernel.scheduler import PlacementPolicy, Scheduler
from praxis.kernel.spec import ProcessSpec
from praxis.workspaces.local import LocalWorkspaces


class SlowFake(FakeExecutor):
    active = 0
    peak = 0

    async def collect_result(self, attempt_id):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return await super().collect_result(attempt_id)


def test_fallback_preserves_spec_and_bounded_concurrency(tmp_path):
    async def exercise():
        slow = SlowFake()
        kernel = Kernel(ProcessRecords(tmp_path / "records"), LocalWorkspaces(tmp_path / "ws"), {
            "missing": FakeExecutor(Outcome(OutcomeStatus.UNAVAILABLE, "offline")), "ok": slow,
        }, authority=Authority(execution_defaults=frozenset({"missing", "ok"})))
        scheduler = Scheduler(kernel, concurrency=3, executor_limits={"ok": 1})
        processes = [kernel.create(ProcessSpec("work", "missing")) for _ in range(3)]
        originals = [p.spec.to_json() for p in processes]
        for process in processes:
            scheduler.enqueue(process.process_id, PlacementPolicy(("missing", "ok")))
        results = await scheduler.drain()
        assert all(result.status == OutcomeStatus.COMPLETED for result in results)
        assert slow.peak == 1
        assert [p.spec.to_json() for p in processes] == originals
        assert all(len({h.attempt_id for h in p.history}) == 2 for p in processes)
        assert all(value == 0 for value in scheduler.active.values())
        incompatible = kernel.create(ProcessSpec("work", "ok"))
        scheduler.enqueue(incompatible.process_id, PlacementPolicy(("ok",), frozenset({"checkpoint"})))
        assert (await scheduler.drain())[0].reason == "no_compatible_executor"
    asyncio.run(exercise())
