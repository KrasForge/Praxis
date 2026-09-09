import asyncio

import pytest

from praxis.compatibility import CompatibilityError, VERSIONS, negotiate
from praxis.remote.heartbeat import Heartbeats, RegistryRPC
from praxis.remote.workers import WorkerRegistry
from praxis.storage.sqlite import SQLiteStore


@pytest.mark.parametrize("component", list(VERSIONS))
def test_matrix_negotiation(component):
    assert negotiate(component, [3, 1, 2]) == 1
    for offer in ([2], [], [True], ["1"]):
        with pytest.raises(CompatibilityError):
            negotiate(component, offer)


def test_worker_negotiates_and_persists_version(tmp_path):
    async def exercise():
        store = SQLiteStore(tmp_path / "db")
        registry = WorkerRegistry(store, lambda *_: "owner")
        rpc = RegistryRPC(Heartbeats(registry))
        response = await rpc.rpc({"operation": "register", "worker_id": "w", "incarnation": "boot", "protocol_versions": [2, 1]}, "valid")
        assert response["worker"]["protocol_version"] == registry.load("w").protocol_version == 1
        with pytest.raises(CompatibilityError):
            await rpc.rpc({"operation": "register", "worker_id": "x", "incarnation": "boot", "protocol_versions": [2]}, "valid")
        store.close()
    asyncio.run(exercise())


def test_deprecation_is_visible_filterable_and_contains_only_identifiers():
    from praxis.compatibility import PraxisDeprecationWarning, warn_deprecated
    with pytest.warns(PraxisDeprecationWarning, match="replacement:new_api"):
        warn_deprecated("old_api", replacement="new_api", removal="2.0")
    with pytest.raises(ValueError):
        warn_deprecated("raw secret value", replacement="new_api", removal="2.0")
