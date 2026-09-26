import asyncio
import base64
import json
import sys

import pytest

from praxis.executors.local import LocalProcessExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.capabilities import Resource
from praxis.kernel.contracts import Check, Contract
from praxis.kernel.effects import EffectKind
from praxis.kernel.lifecycle import State
from praxis.kernel.proposals import (
    PROPOSAL_PREFIX, ProposalError, parse_proposal, proposals_from_files, published,
)
from praxis.kernel.runtime import Kernel
from praxis.kernel.spec import ProcessSpec
from praxis.storage.sqlite import SQLiteStore
from praxis.validators.protocol import CheckStatus, FakeValidator
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.transaction import CanonicalDirectory


def proposal(**change):
    data = {"schema": "praxis.effect-proposal", "schema_version": 1, "kind": "message_send",
            "target": "ops", "payload": {"text": "done"}}
    data.update(change)
    return json.dumps(data)


@pytest.mark.parametrize("raw,reason", [
    (proposal(effect_id="x"), "unknown_field"),
    (proposal(authority={}), "unknown_field"),
    (proposal(status="approved"), "unknown_field"),
    (proposal(schema="other"), "unknown_schema"),
    (proposal(schema_version=2), "unsupported_version"),
    (proposal(kind="external"), "kind_not_proposable"),
    (proposal(kind="teleport"), "unknown_kind"),
    (proposal(target=""), "invalid_target"),
    (proposal(payload={"text": 1}), "invalid_payload"),
    (proposal(payload={"text": "x", "extra": 1}), "invalid_payload"),
    (proposal(kind="file_write", target="relative", payload={"content_base64": "eA=="}), "invalid_target"),
    (proposal(kind="file_write", target="/tmp/x", payload={"content_base64": "!!"}), "invalid_payload"),
    (proposal(kind="artifact_publish", payload={"artifact_path": "../x", "media_type": "text/plain"}),
     "invalid_artifact_path"),
    (proposal(kind="artifact_publish", payload={"artifact_path": ".praxis/effects/a.json", "media_type": "t"}),
     "invalid_artifact_path"),
    ('{"schema": "praxis.effect-proposal", "schema": "praxis.effect-proposal"}', "invalid_json"),
    ("[]", "invalid_json"),
])
def test_proposal_schema_is_closed(raw, reason):
    with pytest.raises(ProposalError) as error:
        parse_proposal("p.json", raw)
    assert error.value.reason == reason


def test_proposals_are_all_or_nothing_and_bounded():
    good = (PROPOSAL_PREFIX + "a.json", proposal().encode())
    assert [p.kind for p in proposals_from_files((good,))] == [EffectKind.MESSAGE_SEND]
    for bad in ((PROPOSAL_PREFIX + "b.txt", b"{}"), (PROPOSAL_PREFIX + "nested/b.json", proposal().encode()),
                (PROPOSAL_PREFIX + "b.json", b"\xff")):
        with pytest.raises(ProposalError):
            proposals_from_files((good, bad))
    many = tuple((f"{PROPOSAL_PREFIX}{i}.json", proposal().encode()) for i in range(65))
    with pytest.raises(ProposalError, match="too_many"):
        proposals_from_files(many)
    assert not published(".praxis/") and not published(".praxis/effects/a.json") and published("praxis.txt")


def test_idempotency_key_is_deterministic_and_artifacts_bind_to_the_snapshot():
    parsed = parse_proposal(PROPOSAL_PREFIX + "a.json", proposal(
        kind="artifact_publish", target="bucket", payload={"artifact_path": "out.bin", "media_type": "a/b"}))
    effect = parsed.to_effect("p", "a", "snap", {"out.bin": b"bytes"})
    again = parsed.to_effect("p", "a", "snap", {"out.bin": b"bytes"})
    assert effect.idempotency_key == again.idempotency_key != parsed.to_effect("p", "b", "snap", {"out.bin": b"x"}).idempotency_key
    assert effect.payload["artifact_ref"] == "snap:out.bin"
    assert effect.payload["metadata"]["size"] == 5
    with pytest.raises(ProposalError, match="artifact_not_in_snapshot"):
        parsed.to_effect("p", "a", "snap", {})


class Sink:
    def __init__(self):
        self.calls = []

    def propose(self, process_id, effects):
        self.calls.append((process_id, effects))


WRITE = """
from pathlib import Path
Path('.praxis/effects').mkdir(parents=True)
Path('.praxis/effects/1.json').write_text({first!r})
Path('.praxis/effects/2.json').write_text({second!r})
Path('output').write_text('done')
"""


def run(tmp_path, status=CheckStatus.PASS, first=None, second=None, canonical=False):
    async def exercise():
        workspaces = LocalWorkspaces(tmp_path / "ws")
        authority = Authority(execution_defaults=frozenset({"local"}))
        sink = Sink()
        kernel = Kernel(SQLiteStore(tmp_path / "runtime.db"), workspaces,
                        {"local": LocalProcessExecutor(workspaces)}, authority=authority,
                        validators={"fake": FakeValidator(status)}, proposal_sink=sink)
        contract = Contract(required_outputs=("output",), validators=(Check("check", "fake"),))
        script = WRITE.format(first=first or proposal(), second=second or proposal(
            kind="file_write", target="/tmp/out", payload={"content_base64": base64.b64encode(b"x").decode()}))
        target = CanonicalDirectory(tmp_path / "canonical") if canonical else None
        process = kernel.create(ProcessSpec("work", "local", inputs={"argv": [sys.executable, "-c", script]},
                                            contract=json.loads(contract.to_json())), canonical=target)
        if target is not None:
            authority.issue(process.process_id, Resource.WORKSPACE, frozenset({"commit"}), process.process_id)
            authority.issue(process.process_id, Resource.FILESYSTEM, frozenset({"read", "write"}), str(target.root))
        kernel.start(process.process_id)
        await kernel.tasks[process.process_id]
        return kernel, process, sink, target
    return asyncio.run(exercise())


def test_verified_process_hands_its_proposals_to_the_sink(tmp_path):
    kernel, process, sink, _ = run(tmp_path)
    assert process.state == State.COMPLETED
    [(identity, effects)] = sink.calls
    assert identity == process.process_id
    assert [e.kind for e in effects] == [EffectKind.MESSAGE_SEND, EffectKind.FILE_WRITE]
    assert all(e.attempt_id == process.attempt_id for e in effects)
    assert [e.type for e in kernel.events].count("effects.proposed") == 1
    # The journal replays the same effects, with the same keys, after a restart.
    [(replayed_id, replayed)] = kernel.proposals()
    assert replayed_id == process.process_id
    assert [e.idempotency_key for e in replayed] == [e.idempotency_key for e in effects]


def test_failed_verification_stages_nothing(tmp_path):
    kernel, process, sink, _ = run(tmp_path, status=CheckStatus.FAIL)
    assert process.state == State.FAILED
    assert not sink.calls and not kernel.proposals()
    assert "effects.proposed" not in [e.type for e in kernel.events]


def test_one_invalid_proposal_stages_none(tmp_path):
    kernel, process, sink, _ = run(tmp_path, second=proposal(effect_id="forged"))
    assert process.state == State.COMPLETED
    assert not sink.calls
    [rejected] = [e for e in kernel.events if e.type == "effects.proposals_rejected"]
    assert rejected.payload["reason"] == "unknown_field"


def test_proposals_never_reach_the_canonical_directory(tmp_path):
    _, process, sink, canonical = run(tmp_path, canonical=True)
    assert process.state == State.COMPLETED and sink.calls
    assert (canonical.path / "output").read_text() == "done"
    assert not (canonical.path / ".praxis").exists()
