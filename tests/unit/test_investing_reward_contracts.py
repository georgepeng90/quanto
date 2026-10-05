import pytest

from quanto.investing.contracts import PolicyTrainingSpec
from tests.fixtures.investing_training_cases import investing_case


@pytest.mark.parametrize("reward", ["net-log-return-with-state-v1", "differential-sharpe-v1"])
def test_state_reward_requires_undiscounted_zero_risk_free_training(tmp_path, reward):
    spec, _ = investing_case(tmp_path / "data")
    spec["reward_version"] = reward
    assert PolicyTrainingSpec.model_validate(spec).reward_version == reward
    with pytest.raises(ValueError, match="gamma"):
        PolicyTrainingSpec.model_validate({**spec, "gamma": .99})
    spec["backtest"]["metrics"]["risk_free_rate"] = .03
    with pytest.raises(ValueError, match="risk_free_rate"):
        PolicyTrainingSpec.model_validate(spec)


def test_legacy_reward_is_default_and_retains_existing_parameter_range(tmp_path):
    spec, _ = investing_case(tmp_path / "data")
    spec["gamma"] = .99
    spec["backtest"]["metrics"]["risk_free_rate"] = .03
    config = PolicyTrainingSpec.model_validate(spec)
    assert config.reward_version == "net-log-return-v1"
    with pytest.raises(ValueError):
        PolicyTrainingSpec.model_validate({**spec, "reward_version": "unknown"})


@pytest.mark.parametrize("explicit_null", [False, True])
def test_training_can_end_with_validation_without_test_dates(tmp_path, explicit_null):
    spec, _ = investing_case(tmp_path / "data")
    spec.pop("test_start")
    spec.pop("test_end")
    spec["backtest"]["end"] = spec["validation_end"]
    if explicit_null:
        spec.update(test_start=None, test_end=None)
    config = PolicyTrainingSpec.model_validate(spec)
    assert config.test_start is None and config.test_end is None
    assert config.backtest.end == config.validation_end
    with pytest.raises(ValueError, match="coverage"):
        PolicyTrainingSpec.model_validate({**spec, "validation_end": "2025-01-01"})


@pytest.mark.parametrize("missing", ["test_start", "test_end"])
def test_test_dates_must_be_present_as_a_pair(tmp_path, missing):
    spec, _ = investing_case(tmp_path / "data")
    spec[missing] = None
    with pytest.raises(ValueError, match="together|pair"):
        PolicyTrainingSpec.model_validate(spec)
