import asyncio

import pytest

from praxis.executors.fake import FakeExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.contracts import Check, Contract
from praxis.kernel.lineage import Lineage
from praxis.kernel.process import ProcessRecords
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.validators.protocol import FakeValidator
from praxis.workspaces.local import LocalWorkspaces
import json


def test_executor_validator_causal_lineage(tmp_path):
    async def exercise():
        kernel = Kernel(ProcessRecords(tmp_path / "records"), LocalWorkspaces(tmp_path / "ws"),
                        {"fake": FakeExecutor()}, authority=Authority(execution_defaults=frozenset({"fake"})),
                        validators={"fake": FakeValidator()})
        process = kernel.create(ProcessSpec("work", "fake", contract=json.loads(Contract(validators=(Check("c", "fake"),)).to_json())))
        kernel.start(process.process_id)
        await kernel.tasks[process.process_id]
        linked = [Lineage.from_json(e.payload["lineage"]) for e in kernel.events if "lineage" in e.payload]
        assert len(linked) == 2
        assert linked[0].invocation_id == linked[1].invocation_id
        assert linked[1].validator_run_id
        for lineage in linked:
            lineage.validate(process, tuple(kernel.events))
            assert Lineage.from_json(lineage.to_json()) == lineage
        with pytest.raises(ValueError):
            linked[1].validate(process, ())
    asyncio.run(exercise())


def test_trace_parent_child_executor_validator_correlation(tmp_path):
    from praxis.observability.tracing import TraceContext, process_context

    async def exercise():
        kernel = Kernel(ProcessRecords(tmp_path / "records"), LocalWorkspaces(tmp_path / "ws"),
                        {"fake": FakeExecutor()}, authority=Authority(execution_defaults=frozenset({"fake"})), validators={"fake": FakeValidator()})
        parent = kernel.create(ProcessSpec("parent", "fake"))
        child = kernel.create(ProcessSpec("child", "fake", contract=json.loads(Contract(validators=(Check("c", "fake"),)).to_json())), parent.process_id)
        kernel.start(child.process_id)
        await kernel.tasks[child.process_id]
        traces = [TraceContext.from_json(json.dumps(e.payload["trace"])) for e in kernel.events if "trace" in e.payload]
        parent_trace = process_context(parent.process_id, lambda identity: kernel.processes[identity].parent_id)
        child_trace = process_context(child.process_id, lambda identity: kernel.processes[identity].parent_id)
        assert child_trace.parent_span_id == parent_trace.span_id
        assert all(t.trace_id == parent_trace.trace_id for t in traces)
        assert traces[1].parent_span_id == traces[0].span_id
        request = kernel.executors["fake"].requests[child.attempt_id]
        assert TraceContext.from_json(request.trace_json) == traces[0]
    asyncio.run(exercise())
