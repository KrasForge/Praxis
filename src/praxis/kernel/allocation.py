"""Parent-child reservations and measured budget exhaustion."""

from praxis.kernel.budgets import RESOURCES, ResourceBudget
from praxis.kernel.usage import UsageLedger


class BudgetExceeded(ValueError):
    code = "budget_exhausted"


class BudgetManager:
    def __init__(self, usage: UsageLedger):
        self.usage = usage
        self.limits: dict[str, ResourceBudget] = {}
        self.parents: dict[str, str | None] = {}
        self.active: set[str] = set()

    def allocate(self, process_id: str, budget: ResourceBudget, parent_id: str | None = None) -> None:
        if process_id in self.limits:
            raise ValueError("duplicate allocation")
        if parent_id is not None:
            if parent_id not in self.limits:
                raise ValueError("unknown parent budget")
            for resource in RESOURCES:
                available = self.remaining(parent_id, resource)
                requested = getattr(budget, resource)
                if available is not None and (requested is None or requested > available):
                    raise BudgetExceeded("child_allocation_exceeds_parent")
        self.limits[process_id] = budget
        self.parents[process_id] = parent_id
        self.active.add(process_id)

    def remaining(self, process_id: str, resource: str) -> int | None:
        limit: int | None = getattr(self.limits[process_id], resource)
        if limit is None:
            return None
        spent = self.usage.total(process_id, tree=True).get(resource, 0)
        reserved = 0
        for child in self.active:
            if self.parents[child] == process_id:
                allocated = getattr(self.limits[child], resource)
                if allocated is not None:
                    reserved += max(0, allocated - self.usage.total(child, tree=True).get(resource, 0))
        return max(0, limit - spent - reserved)

    def check(self, process_id: str, values: dict[str, int]) -> None:
        if not values.keys() <= RESOURCES or any(type(v) is not int or v < 0 for v in values.values()):
            raise ValueError("invalid usage")
        for resource, value in values.items():
            remaining = self.remaining(process_id, resource)
            if remaining is not None and value > remaining:
                raise BudgetExceeded(resource)
            parent = self.parents[process_id]
            while parent is not None:
                limit = getattr(self.limits[parent], resource)
                if limit is not None and self.usage.total(parent, tree=True).get(resource, 0) + value > limit:
                    raise BudgetExceeded(resource)
                parent = self.parents[parent]

    def release(self, process_id: str) -> None:
        self.active.discard(process_id)

    def reactivate(self, process_id: str) -> None:
        parent = self.parents[process_id]
        if parent is not None and process_id not in self.active:
            for resource in RESOURCES:
                available = self.remaining(parent, resource)
                limit = getattr(self.limits[process_id], resource)
                spent = self.usage.total(process_id, tree=True).get(resource, 0)
                if available is not None and (limit is None or max(0, limit - spent) > available):
                    raise BudgetExceeded("retry_allocation_exceeds_parent")
        self.active.add(process_id)
