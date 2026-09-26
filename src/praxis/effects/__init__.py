"""Effect adapters: the code that performs an approved external write (ADR 0001).

Adapters sit at the same boundary as executor adapters. The kernel never imports this
package; a host loads adapters from its configuration and hands them to
``EffectService``. Every adapter serves one ``EffectKind`` and uses only the standard
library. An adapter for an irreversible kind must also implement ``lookup`` so an
uncertain application can be reconciled instead of repeated.
"""

from praxis.kernel.effect_service import EffectAdapter, ReconciliableEffectAdapter
from praxis.kernel.effects import EffectKind

IRREVERSIBLE = frozenset({EffectKind.MESSAGE_SEND, EffectKind.ARTIFACT_PUBLISH})


def reconcilable(adapter: EffectAdapter) -> bool:
    return isinstance(adapter, ReconciliableEffectAdapter)


def require_reconcilable(adapters: dict[EffectKind, EffectAdapter]) -> None:
    """Refuse a registry that could leave an irreversible effect without a lookup."""
    for kind, adapter in adapters.items():
        if kind in IRREVERSIBLE and not reconcilable(adapter):
            raise ValueError(f"effect adapter for {kind.value} must implement lookup")
