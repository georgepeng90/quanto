import math

import numpy as np
import pytest


def test_dsr_uses_previous_moments_before_daily_update():
    from quanto.investing.rewards import DifferentialSharpeState
    state = DifferentialSharpeState()
    reward, floored = state.update(.01)
    assert reward == pytest.approx(1.)
    assert not floored
    assert state.mean == pytest.approx(.0001)
    assert state.second_moment == pytest.approx(.0001)
    reward, floored = state.update(-.02)
    assert reward == pytest.approx(-2.02530378797318)
    assert not floored
    assert state.mean == pytest.approx(-.000101)
    assert state.second_moment == pytest.approx(.000103)


def test_zero_returns_decay_prior_and_variance_floor_does_not_clip_shock():
    from quanto.investing.rewards import DifferentialSharpeState
    state = DifferentialSharpeState()
    floors = 0
    for _ in range(1000):
        reward, floored = state.update(0.)
        assert reward == 0.
        floors += floored
    assert floors == 83
    reward, floored = state.update(.1)
    assert floored
    assert reward == pytest.approx(431.71247410657867)
    assert state.mean == pytest.approx(.001)


@pytest.mark.parametrize("returns", [[0.] * 2000, [.01] * 2000, [-.01] * 2000,
                                    [.5, -.9, 2., -.99] * 500])
def test_long_and_extreme_return_sequences_keep_finite_float64_state(returns):
    from quanto.investing.rewards import DifferentialSharpeState
    state = DifferentialSharpeState()
    for value in returns:
        reward, _ = state.update(value)
        assert math.isfinite(reward)
        assert math.isfinite(state.mean) and math.isfinite(state.second_moment)
    assert isinstance(state.mean, np.float64)
    assert isinstance(state.second_moment, np.float64)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 1e200])
def test_dsr_rejects_nonfinite_input_or_overflow_without_updating_state(value):
    from quanto.investing.rewards import DifferentialSharpeState
    state = DifferentialSharpeState()
    with pytest.raises(ValueError, match="finite"):
        state.update(value)
    assert state.mean == 0.
    assert state.second_moment == .0001


def test_reward_parameters_are_fixed_and_return_independent_copies():
    from quanto.investing.rewards import reward_parameters
    expected = dict(eta=.01, initial_mean=0., initial_second_moment=.0001, variance_floor=1e-8)
    assert reward_parameters("net-log-return-v1") == {}
    control = reward_parameters("net-log-return-with-state-v1")
    assert control == reward_parameters("differential-sharpe-v1") == expected
    control["eta"] = .5
    assert reward_parameters("differential-sharpe-v1") == expected
