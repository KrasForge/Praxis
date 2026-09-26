"""Plans as verified, materializable artifacts (ADR 0003)."""

import asyncio
import json
import sys

import pytest

from praxis.host.app import build_host
from praxis.host.auth import token_digest
from praxis.host.config import HostConfigError, parse_host_config
from praxis.kernel.budgets import ResourceBudget
from praxis.kernel.capabilities import Resource
from praxis.kernel.contracts import Check
from praxis.kernel.plan import Plan, PlanError, parse_plan
from praxis.validators.plan import PlanValidator
from praxis.validators.protocol import CheckStatus, ValidationInput

MODULO, REVIEW = "m" * 40, "r" * 40
OUT = {"required_outputs": ["out"]}


def node(key, executor="local", contract=None, budget=None, script="Path('out').write_text('ok')"):
    spec = {"objective": key, "executor": executor, "contract": OUT if contract is None else contract,
            "inputs": {"argv": [sys.executable, "-c", "from pathlib import Path\n" + script]}}
    if budget is not None:
        spec["budget"] = budget
    return {"key": key, "spec": spec}


def plan(**change):
    data = {"schema": "praxis.plan", "schema_version": 1,
            "nodes": [node("build"), node("test")],
            "dependencies": [{"prerequisite": "build", "dependent": "test"}],
            "requested_grants": [{"node": "test", "resource": "filesystem", "actions": ["read"],
                                  "scope": "/srv/data/reports"}]}
    data.update(change)
    return json.dumps(data)


@pytest.mark.parametrize("raw,field,reason", [
    (plan(surprise=1), "plan", "unknown_field"),
    (plan(schema="other"), "schema", "unknown_schema"),
    (plan(schema_version=2), "schema_version", "unsupported_version"),
    (plan(nodes=[]), "nodes", "expected 1..256 nodes"),
    (plan(nodes=[node("Build")]), "nodes[0].key", "expected_lowercase_key"),
    (plan(nodes=[node("a"), node("a")], dependencies=[], requested_grants=[]), "nodes", "duplicate_key"),
    (plan(nodes=[{"key": "a", "spec": {"objective": "a", "executor": "local",
                                       "capabilities": [{"resource": "secret"}]}}], dependencies=[],
          requested_grants=[]), "nodes[0].spec.capabilities", "authority_in_spec"),
    (plan(nodes=[{"key": "a", "spec": {"objective": "a", "executor": "local", "extra": 1}}], dependencies=[],
          requested_grants=[]), "nodes[0].spec", "schema: malformed, missing, or unknown fields"),
    (plan(dependencies=[{"prerequisite": "build", "dependent": "test"},
                        {"prerequisite": "test", "dependent": "build"}]), "dependencies", "dependency_cycle"),
    (plan(dependencies=[{"prerequisite": "build", "dependent": "ghost"}]), "dependencies", "invalid_graph"),
    (plan(dependencies=[{"prerequisite": "build", "dependent": "test", "policy": "x"}]), "dependencies[0]",
     "unknown_field"),
    (plan(requested_grants=[{"node": "test", "resource": "executor", "actions": ["shell"], "scope": "local"}]),
     "requested_grants[0]", "invalid_grant_request"),
    (plan(requested_grants=[{"node": "ghost", "resource": "secret", "actions": ["read"], "scope": "k"}]),
     "requested_grants[0].node", "unknown_node"),
])
def test_plan_schema_is_closed(raw, field, reason):
    with pytest.raises(PlanError) as error:
        parse_plan(raw)
    assert (error.value.field, error.value.reason) == (field, reason)


def test_plan_round_trips_and_builds_its_graph():
    parsed = parse_plan(plan())
    assert Plan.from_json(parsed.to_json()) == parsed
    assert parsed.graph().topological() == ("build", "test")
    assert "capabilities" not in json.loads(parsed.to_json())["nodes"][0]["spec"]


ALLOWED = ((Resource.FILESYSTEM, frozenset({"read"}), "/srv/data"),)


def validate(raw, budget=None, executors=("local",), allowlist=ALLOWED):
    validator = PlanValidator(lambda: executors, lambda pid: budget, lambda: allowlist)
    files = () if raw is None else (("plan.json", raw.encode()),)
    source = ValidationInput("p", "a", "s", files)
    return asyncio.run(validator.validate(source, Check("plan", "plan")))


@pytest.mark.parametrize("raw,budget,reason", [
    (None, None, "plan_missing"),
    ("{", None, "invalid_plan"),
    (plan(nodes=[node("a", executor="remote")], dependencies=[], requested_grants=[]), None,
     "unregistered_executor:a"),
    (plan(nodes=[node("a", contract={})], dependencies=[], requested_grants=[]), None, "empty_contract:a"),
    (plan(nodes=[node("a", budget={"wall_milliseconds": 800}), node("b", budget={"wall_milliseconds": 300})],
          dependencies=[], requested_grants=[]), ResourceBudget(wall_milliseconds=1000),
     "budget_exceeds_plan:wall_milliseconds"),
    (plan(nodes=[node("a", budget={"wall_milliseconds": 100}), node("b")], dependencies=[], requested_grants=[]),
     ResourceBudget(wall_milliseconds=1000), "budget_exceeds_plan:wall_milliseconds"),
    (plan(requested_grants=[{"node": "test", "resource": "filesystem", "actions": ["write"],
                             "scope": "/srv/data"}]), None, "grant_not_allowed:test"),
    (plan(requested_grants=[{"node": "test", "resource": "filesystem", "actions": ["read"],
                             "scope": "/etc"}]), None, "grant_not_allowed:test"),
])
def test_plan_validator_rejects_unsafe_plans(raw, budget, reason):
    result = validate(raw, budget)
    assert (result.status, result.reason) == (CheckStatus.FAIL, reason)


def test_plan_validator_accepts_a_plan_within_its_bounds():
    within = plan(nodes=[node("a", budget={"wall_milliseconds": 400}), node("b", budget={"wall_milliseconds": 600})],
                  dependencies=[], requested_grants=[])
    assert validate(within, ResourceBudget(wall_milliseconds=1000)).status == CheckStatus.PASS
    assert validate(plan()).status == CheckStatus.PASS


def host_config(tmp_path, allowlist=None):
    return parse_host_config({
        "data_dir": str(tmp_path / "data"), "executors": ["local"],
        "clients": [
            {"id": "modulo", "token_sha256": token_digest(MODULO), "delegate": True,
             "roles": ["submit", "read", "control", "approve"]},
            {"id": "review", "token_sha256": token_digest(REVIEW), "delegate": True, "roles": ["read", "approve"]},
        ],
        "planning": {"grant_allowlist": allowlist if allowlist is not None else [
            {"resource": "filesystem", "actions": ["read"], "scope": "/srv/data"}]},
    })


async def call(app, method, path, token, data=None, user=None):
    messages = []
    headers = [(b"authorization", b"Bearer " + token.encode())]
    if user is not None:
        headers.append((b"x-praxis-on-behalf-of", user.encode()))

    async def receive():
        return {"type": "http.request", "body": json.dumps(data or {}).encode()}

    async def send(message):
        messages.append(message)
    await app({"type": "http", "method": method, "path": path, "headers": headers}, receive, send)
    return messages[0]["status"], json.loads(messages[1]["body"])


def planner(raw, checks=True):
    contract = {"required_outputs": ["plan.json"]}
    if checks:
        contract["validators"] = [{"check_id": "plan", "validator": "plan"}]
    return {"objective": "plan the work", "executor": "local", "contract": contract,
            "inputs": {"argv": [sys.executable, "-c", f"from pathlib import Path\nPath('plan.json').write_text({raw!r})"]}}


async def plan_process(host, raw, checks=True):
    status, body = await call(host.app, "POST", "/v1/processes", MODULO, planner(raw, checks), user="alice")
    assert status == 202, body
    await host.kernel.tasks[body["process_id"]]
    return body["process_id"]


async def settle(host, nodes):
    for _ in range(200):
        states = [host.kernel.processes[n].state.value for n in nodes]
        if all(s in {"completed", "failed", "cancelled"} for s in states):
            return states
        await asyncio.sleep(0.02)
    raise AssertionError(states)


def test_verified_plan_is_materialized_once_and_runs_in_order(tmp_path):
    async def exercise():
        host = build_host(host_config(tmp_path))
        process_id = await plan_process(host, plan())
        assert host.kernel.processes[process_id].state.value == "completed"
        path = f"/v1/processes/{process_id}/plan/materialize"
        status, _ = await call(host.app, "POST", path, REVIEW, user="carol")
        assert status == 403  # neither the owner nor a configured approver
        status, body = await call(host.app, "POST", path, MODULO, user="alice")
        assert status == 200 and not body["duplicate"], body
        nodes = body["nodes"]
        assert body["started"] == [nodes["build"]]  # test waits for build
        assert body["grants_issued"] == [{"node": "test", "resource": "filesystem", "actions": ["read"],
                                          "scope": "/srv/data/reports"}]
        assert await settle(host, [nodes["build"], nodes["test"]]) == ["completed", "completed"]
        assert host.kernel.authority.authorize(nodes["test"], Resource.FILESYSTEM, "read", "/srv/data/reports").allowed
        status, again = await call(host.app, "POST", path, MODULO, user="alice")
        assert status == 200 and again["duplicate"] and again["nodes"] == nodes
        from praxis.client import Client
        from test_client import ASGITransport

        class Authenticated(ASGITransport):
            async def request(self, method, path, body=None, headers=None):
                return await super().request(method, path, body, {**(headers or {}),
                                             "Authorization": "Bearer " + MODULO, "X-Praxis-On-Behalf-Of": "alice"})
        via_client = await Client(Authenticated(host.app)).materialize_plan(process_id)
        assert via_client["duplicate"] and via_client["nodes"] == nodes
        assert len(host.kernel.processes) == 3
        # The submitter owns the nodes, so they can inspect them.
        status, view = await call(host.app, "GET", f"/v1/processes/{nodes['test']}", MODULO, user="alice")
        assert status == 200 and view["state"] == "completed"
        [event] = [e for e in host.kernel.events if e.type == "plan.materialized"]
        assert event.payload["actor"] == "modulo/alice"
        host.close()
    asyncio.run(exercise())


def test_failed_prerequisite_cancels_dependents_that_require_success(tmp_path):
    async def exercise():
        host = build_host(host_config(tmp_path))
        failing = plan(nodes=[node("build", script="raise SystemExit(1)"), node("test")],
                       dependencies=[{"prerequisite": "build", "dependent": "test", "on_failure": "fail"}],
                       requested_grants=[])
        process_id = await plan_process(host, failing)
        status, body = await call(host.app, "POST", f"/v1/processes/{process_id}/plan/materialize", MODULO,
                                  user="alice")
        assert status == 200
        nodes = body["nodes"]
        assert await settle(host, [nodes["build"], nodes["test"]]) == ["failed", "cancelled"]
        host.close()
    asyncio.run(exercise())


def test_unverified_or_unvalidated_plans_cannot_be_materialized(tmp_path):
    async def exercise():
        host = build_host(host_config(tmp_path))
        rejected = await plan_process(host, plan(nodes=[node("a", contract={})], dependencies=[],
                                                 requested_grants=[]))
        assert host.kernel.processes[rejected].state.value == "failed"
        status, body = await call(host.app, "POST", f"/v1/processes/{rejected}/plan/materialize", MODULO,
                                  user="alice")
        assert (status, body["error"]["code"]) == (409, "plan_not_verified")
        # A plan.json output without the plan validator in its contract is not a plan.
        unchecked = await plan_process(host, plan(), checks=False)
        assert host.kernel.processes[unchecked].state.value == "completed"
        status, body = await call(host.app, "POST", f"/v1/processes/{unchecked}/plan/materialize", MODULO,
                                  user="alice")
        assert (status, body["error"]["code"]) == (409, "plan_not_validated")
        assert len(host.kernel.processes) == 2
        host.close()
    asyncio.run(exercise())


def test_materialization_issues_only_currently_allowed_grants(tmp_path):
    async def exercise():
        host = build_host(host_config(tmp_path))
        process_id = await plan_process(host, plan())
        assert "planning allowlist" in host.reload(host_config(tmp_path, allowlist=[]))
        status, body = await call(host.app, "POST", f"/v1/processes/{process_id}/plan/materialize", MODULO,
                                  user="alice")
        assert status == 200 and body["grants_issued"] == [] and len(body["grants_refused"]) == 1
        test_node = body["nodes"]["test"]
        assert not host.kernel.authority.authorize(test_node, Resource.FILESYSTEM, "read", "/srv/data/reports").allowed
        await settle(host, list(body["nodes"].values()))
        host.close()
    asyncio.run(exercise())


def test_restart_continues_a_materialized_plan(tmp_path):
    async def exercise():
        config = host_config(tmp_path)
        host = build_host(config)
        process_id = await plan_process(host, plan())
        # Materialize without starting, as if the host stopped right after.
        result = host.control.planning.materialize(process_id, "modulo/alice", "modulo/alice")
        host.close()
        restarted = build_host(config)
        messages = []

        async def receive():
            return {"type": "lifespan.startup"} if not messages else {"type": "lifespan.shutdown"}

        async def send(message):
            messages.append(message)
        await restarted.app({"type": "lifespan"}, receive, send)
        nodes = result["nodes"]
        assert await settle(restarted, [nodes["build"], nodes["test"]]) == ["completed", "completed"]
        restarted.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("entry,field", [
    ({"resource": "executor", "actions": ["shell"], "scope": "local"}, "resource"),
    ({"resource": "filesystem", "actions": ["read"], "scope": "*"}, "scope"),
    ({"resource": "filesystem", "actions": ["write"], "scope": "/"}, "scope"),
    ({"resource": "filesystem", "actions": ["read"], "scope": "relative"}, "scope"),
    ({"resource": "network", "actions": ["listen"], "scope": "example.com"}, "actions"),
    ({"resource": "secret", "actions": ["read"], "scope": "a*"}, "scope"),
    ({"resource": "secret", "actions": ["read"], "scope": "k", "extra": 1}, None),
])
def test_grant_allowlist_cannot_be_wider_than_host_policy(tmp_path, entry, field):
    with pytest.raises(HostConfigError) as error:
        host_config(tmp_path, allowlist=[entry])
    expected = "planning.grant_allowlist[0]" + ("" if field is None else "." + field)
    assert error.value.field == expected
