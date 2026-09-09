import asyncio
import sys

import pytest

from praxis.executors.local import LocalProcessExecutor
from praxis.executors.outcomes import OutcomeStatus
from praxis.kernel.allocation import BudgetExceeded, BudgetManager
from praxis.kernel.authority import Authority
from praxis.kernel.budgets import ResourceBudget
from praxis.kernel.process import ProcessRecords
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.kernel.usage import UsageLedger
from praxis.workspaces.local import LocalWorkspaces


def test_parent_child_reservations_and_exhaustion():
    usage = UsageLedger([])
    budgets = BudgetManager(usage)
    usage.register("p")
    budgets.allocate("p", ResourceBudget(tokens=100))
    budgets.allocate("c", ResourceBudget(tokens=60), "p")
    usage.register("c", "p")
    assert budgets.remaining("p", "tokens") == 40
    with pytest.raises(BudgetExceeded):
        budgets.allocate("other", ResourceBudget(tokens=41), "p")
    budgets.check("c", {"tokens": 50})
    usage.record("c", "a", "u", {"tokens": 50})
    with pytest.raises(BudgetExceeded):
        budgets.check("c", {"tokens": 11})
    budgets.release("c")
    assert budgets.remaining("p", "tokens") == 50


def test_wall_budget_stops_execution(tmp_path):
    async def exercise():
        provider = LocalWorkspaces(tmp_path / "ws")
        kernel = Kernel(ProcessRecords(tmp_path / "records"), provider,
                        {"local": LocalProcessExecutor(provider)},
                        authority=Authority(execution_defaults=frozenset({"local"})))
        process = kernel.create(ProcessSpec("wait", "local", inputs={
            "argv": [sys.executable, "-c", "import time; time.sleep(60)"],
        }, budget={"wall_milliseconds": 100}))
        kernel.start(process.process_id)
        result = await kernel.tasks[process.process_id]
        assert result.status == OutcomeStatus.BUDGET_EXHAUSTED
        assert process.process_id not in kernel.verification
    asyncio.run(exercise())
