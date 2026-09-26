"""Hashed bearer tokens, role-scoped actions and per-client process ownership.

A client authenticates with ``Authorization: Bearer <token>``; the host stores only the
token's SHA-256 digest. A client configured with ``delegate = true`` (Modulo) may name the
end user it acts for in ``X-Praxis-On-Behalf-Of``; that user's identity becomes
``<client>/<user>`` so submissions, approvals and interventions are audited per person.
"""

import hashlib
import hmac
import re
import secrets

from praxis.api.auth import Actor, SecurityHooks
from praxis.host.config import ClientConfig, HostConfig
from praxis.kernel.runtime import Kernel
from praxis.storage.protocol import ProcessStore

ON_BEHALF_OF = "x-praxis-on-behalf-of"
PRINCIPAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}")
ACTION_ROLES = {
    "health": "health", "submit": "submit", "inspect": "read", "tree": "read", "events": "read",
    "control": "control", "interventions": "control", "approvals": "approve", "publication": "publish",
    "effects": "publish",
}


def new_token() -> tuple[str, str]:
    """Return a fresh client token and the digest to place in the host config."""
    token = "praxis_" + secrets.token_urlsafe(32)
    return token, token_digest(token)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class HostSecurity:
    def __init__(self, config: HostConfig, kernel: Kernel):
        self.config = config
        self.kernel = kernel
        self.owners: dict[str, str] = {}

    def hooks(self) -> SecurityHooks:
        return SecurityHooks(self.authenticate, self.authorize)

    def authenticate(self, headers: dict[str, str]) -> Actor | None:
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        presented = token_digest(token)
        client = None
        for candidate in self.config.clients:
            # Compare against every digest so timing does not reveal which one matched.
            for digest in candidate.token_sha256:
                if hmac.compare_digest(presented, digest):
                    client = candidate
        if client is None:
            return None
        principal = headers.get(ON_BEHALF_OF)
        if principal is None:
            return Actor(client.id, (("client", client.id),))
        if not client.delegate or not PRINCIPAL.fullmatch(principal):
            return None
        return Actor(f"{client.id}/{principal}", (("client", client.id), ("principal", principal)))

    def client_of(self, actor: Actor) -> ClientConfig | None:
        return self.config.client(dict(actor.attributes).get("client", ""))

    def authorize(self, actor: Actor, action: str, process_id: str | None) -> bool:
        client = self.client_of(actor)
        role = ACTION_ROLES.get(action)
        if client is None or role is None or not (role in client.roles or "admin" in client.roles):
            return False
        if process_id is None or "admin" in client.roles:
            return True
        owner = self.owner(process_id)
        if action == "approvals" and owner is not None and self.config.approver(actor.identity):
            # A configured approver decides on work they do not own (ADR 0002); which
            # effects they may decide is checked per effect by the approver policy.
            return True
        if owner is None:
            # Unknown process: let the control plane answer 404 without revealing anything else.
            return process_id not in self.kernel.processes
        if dict(actor.attributes).get("principal") is not None:
            return owner == actor.identity
        return owner == client.id or owner.startswith(client.id + "/")

    def owner(self, process_id: str) -> str | None:
        """Submitting actor of the process tree root; children inherit their root's owner."""
        seen = set()
        identity = process_id
        while identity in self.kernel.processes and identity not in seen:
            seen.add(identity)
            parent = self.kernel.processes[identity].parent_id
            if parent is None:
                break
            identity = parent
        if identity not in self.kernel.processes:
            return None
        if identity not in self.owners:
            records = self.kernel.records
            history = (tuple(entry.event for entry in records.read_events(identity))
                       if isinstance(records, ProcessStore) else
                       tuple(e for e in self.kernel.events if e.process_id == identity))
            created = next((e for e in history if e.type == "process.created"), None)
            actor = None if created is None else created.payload.get("actor")
            # Processes created outside the API (no submitting actor) belong to admins only.
            self.owners[identity] = actor if isinstance(actor, str) and actor else ""
        return self.owners[identity]
