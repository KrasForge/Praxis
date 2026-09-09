import json

import pytest

from praxis.kernel.budgets import ResourceBudget
from praxis.kernel.spec import ProcessSpec, SpecError


def test_budget_units_and_spec_roundtrip():
    budget = ResourceBudget(tokens=100, cost_microusd=1000000, wall_milliseconds=1000,
                            cpu_milliseconds=500, tool_calls=5)
    spec = ProcessSpec("work", "fake", budget=json.loads(budget.to_json()), priority=10)
    assert ProcessSpec.from_json(spec.to_json()) == spec
    assert ResourceBudget.from_json(budget.to_json()) == budget
    assert ResourceBudget(tokens=0).tokens == 0
    assert ResourceBudget().tokens is None


@pytest.mark.parametrize("budget", [{"tokens": -1}, {"tokens": True}, {"tokens": 1.5},
                                     {"cost_microusd": "1"}, {"unknown": 1}])
def test_invalid_budget(budget):
    with pytest.raises(SpecError):
        ProcessSpec("work", "fake", budget=budget)
