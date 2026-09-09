import pytest

from praxis.observability.metrics import CATALOG, METRICS_VERSION, Metrics


def test_metric_contract_and_cardinality():
    metrics = Metrics()
    assert METRICS_VERSION == 1
    metrics.emit("praxis_processes_created_total", 1, executor="local")
    metrics.emit("praxis_execution_seconds", 0.5, executor="local")
    metrics.emit("praxis_execution_seconds", 1.5, executor="local")
    metrics.emit("praxis_usage_tokens_total", 12)
    assert metrics.value("praxis_execution_seconds", executor="local") == 2
    assert metrics.value("praxis_usage_tokens_total") == 12
    assert all(name.startswith("praxis_") for name in CATALOG)
    with pytest.raises(ValueError, match="labels"):
        metrics.emit("praxis_processes_created_total", 1, executor="local", process_id="secret")
    with pytest.raises(ValueError, match="labels"):
        metrics.emit("praxis_processes_created_total", 1, executor="arbitrary-process-id")
    with pytest.raises(ValueError, match="value"):
        metrics.emit("praxis_queue_depth", float("nan"))
