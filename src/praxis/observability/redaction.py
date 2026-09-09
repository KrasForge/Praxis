"""Shared export boundary for diagnostics; durable recovery records stay private."""

import json
import logging
import re
from collections.abc import Callable
from typing import Any

REDACTED = "[REDACTED]"
SENSITIVE = re.compile(r"secret|password|credential|authorization|api.?key|access.?token|private.?key|capabilit|^environment$", re.I)


class RedactionPolicy:
    def __init__(self, secrets: tuple[str, ...] = ()):
        self._secrets = tuple(sorted((s for s in secrets if s), key=len, reverse=True))

    def register(self, secret: str) -> None:
        if secret and secret not in self._secrets:
            self._secrets = tuple(sorted((*self._secrets, secret), key=len, reverse=True))

    def clean(self, value: Any, *, _depth: int = 0) -> Any:
        if _depth > 32:
            return REDACTED
        if isinstance(value, dict):
            return {self.clean(str(k), _depth=_depth + 1): REDACTED if SENSITIVE.search(str(k)) else
                    self.clean(v, _depth=_depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v, _depth=_depth + 1) for v in value]
        if isinstance(value, str):
            # Events carry some versioned documents as JSON strings.
            if value.lstrip().startswith(("{", "[")):
                try:
                    return json.dumps(self.clean(json.loads(value), _depth=_depth + 1), sort_keys=True)
                except (ValueError, RecursionError):
                    return REDACTED
            for secret in self._secrets:
                value = value.replace(secret, REDACTED)
            return value
        return value

    def emit(self, sink: Callable[[Any], None], value: Any) -> None:
        sink(self.clean(value))


class RedactingLogFilter(logging.Filter):
    """Install on every export handler, including handlers used by third parties."""

    def __init__(self, policy: RedactionPolicy):
        super().__init__()
        self.policy = policy

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.policy.clean(record.getMessage())
        record.args = ()
        # Exceptions can contain arbitrary credentials; export structured errors instead.
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        for key, value in tuple(record.__dict__.items()):
            if SENSITIVE.search(key):
                record.__dict__[key] = REDACTED
            elif isinstance(value, (str, dict, list, tuple)):
                record.__dict__[key] = self.policy.clean(value)
        return True
