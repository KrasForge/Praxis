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
