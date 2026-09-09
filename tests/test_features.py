import pytest

from praxis.executors.features import ExecutorFeatures


def test_matching_and_roundtrip():
    features = ExecutorFeatures("local", frozenset({"signal", "cancel"}))
    assert features.matches(frozenset())
    assert features.matches(frozenset({"signal"}))
    assert not features.matches(frozenset({"checkpoint"}))
    assert not features.matches(frozenset(), 2)
    assert not features.matches(frozenset(), True)
    assert ExecutorFeatures.from_json(features.to_json()) == features


@pytest.mark.parametrize("raw", ['{}', 'null', '{"name":"x","features":["magic"]}',
                                  '{"name":"x","features":"signal"}'])
def test_invalid_descriptor(raw):
    with pytest.raises(ValueError):
        ExecutorFeatures.from_json(raw)
