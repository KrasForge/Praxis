import pytest

from praxis.kernel.usage import UsageLedger


def test_attempt_process_tree_totals_and_deduplication():
    ledger = UsageLedger([])
    ledger.register("p")
    ledger.register("c", "p")
    ledger.record("p", "a1", "u1", {"tokens": 10})
    ledger.record("p", "a2", "u2", {"tokens": 5})
    ledger.record("c", "a3", "u3", {"tokens": 7, "cost_microusd": 100})
    ledger.record("c", "a3", "u3", {"tokens": 7, "cost_microusd": 100})
    assert ledger.total("p", attempt_id="a1") == {"tokens": 10}
    assert ledger.total("p") == {"tokens": 15}
    assert ledger.total("p", tree=True) == {"tokens": 22, "cost_microusd": 100}
    assert "cpu_milliseconds" not in ledger.total("p", tree=True)
    assert len(ledger.events) == 3
    with pytest.raises(ValueError):
        ledger.record("c", "a3", "u3", {"tokens": 8})
    with pytest.raises(ValueError):
        ledger.record("c", "a1", "u4", {"tokens": 1})
