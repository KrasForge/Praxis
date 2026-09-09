"""TM-7: host-owned provider and scoped, ephemeral secret delivery."""

import re
from collections.abc import Mapping
from typing import Protocol

from praxis.kernel.authority import Authority, AuthorizationError
from praxis.kernel.capabilities import Resource
from praxis.observability.redaction import RedactionPolicy


class SecretProvider(Protocol):
    def resolve(self, name: str) -> str: ...


class SecretAccess:
    def __init__(self, provider: SecretProvider, authority: Authority, redaction: RedactionPolicy):
        self.provider, self.authority, self.redaction = provider, authority, redaction

    def environment(self, process_id: str, bindings: Mapping[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(name, str) or not name
               for key, name in bindings.items()):
            raise ValueError("invalid_secret_binding")
        # Authorize the entire batch before contacting a provider.
        for name in bindings.values():
            if not self.authority.authorize(process_id, Resource.SECRET, "read", name).allowed:
                raise AuthorizationError("secret_capability_denied")
        values = {}
        try:
            for key, name in bindings.items():
                value = self.provider.resolve(name)
                if not isinstance(value, str) or not value or "\x00" in value or len(value) > 65536:
                    raise ValueError("invalid_secret_value")
                self.redaction.register(value)
                values[key] = value
        except Exception:
            raise ValueError("secret_provider_unavailable") from None
        return values
