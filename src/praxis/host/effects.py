"""Host side of the effect pipeline (ADR 0001) and approval policy (ADR 0002).

``HostEffects`` receives the effects a verified process proposed. For each one it finds
the most specific ``[[effect_policy]]`` rule. No rule, or ``deny``, stages the effect
without authority, so the kernel records it as rejected. Otherwise the host issues the
process exactly the authority that effect needs, stages it, and applies the rule:
``auto`` approves it at once, ``human`` queues it for the rule's approvers. The same
object answers ``ApproverPolicy`` for people, from configuration only.
"""

import logging
import ssl
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from praxis.effects import require_reconcilable
from praxis.effects.artifact import ArtifactPublishAdapter, HTTPObjectStore, SnapshotArtifacts
from praxis.effects.git import GitCommitAdapter
from praxis.effects.webhook import WebhookMessageAdapter
from praxis.host.config import EffectAdapterConfig, EffectPolicyRule, HostConfig
from praxis.kernel.capabilities import Resource
from praxis.kernel.effect_service import EffectAdapter, EffectPolicy, EffectService
from praxis.kernel.effects import Effect, EffectKind, EffectStatus
from praxis.kernel.runtime import Kernel
from praxis.observability.redaction import RedactionPolicy
from praxis.transport.http import HTTPTransport

logger = logging.getLogger("praxis.host")


def _credential(entry: EffectAdapterConfig, environ: Mapping[str, str]) -> str | None:
    if entry.token_env is not None:
        token = environ.get(entry.token_env, "")
    elif entry.token_file is not None:
        token = Path(entry.token_file).read_text().strip()
    else:
        return None
    if not token:
        raise ValueError(f"credential for {entry.kind} effects is empty or missing")
    return token


def _ssl(entry: EffectAdapterConfig) -> ssl.SSLContext | None:
    if entry.base_url is None or not entry.base_url.startswith("https://"):
        return None
    context = ssl.create_default_context(cafile=entry.ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if entry.client_certfile is not None:
        context.load_cert_chain(entry.client_certfile, entry.client_keyfile)
    return context


def build_adapters(config: HostConfig, kernel: Kernel, effects: Callable[[], EffectService],
                   environ: Mapping[str, str], redaction: RedactionPolicy) -> dict[EffectKind, EffectAdapter]:
    """Adapters for every ``[[effects]]`` entry. Refuses irreversible kinds without lookup."""
    adapters: dict[EffectKind, EffectAdapter] = {}
    for entry in config.effects:
        token = _credential(entry, environ)
        headers = {} if token is None else {"Authorization": "Bearer " + token}
        if token is not None:
            redaction.register(token)
        if entry.kind == "git_commit":
            adapters[EffectKind.GIT_COMMIT] = GitCommitAdapter(
                dict(entry.repositories), author_name=entry.author_name, author_email=entry.author_email,
                timeout=entry.timeout_seconds)
        elif entry.kind == "message_send":
            assert entry.base_url is not None
            transport = HTTPTransport(entry.base_url, headers=headers, timeout=entry.timeout_seconds,
                                      ssl_context=_ssl(entry))
            adapters[EffectKind.MESSAGE_SEND] = WebhookMessageAdapter(
                transport, send_path=entry.send_path, status_path=entry.status_path)
        else:
            assert entry.base_url is not None
            store = HTTPObjectStore(entry.base_url, headers=headers, timeout=entry.timeout_seconds,
                                    ssl_context=_ssl(entry))
            source = SnapshotArtifacts(kernel.workspaces, kernel.verification.get)

            def expected(key: str) -> str | None:
                effect = effects().find(key)
                digest = None if effect is None else effect.payload.get("metadata", {}).get("sha256")
                return digest if isinstance(digest, str) else None
            adapters[EffectKind.ARTIFACT_PUBLISH] = ArtifactPublishAdapter(store, source, expected, prefix=entry.prefix)
    require_reconcilable(adapters)
    return adapters


class HostEffects:
    """ProposalSink and ApproverPolicy from the live host configuration."""

    def __init__(self, kernel: Kernel, config: HostConfig, owner: Callable[[str], str | None]):
        self.kernel = kernel
        self.config = config
        self.owner = owner
        self.service: EffectService | None = None

    def rule(self, effect: Effect) -> EffectPolicyRule | None:
        return self.config.effect_rule(effect.kind.value, effect.target)

    def expires_at(self, effect: Effect) -> str | None:
        rule = self.rule(effect)
        if rule is None or rule.expires_seconds is None:
            return None
        return (datetime.now(timezone.utc) + timedelta(seconds=rule.expires_seconds)).isoformat()

    def may_decide(self, actor: str, effect: Effect) -> bool:
        rule = self.rule(effect)
        if rule is None or rule.policy != "human" or not rule.approver(actor):
            return False
        if rule.separate_submitter and self.owner(effect.process_id) == actor:
            return False
        return True

    def propose(self, process_id: str, effects: tuple[Effect, ...]) -> None:
        service = self.service
        if service is None:
            raise RuntimeError("effect service not attached")
        for effect in effects:
            rule = self.rule(effect)
            if rule is not None and rule.policy != "deny":
                self._grant(effect)
            staged = service.stage(effect)
            if staged.status != EffectStatus.PROPOSED or staged.version != 0:
                continue  # rejected at staging, or already handled before a restart
            if rule is None or rule.policy == "deny":
                continue  # unreachable: without authority staging rejects it
            if rule.policy == "auto":
                service.resolve_approval(staged.effect_id, staged.version, True,
                                         actor=staged.process_id, reason=f"policy:{rule.kind}:{rule.target}",
                                         expires_at=self.expires_at(staged))
            else:
                service.apply_policy(staged.effect_id, staged.version, EffectPolicy.HUMAN,
                                     actor=staged.process_id, reason=f"policy:{rule.kind}:{rule.target}")

    def _grant(self, effect: Effect) -> None:
        """Exactly the authority this effect needs, and only if the process lacks it."""
        authority = self.kernel.authority
        needed = [(Resource.EFFECT, "stage", effect.target),
                  (effect.authority.resource, effect.authority.action, effect.authority.scope)]
        rule = self.rule(effect)
        if rule is not None and rule.policy == "auto":
            needed.append((Resource.EFFECT, "approve", effect.target))
        for resource, action, scope in needed:
            if not authority.authorize(effect.process_id, resource, action, scope).allowed:
                authority.issue(effect.process_id, resource, frozenset({action}), scope)
