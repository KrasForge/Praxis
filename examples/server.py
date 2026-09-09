"""Single-operator, fake-executor API factory; see docs/operations.md."""
import hmac
import os
from pathlib import Path

from praxis.api.asgi import Application
from praxis.api.auth import Actor, SecurityHooks
from praxis.api.service import ControlPlane
from praxis.executors.fake import FakeExecutor
from praxis.kernel.authority import Authority
from praxis.kernel.runtime import Kernel
from praxis.observability.redaction import RedactionPolicy
from praxis.storage.sqlite import SQLiteStore
from praxis.workspaces.local import LocalWorkspaces


def create_app():
    token = os.environ["PRAXIS_API_TOKEN"]
    if len(token) < 32:
        raise ValueError("use a strong host-provided API token")
    root = Path(os.environ.get("PRAXIS_DATA", "./praxis-data")).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    authority = Authority(execution_defaults=frozenset({"fake"}))
    kernel = Kernel(SQLiteStore(root / "runtime.db"), LocalWorkspaces(root / "workspaces"),
                    {"fake": FakeExecutor()}, authority=authority)
    kernel.recover_records()
    def authenticate(headers):
        presented = headers.get("authorization", "")
        return Actor("operator") if hmac.compare_digest(presented, "Bearer " + token) else None
    security = SecurityHooks(authenticate, lambda actor, action, pid: actor.identity == "operator")
    return Application(ControlPlane(kernel), security, RedactionPolicy((token,)))
