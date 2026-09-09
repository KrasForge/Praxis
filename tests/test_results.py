from dataclasses import replace

import pytest

from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.claims import Claim
from praxis.kernel.contracts import Contract
from praxis.kernel.lifecycle import State
from praxis.kernel.references import Reference, ReferenceKind
from praxis.kernel.results import ProcessResult
from praxis.validators.policy import evaluate
from praxis.validators.protocol import ValidationInput


def test_nested_process_result_roundtrip():
    evidence = Reference.from_content(b"test passed", ReferenceKind.EVIDENCE, "text/plain", "p", "a")
    artifact = Reference.from_content(b"output", ReferenceKind.ARTIFACT, "text/plain", "p", "a")
    verification = evaluate(Contract(), ValidationInput("p", "a", "snapshot", ()), ())
    result = ProcessResult("p", "a", State.COMPLETED, Outcome(OutcomeStatus.COMPLETED, "done"),
                           "verified work", verification, (artifact,), (evidence,),
                           (Claim("Tests pass", "p", "a", (evidence.reference_id,)),), ("Future work?",), usage={"tokens": 10})
    assert ProcessResult.from_json(result.to_json()) == result
    with pytest.raises(ValueError):
        replace(result, verification=None)
    with pytest.raises(ValueError):
        replace(result, outcome=Outcome(OutcomeStatus.PARTIAL, "partial"))
    partial = ProcessResult("p", "a", State.FAILED, Outcome(OutcomeStatus.PARTIAL, "partial"))
    assert ProcessResult.from_json(partial.to_json()).outcome.status == OutcomeStatus.PARTIAL
