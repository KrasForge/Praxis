"""Conformance for every effect adapter: success, refusal, lookup and uncertainty."""

import asyncio
import hashlib
import subprocess
from pathlib import Path

import pytest

import praxis.kernel.runtime
from praxis.effects import IRREVERSIBLE, reconcilable, require_reconcilable
from praxis.effects.artifact import ArtifactPublishAdapter
from praxis.effects.git import GitCommitAdapter
from praxis.effects.webhook import WebhookMessageAdapter
from praxis.kernel.effect_service import EffectReceipt
from praxis.kernel.effects import EffectKind, artifact_publish, git_commit, message_send
from praxis.transport.http import TransportError


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin",
                               "GIT_CONFIG_GLOBAL": "/dev/null"}).stdout.strip()


class Receiver:
    """A webhook receiver that deduplicates by key, optionally losing responses."""

    def __init__(self):
        self.delivered = {}
        self.lose_response = False
        self.refuse = None

    async def request(self, method, path, body=None):
        if method == "POST":
            if self.refuse is not None:
                raise TransportError("http_error", self.refuse)
            self.delivered.setdefault(body["idempotency_key"], body)
            if self.lose_response:
                raise TransportError("transport_unavailable")
            return {"id": "m-" + body["idempotency_key"][:6]}
        key = path.rsplit("/", 1)[1]
        if key not in self.delivered:
            raise TransportError("http_error", 404)
        return {"delivered": True, "id": "m-" + key[:6]}


class Store:
    def __init__(self):
        self.objects = {}
        self.lose_response = False

    async def put(self, path, content, media_type):
        self.objects[path] = content
        if self.lose_response:
            raise TransportError("transport_unavailable")

    async def get(self, path):
        return self.objects.get(path)


class Source:
    def __init__(self, content=b"artifact"):
        self.content = content

    def read(self, effect):
        if effect.payload["artifact_ref"] != "snap:out.bin":
            raise PermissionError("artifact_not_verified")
        return self.content


def artifact_effect():
    effect = artifact_publish("p", "a", "bucket", "snap:out.bin", "application/octet-stream")
    return effect


def build(kind, tmp_path):
    if kind == EffectKind.GIT_COMMIT:
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "a").write_text("1")
        git(repo, "add", "a")
        git(repo, "commit", "-qm", "init")
        adapter = GitCommitAdapter({"main": str(repo)})

        def effect():
            (repo / "a").write_text((repo / "a").read_text() + "1")
            return git_commit("p", "a", "main", "change", git(repo, "rev-parse", "HEAD"))
        return adapter, effect, None
    if kind == EffectKind.MESSAGE_SEND:
        receiver = Receiver()
        return WebhookMessageAdapter(receiver), lambda: message_send("p", "a", "ops", "hi"), receiver
    store = Store()
    digests = {}
    adapter = ArtifactPublishAdapter(store, Source(), digests.get)

    def effect():
        created = artifact_effect()
        digests[created.idempotency_key] = hashlib.sha256(b"artifact").hexdigest()
        return created
    return adapter, effect, store


KINDS = [EffectKind.GIT_COMMIT, EffectKind.MESSAGE_SEND, EffectKind.ARTIFACT_PUBLISH]


@pytest.mark.parametrize("kind", KINDS)
def test_apply_then_lookup_finds_the_same_application(tmp_path, kind):
    adapter, make, _ = build(kind, tmp_path)
    assert adapter.kind == kind and reconcilable(adapter)
    effect = make()
    receipt = asyncio.run(adapter.apply(effect))
    assert receipt.applied
    found = asyncio.run(adapter.lookup(effect.idempotency_key))
    assert isinstance(found, EffectReceipt) and found.applied
    missing = asyncio.run(adapter.lookup("0" * 64))
    assert missing is None or not missing.applied


@pytest.mark.parametrize("kind", [EffectKind.MESSAGE_SEND, EffectKind.ARTIFACT_PUBLISH])
def test_lost_response_raises_and_lookup_resolves_without_resending(tmp_path, kind):
    adapter, make, remote = build(kind, tmp_path)
    remote.lose_response = True
    effect = make()
    with pytest.raises(TransportError):
        asyncio.run(adapter.apply(effect))
    remote.lose_response = False
    receipt = asyncio.run(adapter.lookup(effect.idempotency_key))
    assert receipt is not None and receipt.applied
    stored = remote.delivered if kind == EffectKind.MESSAGE_SEND else remote.objects
    assert len(stored) == 1


def test_git_refuses_a_moved_head_and_empty_commits(tmp_path):
    adapter, make, _ = build(EffectKind.GIT_COMMIT, tmp_path)
    effect = make()
    moved = git_commit("p", "a", "main", "late", "0" * 40)
    assert asyncio.run(adapter.apply(moved)).reason == "head_moved"
    assert asyncio.run(adapter.apply(effect)).applied
    repo = tmp_path / "repo"
    empty = git_commit("p", "a", "main", "empty", git(repo, "rev-parse", "HEAD"))
    assert asyncio.run(adapter.apply(empty)).reason == "nothing_to_commit"
    assert asyncio.run(adapter.apply(git_commit("p", "a", "other", "m", "x"))).reason == "unknown_repository"
    assert "Praxis-Effect: " + effect.idempotency_key in git(repo, "log", "-1", "--format=%B")
    assert asyncio.run(adapter.lookup(moved.idempotency_key)).reason == "not_committed"


def test_git_never_runs_repository_hooks(tmp_path):
    adapter, make, _ = build(EffectKind.GIT_COMMIT, tmp_path)
    hook = tmp_path / "repo" / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ntouch " + str(tmp_path / "hooked") + "\nexit 1\n")
    hook.chmod(0o755)
    assert asyncio.run(adapter.apply(make())).applied
    assert not (tmp_path / "hooked").exists()


def test_refusals_are_failed_receipts_not_uncertainty(tmp_path):
    adapter, make, receiver = build(EffectKind.MESSAGE_SEND, tmp_path)
    receiver.refuse = 422
    assert asyncio.run(adapter.apply(make())).reason == "rejected_422"
    store_adapter = ArtifactPublishAdapter(Store(), Source(), lambda key: None)
    wrong = artifact_publish("p", "a", "bucket", "other:out.bin", "a/b")
    assert asyncio.run(store_adapter.apply(wrong)).reason == "artifact_not_verified"


def test_artifact_lookup_does_not_claim_foreign_bytes(tmp_path):
    store = Store()
    adapter = ArtifactPublishAdapter(store, Source(), lambda key: "0" * 64)
    effect = artifact_effect()
    store.objects[f"/artifacts/{effect.idempotency_key}"] = b"someone else"
    assert asyncio.run(adapter.lookup(effect.idempotency_key)) is None


def test_irreversible_kinds_require_lookup():
    class SendOnly:
        async def apply(self, effect):
            return EffectReceipt(True, "sent")
    assert IRREVERSIBLE == {EffectKind.MESSAGE_SEND, EffectKind.ARTIFACT_PUBLISH}
    with pytest.raises(ValueError, match="lookup"):
        require_reconcilable({EffectKind.MESSAGE_SEND: SendOnly()})
    require_reconcilable({EffectKind.GIT_COMMIT: SendOnly()})


def test_kernel_does_not_import_effect_adapters():
    source = Path(praxis.kernel.runtime.__file__).parent
    for module in source.glob("*.py"):
        assert "praxis.effects" not in module.read_text(), module.name
