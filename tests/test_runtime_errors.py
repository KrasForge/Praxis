import json

import pytest

from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.kernel.lifecycle import State
from praxis.kernel.results import ProcessResult
from praxis.observability.errors import ErrorCode, RuntimeErrorRecord


def test_nested_causes_roundtrip():
    record = RuntimeErrorRecord(ErrorCode.EXECUTOR, (RuntimeErrorRecord(ErrorCode.TRANSPORT),))
    assert RuntimeErrorRecord.from_json(record.to_json()) == record
    result = ProcessResult("p", "a", State.FAILED, Outcome(OutcomeStatus.FAILED, "failed"), error=record)
    assert ProcessResult.from_json(result.to_json()).error == record


@pytest.mark.parametrize("status,reason,code", [
    (OutcomeStatus.COMPLETED, "completed", ErrorCode.CONTRACT),
    (OutcomeStatus.FAILED, "capability_denied", ErrorCode.POLICY),
    (OutcomeStatus.PARTIAL, "remote_transport_unavailable", ErrorCode.TRANSPORT),
    (OutcomeStatus.FAILED, "executor_error", ErrorCode.EXECUTOR),
])
def test_result_failure_categories(status, reason, code):
    result = ProcessResult("p", "a", State.FAILED, Outcome(status, reason))
    assert result.error.code == code
    assert ProcessResult.from_json(result.to_json()) == result


def test_invalid_and_excessive_chains():
    for data in ({"code": "unknown"}, {"code": "executor_failed", "schema_version": 2},
                 {"code": "executor_failed", "message": "secret"}):
        with pytest.raises(ValueError):
            RuntimeErrorRecord.from_json(json.dumps(data))
    record = {"code": "executor_failed"}
    for _ in range(18):
        record = {"code": "executor_failed", "causes": [record]}
    with pytest.raises(ValueError):
        RuntimeErrorRecord.from_json(json.dumps(record))
