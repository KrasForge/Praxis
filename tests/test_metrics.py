import pytest

from praxis.observability.metrics import CATALOG, METRICS_VERSION, Metrics


def test_metric_contract_and_cardinality():
    metrics = Metrics()
    assert METRICS_VERSION == 1
    metrics.emit("praxis_processes_created_total", 1, executor="local")
    metrics.emit("praxis_execution_seconds", 0.5, executor="local")
    metrics.emit("praxis_execution_seconds", 1.5, executor="local")
    metrics.emit("praxis_usage_tokens_total", 12)
    assert metrics.value("praxis_execution_seconds", executor="local") == 2
    assert metrics.value("praxis_usage_tokens_total") == 12
    assert all(name.startswith("praxis_") for name in CATALOG)
    with pytest.raises(ValueError, match="labels"):
        metrics.emit("praxis_processes_created_total", 1, executor="local", process_id="secret")
    with pytest.raises(ValueError, match="labels"):
        metrics.emit("praxis_processes_created_total", 1, executor="arbitrary-process-id")
    with pytest.raises(ValueError, match="value"):
        metrics.emit("praxis_queue_depth", float("nan"))


def test_runtime_metrics_replay_and_retry_accounting(tmp_path):
    import asyncio

    from praxis.executors.fake import FakeExecutor
    from praxis.executors.outcomes import Outcome, OutcomeStatus
    from praxis.kernel.authority import Authority
    from praxis.kernel.retry import RetryPolicy
    from praxis.kernel.runtime import Kernel
    from praxis.kernel.scheduler import Scheduler
    from praxis.kernel.spec import ProcessSpec
    from praxis.observability.runtime import RuntimeMetrics
    from praxis.storage.memory import MemoryStore
    from praxis.workspaces.local import LocalWorkspaces

    async def exercise():
        store = MemoryStore()
        kernel = Kernel(store, LocalWorkspaces(tmp_path), {"fake": FakeExecutor(Outcome(OutcomeStatus.FAILED, "transient", retryable=True))},
                        authority=Authority(execution_defaults=frozenset({"fake"})))
        process = kernel.create(ProcessSpec("task", "fake"))
        scheduler = Scheduler(kernel)
        scheduler.enqueue(process.process_id)
        await scheduler.drain()
        kernel.executors["fake"] = FakeExecutor()
        await kernel.retry(process.process_id, RetryPolicy())
        await kernel.tasks[process.process_id]
        metrics = kernel.metrics.refresh()
        assert metrics.value("praxis_processes_created_total", executor="fake") == 1
        assert metrics.value("praxis_attempts_total", executor="fake") == 2
        assert metrics.value("praxis_retries_total", executor="fake") == 1
        assert metrics.value("praxis_failures_total", failure_class="executor") == 1
        assert metrics.value("praxis_queue_depth") == 0
        assert metrics.value("praxis_usage_wall_milliseconds_total") == kernel.usage.total(process.process_id)["wall_milliseconds"]
        before = kernel.metrics.snapshot()
        assert kernel.metrics.snapshot() == before
        rebuilt = RuntimeMetrics(lambda after: store.read_events(after=after), lambda pid: kernel.processes[pid].spec.executor)
        assert rebuilt.snapshot() == before
    asyncio.run(exercise())
