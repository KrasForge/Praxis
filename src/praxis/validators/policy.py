"""Deterministic required/advisory/quorum verification decisions."""

from dataclasses import dataclass

from praxis.kernel.contracts import Contract
from praxis.validators.protocol import CheckResult, CheckStatus, ValidationInput


@dataclass(frozen=True)
class VerificationReport:
    snapshot_id: str
    approved: bool
    results: tuple[CheckResult, ...]
    required_failures: tuple[str, ...]
    advisory_failures: tuple[str, ...]
    missing_outputs: tuple[str, ...]
    quorum_passed: bool


def evaluate(
    contract: Contract, source: ValidationInput, results: tuple[CheckResult, ...],
) -> VerificationReport:
    checks = contract.invariants + contract.validators
    ids = {check.check_id for check in checks}
    mapped = {result.check_id: result for result in results}
    if len(mapped) != len(results) or not mapped.keys() <= ids:
        raise ValueError("duplicate or unknown validator result")
    for check_id in sorted(ids - mapped.keys()):
        mapped[check_id] = CheckResult(check_id, CheckStatus.UNAVAILABLE, "validator_unavailable")
    required = {check.check_id for check in checks if check.required} | set(contract.acceptance_checks)
    required.update(check.check_id for check in contract.invariants)
    required_failures = tuple(sorted(k for k in required if mapped[k].status != CheckStatus.PASS))
    advisory_failures = tuple(sorted(k for k in ids - required if mapped[k].status != CheckStatus.PASS))
    available = {path for path, _ in source.files}
    missing = tuple(sorted(set(contract.required_outputs) - available))
    quorum_passed = contract.quorum is None or sum(
        mapped[k].status == CheckStatus.PASS for k in contract.quorum_checks
    ) >= contract.quorum
    return VerificationReport(
        source.snapshot_id, not required_failures and not missing and quorum_passed,
        tuple(mapped[k] for k in sorted(mapped)), required_failures, advisory_failures,
        missing, quorum_passed,
    )
