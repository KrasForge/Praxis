"""The ``plan`` validator: a plan that is unsafe or pointless to run fails verification.

It reads ``plan.json`` from the immutable validation input, never the live workspace,
and checks the schema, the graph, the registered executors, that the node budgets fit
inside the budget of the planning process, that every node has a non-empty contract,
and that every requested grant is inside the host allowlist (ADR 0003).
"""

import json
from collections.abc import Callable, Collection

from praxis.kernel.budgets import RESOURCES, ResourceBudget
from praxis.kernel.capabilities import Resource, scope_contains
from praxis.kernel.contracts import Check, Contract
from praxis.kernel.plan import PLAN_FILE, GrantRequest, Plan, PlanError, parse_plan
from praxis.validators.protocol import CheckResult, CheckStatus, ValidationInput

AllowedGrant = tuple[Resource, frozenset[str], str]


def grant_allowed(request: GrantRequest, allowlist: Collection[AllowedGrant]) -> bool:
    return any(resource == request.resource and request.actions <= actions
               and scope_contains(resource, scope, request.scope)
               for resource, actions, scope in allowlist)


def check_plan(plan: Plan, executors: Collection[str], budget: ResourceBudget | None,
               allowlist: Collection[AllowedGrant]) -> str | None:
    """The first reason the plan must not run, or None."""
    for key, spec in plan.nodes:
        if spec.executor not in executors:
            return f"unregistered_executor:{key}"
        contract = Contract.from_json(json.dumps(spec.contract))
        if not (contract.required_outputs or contract.invariants or contract.validators):
            return f"empty_contract:{key}"
    if budget is not None:
        for resource in sorted(RESOURCES):
            limit = getattr(budget, resource)
            if limit is None:
                continue
            requests = [spec.budget.get(resource) for _, spec in plan.nodes]
            if any(value is None for value in requests) or sum(value or 0 for value in requests) > limit:
                return f"budget_exceeds_plan:{resource}"
    for request in plan.requested_grants:
        if not grant_allowed(request, allowlist):
            return f"grant_not_allowed:{request.node}"
    return None


class PlanValidator:
    protocol_version = 1

    def __init__(self, executors: Callable[[], Collection[str]],
                 budget: Callable[[str], ResourceBudget | None],
                 allowlist: Callable[[], Collection[AllowedGrant]]):
        self.executors = executors
        self.budget = budget
        self.allowlist = allowlist

    async def validate(self, source: ValidationInput, check: Check) -> CheckResult:
        content = dict(source.files).get(PLAN_FILE)
        if content is None:
            return CheckResult(check.check_id, CheckStatus.FAIL, "plan_missing")
        try:
            plan = parse_plan(content.decode())
        except (PlanError, UnicodeError) as exc:
            detail = f"{exc.field}: {exc.reason}" if isinstance(exc, PlanError) else "invalid_encoding"
            return CheckResult(check.check_id, CheckStatus.FAIL, "invalid_plan", stderr=detail)
        reason = check_plan(plan, self.executors(), self.budget(source.process_id), self.allowlist())
        if reason is not None:
            return CheckResult(check.check_id, CheckStatus.FAIL, reason)
        return CheckResult(check.check_id, CheckStatus.PASS, "plan_valid",
                           stdout=f"{len(plan.nodes)} node(s), {len(plan.dependencies)} dependenc(ies)")
