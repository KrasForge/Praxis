import pytest

from praxis.kernel.claims import Claim
from praxis.kernel.references import Reference, ReferenceKind


def test_claim_support_contradiction_and_integrity():
    support = Reference.from_content(b"support", ReferenceKind.EVIDENCE, "text/plain", "p", "a")
    contradiction = Reference.from_content(b"counterexample", ReferenceKind.EVIDENCE, "text/plain", "q", "b")
    claim = Claim("The invariant holds", "p", "a", (support.reference_id,), (contradiction.reference_id,), 0.6)
    claim.validate_evidence((support, contradiction))
    assert Claim.from_json(claim.to_json()) == claim
    assert not claim.unsupported
    with pytest.raises(ValueError, match="missing"):
        claim.validate_evidence((support,))
    unsupported = Claim("Unverified hypothesis", "p", "a")
    unsupported.validate_evidence(())
    assert unsupported.unsupported
