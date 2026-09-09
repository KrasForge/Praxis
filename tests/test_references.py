from dataclasses import replace

from praxis.kernel.references import Availability, Reference, ReferenceKind


def test_stable_content_addresses_and_missing_targets():
    first = Reference.from_content(b"artifact", ReferenceKind.ARTIFACT, "application/octet-stream", "p", "a")
    second = Reference.from_content(b"artifact", ReferenceKind.EVIDENCE, "application/octet-stream", "q", "b")
    assert first.reference_id == second.reference_id
    assert first.process_id != second.process_id
    missing = replace(first, availability=Availability.MISSING)
    assert Reference.from_json(missing.to_json()) == missing
    assert missing.reference_id == first.reference_id
