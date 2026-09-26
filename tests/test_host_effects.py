"""The effect pipeline and approval policy through the deployment host (ADR 0001, 0002)."""

import asyncio
import base64
import json
import sys

import pytest

from praxis.host.app import build_host
from praxis.host.auth import token_digest
from praxis.host.config import HostConfigError, parse_host_config
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.effect_service import EffectReceipt, EffectService
from praxis.kernel.effects import EffectKind, EffectStatus, message_send
from praxis.kernel.process import Process
from praxis.kernel.spec import ProcessSpec
from praxis.storage.sqlite import SQLiteStore

MODULO, REVIEW = "m" * 40, "r" * 40


def host_config(tmp_path, rules=None, **extra):
    data = {
        "data_dir": str(tmp_path / "data"),
        "executors": ["local"],
        "clients": [
            {"id": "modulo", "token_sha256": token_digest(MODULO), "delegate": True,
             "roles": ["submit", "read", "control", "approve", "publish", "health"]},
            {"id": "review", "token_sha256": token_digest(REVIEW), "delegate": True, "roles": ["read", "approve"]},
        ],
        "effect_policy": rules if rules is not None else [
            {"kind": "message_send", "target": "ops", "policy": "human",
             "approvers": ["review/bob", "modulo/alice"], "separate_submitter": True},
            {"kind": "file_write", "target": "/tmp/praxis-*", "policy": "auto", "expires_seconds": 600},
            {"kind": "git_commit", "target": "*", "policy": "deny"},
        ],
    }
    data.update(extra)
    return parse_host_config(data)


def proposal(kind, target, payload):
    return json.dumps({"schema": "praxis.effect-proposal", "schema_version": 1, "kind": kind,
                       "target": target, "payload": payload})


PROPOSALS = {
    "1.json": proposal("message_send", "ops", {"text": "shipped"}),
    "2.json": proposal("file_write", "/tmp/praxis-out", {"content_base64": base64.b64encode(b"x").decode()}),
    "3.json": proposal("git_commit", "main", {"message": "m", "expected_head": "h"}),
    "4.json": proposal("artifact_publish", "bucket", {"artifact_path": "output", "media_type": "text/plain"}),
}


def spec(proposals=None):
    script = ("from pathlib import Path\nPath('.praxis/effects').mkdir(parents=True)\n"
              + "".join(f"Path('.praxis/effects/{name}').write_text({raw!r})\n"
                        for name, raw in (proposals or PROPOSALS).items())
              + "Path('output').write_text('done')\n")
    return {"objective": "work", "executor": "local", "inputs": {"argv": [sys.executable, "-c", script]},
            "contract": {"required_outputs": ["output"]}}


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


class Sender:
    def __init__(self):
        self.sent = {}
        self.fail = False

    async def apply(self, effect):
        self.sent[effect.idempotency_key] = effect.payload["text"]
        if self.fail:
            raise ConnectionError("lost")
        return EffectReceipt(True, "delivered", "m-1")

    async def lookup(self, key):
        return EffectReceipt(True, "delivered", "m-1") if key in self.sent else EffectReceipt(False, "not_delivered")


async def submit(host, proposals=None):
    status, body = await call(host.app, "POST", "/v1/processes", MODULO, spec(proposals), user="alice")
    assert status == 202, body
    await host.kernel.tasks[body["process_id"]]
    return body["process_id"]


def by_kind(host, process_id):
    effects = {}
    for event in host.store.read_events(process_id):
        if event.event.type.startswith("effect."):
            effect = json.loads(event.event.payload["effect"])
            effects[effect["kind"]] = effect
    return effects


def test_policy_matrix_stages_each_effect_by_its_rule(tmp_path):
    async def exercise():
        host = build_host(host_config(tmp_path), effect_adapters={EffectKind.MESSAGE_SEND: Sender()})
        process_id = await submit(host)
        assert host.kernel.processes[process_id].state.value == "completed"
        effects = by_kind(host, process_id)
        assert effects["message_send"]["status"] == "proposed"      # human: waits for an approver
        assert effects["file_write"]["status"] == "approved"        # auto
        assert effects["git_commit"]["status"] == "rejected"        # deny
        assert effects["artifact_publish"]["status"] == "rejected"  # no rule: deny by default
        status, body = await call(host.app, "GET", f"/v1/processes/{process_id}/approvals", MODULO, user="alice")
        assert status == 200 and [e["kind"] for e in body["approvals"]] == ["message_send"]
        records = host.control.effects.approvals(effects["file_write"]["effect_id"])
        assert records[-1].actor == process_id and records[-1].expires_at is not None
        assert records[-1].reason == "policy:file_write:/tmp/praxis-*"
        host.close()
    asyncio.run(exercise())


def test_configured_approver_decides_and_the_submitter_cannot(tmp_path):
    async def exercise():
        sender = Sender()
        host = build_host(host_config(tmp_path), effect_adapters={EffectKind.MESSAGE_SEND: sender})
        process_id = await submit(host)
        attempt = host.kernel.processes[process_id].attempt_id
        effect = by_kind(host, process_id)["message_send"]
        decision = {"effect_id": effect["effect_id"], "version": effect["version"], "attempt_id": attempt,
                    "approved": True, "reason": "looks right"}
        # The submitter is a named approver, but separate_submitter excludes them.
        status, body = await call(host.app, "POST", f"/v1/processes/{process_id}/approvals", MODULO,
                                  decision, user="alice")
        assert (status, body["error"]["code"]) == (409, "approval_rejected")
        # A person of an approving client who is not named in the rule is refused.
        status, _ = await call(host.app, "POST", f"/v1/processes/{process_id}/approvals", REVIEW,
                               decision, user="carol")
        assert status == 403
        status, body = await call(host.app, "POST", f"/v1/processes/{process_id}/approvals", REVIEW,
                                  decision, user="bob")
        assert status == 200 and body["effect"]["status"] == "approved"
        assert body["approvals"][-1]["actor"] == "review/bob"
        approved = body["effect"]
        # Applying needs the publish role, the current attempt and the current version.
        apply_path = f"/v1/processes/{process_id}/effects/{approved['effect_id']}/apply"
        status, _ = await call(host.app, "POST", apply_path, REVIEW, {"attempt_id": attempt,
                                                                       "version": approved["version"]}, user="bob")
        assert status == 403
        status, body = await call(host.app, "POST", apply_path, MODULO, {"attempt_id": "old",
                                                                         "version": approved["version"]}, user="alice")
        assert (status, body["error"]["code"]) == (409, "stale_process_attempt")
        status, body = await call(host.app, "POST", apply_path, MODULO, {"attempt_id": attempt,
                                                                         "version": approved["version"] - 1}, user="alice")
        assert status == 409 and not sender.sent
        status, body = await call(host.app, "POST", apply_path, MODULO, {"attempt_id": attempt,
                                                                         "version": approved["version"]}, user="alice")
        assert status == 200 and body["receipt"]["applied"] and body["effect"]["status"] == "applied"
        status, again = await call(host.app, "POST", apply_path, MODULO, {"attempt_id": attempt,
                                                                          "version": approved["version"]}, user="alice")
        assert status == 200 and again["receipt"]["applied"] and len(sender.sent) == 1
        host.close()
    asyncio.run(exercise())


def test_uncertain_effect_is_reported_across_restart_and_only_reconciled(tmp_path):
    async def exercise():
        sender = Sender()
        sender.fail = True
        config = host_config(tmp_path)
        host = build_host(config, effect_adapters={EffectKind.MESSAGE_SEND: sender})
        process_id = await submit(host)
        attempt = host.kernel.processes[process_id].attempt_id
        effect = by_kind(host, process_id)["message_send"]
        approved = host.control.effects.resolve_approval(effect["effect_id"], effect["version"], True,
                                                         actor="review/bob", reason="ok")
        status, body = await call(host.app, "POST", f"/v1/processes/{process_id}/effects/{approved.effect_id}/apply",
                                  MODULO, {"attempt_id": attempt, "version": approved.version}, user="alice")
        assert status == 409 and body["receipt"]["reason"] == "application_uncertain"
        host.close()
        restarted = build_host(config, effect_adapters={EffectKind.MESSAGE_SEND: sender})
        status, health = await call(restarted.app, "GET", "/v1/health", MODULO)
        assert {"process_id": process_id, "reason": "effect_uncertain"} in health["blocked_processes"]
        status, view = await call(restarted.app, "GET", f"/v1/processes/{process_id}", MODULO, user="alice")
        assert view["blocking_reason"] == "effect_uncertain"
        current = restarted.control.effects.load(approved.effect_id)
        status, body = await call(restarted.app, "POST",
                                  f"/v1/processes/{process_id}/effects/{approved.effect_id}/apply", MODULO,
                                  {"attempt_id": attempt, "version": current.version}, user="alice")
        assert body["receipt"]["reason"] == "application_uncertain" and len(sender.sent) == 1
        status, body = await call(restarted.app, "POST",
                                  f"/v1/processes/{process_id}/effects/{approved.effect_id}/reconcile", MODULO,
                                  {"attempt_id": attempt}, user="alice")
        assert status == 200 and body["effect"]["status"] == "applied" and len(sender.sent) == 1
        status, view = await call(restarted.app, "GET", f"/v1/processes/{process_id}", MODULO, user="alice")
        assert view["blocking_reason"] is None
        restarted.close()
    asyncio.run(exercise())


def test_restart_replays_proposals_idempotently_and_keeps_approvers(tmp_path):
    async def exercise():
        config = host_config(tmp_path)
        host = build_host(config, effect_adapters={EffectKind.MESSAGE_SEND: Sender()})
        process_id = await submit(host)
        before = by_kind(host, process_id)
        host.close()
        restarted = build_host(config, effect_adapters={EffectKind.MESSAGE_SEND: Sender()})
        after = by_kind(restarted, process_id)
        assert {k: v["effect_id"] for k, v in after.items()} == {k: v["effect_id"] for k, v in before.items()}
        with restarted.store._transaction() as connection:
            assert connection.execute("SELECT count(*) FROM effects").fetchone()[0] == 4
        effect = after["message_send"]
        decision = {"effect_id": effect["effect_id"], "version": effect["version"],
                    "attempt_id": restarted.kernel.processes[process_id].attempt_id,
                    "approved": False, "reason": "not now"}
        status, body = await call(restarted.app, "POST", f"/v1/processes/{process_id}/approvals", REVIEW,
                                  decision, user="bob")
        assert status == 200 and body["effect"]["status"] == "rejected"
        restarted.close()
    asyncio.run(exercise())


def test_reload_removes_an_approver(tmp_path):
    async def exercise():
        host = build_host(host_config(tmp_path), effect_adapters={EffectKind.MESSAGE_SEND: Sender()})
        process_id = await submit(host)
        rules = [{"kind": "message_send", "target": "ops", "policy": "human", "approvers": ["review/dave"]}]
        assert "effect policy" in host.reload(host_config(tmp_path, rules))
        effect = by_kind(host, process_id)["message_send"]
        decision = {"effect_id": effect["effect_id"], "version": effect["version"],
                    "attempt_id": host.kernel.processes[process_id].attempt_id, "approved": True, "reason": "ok"}
        status, _ = await call(host.app, "POST", f"/v1/processes/{process_id}/approvals", REVIEW, decision, user="bob")
        assert status == 403
        status, body = await call(host.app, "POST", f"/v1/processes/{process_id}/approvals", REVIEW,
                                  decision, user="dave")
        assert status == 200 and body["effect"]["status"] == "approved"
        with pytest.raises(HostConfigError):
            host.reload(host_config(tmp_path, effects=[{"kind": "git_commit", "adapter": "git",
                                                        "repositories": {"main": str(tmp_path)}}]))
        host.close()
    asyncio.run(exercise())


def test_irreversible_adapter_without_lookup_is_refused(tmp_path):
    class SendOnly:
        async def apply(self, effect):
            return EffectReceipt(True, "sent")
    with pytest.raises(ValueError, match="lookup"):
        build_host(host_config(tmp_path), effect_adapters={EffectKind.MESSAGE_SEND: SendOnly()})


@pytest.mark.parametrize("change,field", [
    ({"effect_policy": [{"kind": "message_send", "target": "ops", "policy": "auto"}]}, "effect_policy[0].policy"),
    ({"effect_policy": [{"kind": "artifact_publish", "target": "*", "policy": "auto"}]}, "effect_policy[0].policy"),
    ({"effect_policy": [{"kind": "message_send", "target": "ops", "policy": "human"}]}, "effect_policy[0].approvers"),
    ({"effect_policy": [{"kind": "file_write", "target": "/a", "policy": "auto", "approvers": ["x"]}]},
     "effect_policy[0].approvers"),
    ({"effect_policy": [{"kind": "external", "target": "*", "policy": "deny"}]}, "effect_policy[0].kind"),
    ({"effect_policy": [{"kind": "git_commit", "target": "a*b", "policy": "deny"}]}, "effect_policy[0].target"),
    ({"effect_policy": [{"kind": "git_commit", "target": "*", "policy": "deny", "surprise": 1}]}, "effect_policy[0]"),
    ({"effect_policy": [{"kind": "git_commit", "target": "*", "policy": "deny"}] * 2}, "effect_policy"),
    ({"effect_policy": [{"kind": "message_send", "target": "o", "policy": "human", "approvers": ["Bad Name"]}]},
     "effect_policy[0].approvers"),
    ({"effects": [{"kind": "git_commit", "adapter": "webhook", "repositories": {"m": "/r"}}]}, "effects[0].adapter"),
    ({"effects": [{"kind": "git_commit", "adapter": "git", "repositories": {"m": "relative"}}]},
     "effects[0].repositories.m"),
    ({"effects": [{"kind": "message_send", "adapter": "webhook", "base_url": "http://hooks.example"}]},
     "effects[0].base_url"),
    ({"effects": [{"kind": "message_send", "adapter": "webhook", "base_url": "https://h.example",
                   "prefix": "/x"}]}, "effects[0]"),
    ({"effects": [{"kind": "message_send", "adapter": "webhook", "base_url": "https://h.example",
                   "token_env": "A", "token_file": "/t"}]}, "effects[0].token_env"),
    ({"effects": [{"kind": "file_write", "adapter": "x"}]}, "effects[0].kind"),
])
def test_effect_configuration_is_closed(tmp_path, change, field):
    with pytest.raises(HostConfigError) as error:
        host_config(tmp_path, **change)
    assert error.value.field == field


def test_most_specific_rule_wins(tmp_path):
    config = host_config(tmp_path, [
        {"kind": "file_write", "target": "*", "policy": "deny"},
        {"kind": "file_write", "target": "/tmp/*", "policy": "human", "approvers": ["review"]},
        {"kind": "file_write", "target": "/tmp/a*", "policy": "auto"},
        {"kind": "file_write", "target": "/tmp/ab", "policy": "deny"},
    ])
    assert config.effect_rule("file_write", "/tmp/ab").policy == "deny"
    assert config.effect_rule("file_write", "/tmp/ac").policy == "auto"
    assert config.effect_rule("file_write", "/tmp/b").policy == "human"
    assert config.effect_rule("file_write", "/etc/x").policy == "deny"
    assert config.effect_rule("message_send", "/tmp/ab") is None


def test_a_person_cannot_approve_without_an_approver_policy(tmp_path):
    store = SQLiteStore(tmp_path / "runtime.db")
    process = Process(ProcessSpec("work", "fake"))
    store.save(process)
    authority = Authority()
    authority.configure_process(process.process_id)
    authority.issue(process.process_id, Resource.EFFECT, frozenset({"stage"}), "ops")
    authority.issue(process.process_id, Resource.EFFECT, frozenset({"apply"}), "message:ops")
    service = EffectService(store, authority)
    staged = service.stage(message_send(process.process_id, process.attempt_id, "ops", "hi"))
    with pytest.raises((PermissionError, ValueError)):
        service.resolve_approval(staged.effect_id, staged.version, True, actor="modulo/alice", reason="ok")
    assert staged.status == service.load(staged.effect_id).status == EffectStatus.PROPOSED


def test_client_applies_and_reconciles_effects(tmp_path):
    from praxis.client import Client, ClientAPIError
    from test_client import ASGITransport

    class Authenticated(ASGITransport):
        async def request(self, method, path, body=None, headers=None):
            return await super().request(method, path, body, {**(headers or {}),
                                         "Authorization": "Bearer " + MODULO, "X-Praxis-On-Behalf-Of": "alice"})

    async def exercise():
        sender = Sender()
        host = build_host(host_config(tmp_path), effect_adapters={EffectKind.MESSAGE_SEND: sender})
        process_id = await submit(host)
        attempt = host.kernel.processes[process_id].attempt_id
        effect = by_kind(host, process_id)["message_send"]
        approved = host.control.effects.resolve_approval(effect["effect_id"], effect["version"], True,
                                                         actor="review/bob", reason="ok")
        client = Client(Authenticated(host.app))
        with pytest.raises(ClientAPIError) as error:
            await client.apply_effect(process_id, attempt, approved.effect_id, approved.version + 1)
        assert (error.value.status, error.value.code) == (409, "effect_not_approved_or_stale")
        applied = await client.apply_effect(process_id, attempt, approved.effect_id, approved.version)
        assert applied.applied and applied.external_id == "m-1" and applied.effect["status"] == "applied"
        reconciled = await client.reconcile_effect(process_id, attempt, approved.effect_id)
        assert reconciled.applied and len(sender.sent) == 1
        host.close()
    asyncio.run(exercise())


def test_deny_rejects_even_when_the_process_already_holds_the_authority(tmp_path):
    rules = [{"kind": "message_send", "target": "ops", "policy": "deny"}]
    host = build_host(host_config(tmp_path, rules), effect_adapters={EffectKind.MESSAGE_SEND: Sender()})
    process = host.kernel.create(ProcessSpec("work", "local"), submission_actor="modulo/alice")
    # Authority granted for another reason: staging alone would now succeed.
    for action, scope in (("stage", "ops"), ("apply", "message:ops")):
        host.kernel.authority.issue(process.process_id, Resource.EFFECT, frozenset({action}), scope)
    effect = message_send(process.process_id, process.attempt_id, "ops", "hi")
    host.control.policy.propose(process.process_id, (effect,))
    stored = host.control.effects.find(effect.idempotency_key)
    assert stored.status == EffectStatus.REJECTED
    assert host.control.effects.approvals(stored.effect_id)[-1].reason == "policy:deny"
    host.close()


def test_approvers_see_only_the_effects_they_may_decide(tmp_path):
    async def exercise():
        rules = [{"kind": "message_send", "target": "ops", "policy": "human", "approvers": ["review/bob"]},
                 {"kind": "message_send", "target": "legal", "policy": "human", "approvers": ["review/erin"]}]
        host = build_host(host_config(tmp_path, rules), effect_adapters={EffectKind.MESSAGE_SEND: Sender()})
        process_id = await submit(host, {"1.json": PROPOSALS["1.json"],
                                         "2.json": proposal("message_send", "legal", {"text": "contract"})})
        path = f"/v1/processes/{process_id}/approvals"
        _, owner = await call(host.app, "GET", path, MODULO, user="alice")
        assert sorted(e["target"] for e in owner["approvals"]) == ["legal", "ops"]
        _, bob = await call(host.app, "GET", path, REVIEW, user="bob")
        assert [e["target"] for e in bob["approvals"]] == ["ops"]
        _, erin = await call(host.app, "GET", path, REVIEW, user="erin")
        assert [e["target"] for e in erin["approvals"]] == ["legal"]
        host.close()
    asyncio.run(exercise())
