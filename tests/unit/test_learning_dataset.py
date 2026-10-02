from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from tests.fixtures.learning_cases import learning_case


def test_complete_session_anchored_aggregation_and_missing_minutes(tmp_path):
    from quanto.learning.dataset import prepare_dataset, iter_dataset_bars
    spec, start = learning_case(tmp_path / "market", minutes=12)
    spec["frequency_minutes"] = 5
    manifest = prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    rows = list(iter_dataset_bars(tmp_path / "dataset"))
    assert manifest["row_count"] == 4
    assert manifest["trading_days"] == ["2024-01-02"]
    assert rows[0]["bar_start"] == start.isoformat()
    assert rows[0]["bar_end"] == (start + timedelta(minutes=5)).isoformat()
    assert rows[0]["volume"] == 510
    assert rows[2]["bar_start"] == (start + timedelta(hours=3)).isoformat()
    gap_spec, _ = learning_case(tmp_path / "gapmarket", minutes=12, missing=(start + timedelta(minutes=2),))
    gap_spec["frequency_minutes"] = 5
    prepare_dataset(gap_spec, tmp_path / "gapmarket", tmp_path / "gapdata", lambda event: None, lambda: False)
    gaps = list(iter_dataset_bars(tmp_path / "gapdata"))
    assert gaps[0]["bar_start"] == (start + timedelta(minutes=5)).isoformat()


def test_features_are_causal_and_artifact_tampering_rejected(tmp_path):
    from quanto.learning.dataset import prepare_dataset, iter_dataset_bars, read_dataset_manifest
    from quanto.learning.features import FEATURE_NAMES
    spec, start = learning_case(tmp_path / "market")
    other, _ = learning_case(tmp_path / "othermarket", spike_after=start + timedelta(minutes=50))
    first = prepare_dataset(spec, tmp_path / "market", tmp_path / "first", lambda event: None, lambda: False)
    prepare_dataset(other, tmp_path / "othermarket", tmp_path / "other", lambda event: None, lambda: False)
    rows = list(iter_dataset_bars(tmp_path / "first"))
    changed = list(iter_dataset_bars(tmp_path / "other"))
    np.testing.assert_array_equal([[row[name] for name in FEATURE_NAMES] for row in rows[:50]], [[row[name] for name in FEATURE_NAMES] for row in changed[:50]])
    target = tmp_path / "first" / next(iter(first["files"]))
    target.write_bytes(target.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="hash|integrity"):
        read_dataset_manifest(tmp_path / "first")


def test_log_features_do_not_underflow_positive_price_ratios():
    from quanto.learning.features import causal_features
    row = dict(open=1e-200, high=1e-200, low=1e-200, close=1e-200, volume=0., amount=0.)
    previous = {**row, "close": 1e200}
    features = causal_features(row, previous)
    assert features["log_return"] == pytest.approx(-400 * np.log(10))
    assert all(np.isfinite(value) for value in features.values())


@pytest.mark.parametrize("frequency, count", [(1, 100), (5, 20), (15, 6), (30, 3), (60, 1)])
def test_supported_aggregation_sizes_never_combine_lunch_sessions(tmp_path, frequency, count):
    from quanto.learning.dataset import prepare_dataset, iter_dataset_bars
    spec, _ = learning_case(tmp_path / "market")
    spec["frequency_minutes"] = frequency
    manifest = prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    assert manifest["row_count"] == count
    rows = list(iter_dataset_bars(tmp_path / "dataset"))
    assert all((datetime.fromisoformat(row["bar_end"]) - datetime.fromisoformat(row["bar_start"])).total_seconds() == frequency * 60 for row in rows)


def test_sparse_instruments_keep_features_aligned_and_bars_globally_sorted(tmp_path):
    from quanto.learning.dataset import prepare_dataset, iter_dataset_bars
    missing = datetime(2024, 1, 2, 1, 32, tzinfo=timezone.utc)
    spec, _ = learning_case(tmp_path / "market", minutes=12, instrument_count=2, missing_by_instrument={"SZSE:etf:000001": (missing,)})
    spec["frequency_minutes"] = 5
    manifest = prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    rows = list(iter_dataset_bars(tmp_path / "dataset"))
    assert manifest["row_count"] == 7
    keys = [(row["bar_end"], row["instrument_id"]) for row in rows]
    assert keys == sorted(keys)
    previous = {}
    for row in rows:
        expected = np.log(row["close"]) - np.log(previous[row["instrument_id"]]) if row["instrument_id"] in previous else 0.
        assert row["log_return"] == pytest.approx(expected)
        previous[row["instrument_id"]] = row["close"]


def test_intraday_query_uses_original_session_anchor(tmp_path):
    from quanto.learning.dataset import prepare_dataset, iter_dataset_bars
    spec, start = learning_case(tmp_path / "market")
    spec["query"]["start"] = (start + timedelta(minutes=6)).isoformat()
    spec["query"]["end"] = (start + timedelta(minutes=21)).isoformat()
    spec["frequency_minutes"] = 5
    manifest = prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    rows = list(iter_dataset_bars(tmp_path / "dataset"))
    assert manifest["row_count"] == 2
    assert [row["bar_start"] for row in rows] == [(start + timedelta(minutes=minute)).isoformat() for minute in (10, 15)]


def test_missing_minute_statistics_exclude_lunch_and_record_step_policy(tmp_path):
    from quanto.learning.dataset import prepare_dataset
    missing = datetime(2024, 1, 2, 1, 32, tzinfo=timezone.utc)
    spec, _ = learning_case(tmp_path / "market", minutes=12, instrument_count=2,
        missing_by_instrument={"SZSE:etf:000001": (missing, missing + timedelta(minutes=1))})
    manifest = prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    assert manifest["step_policy"] == "observed_bar_steps"
    assert manifest["statistics"]["expected_minutes"] == 44
    assert manifest["statistics"]["missing_minutes"] == 2
    assert manifest["statistics"]["gap_events"] == 1
    assert manifest["statistics"]["missing_minutes_by_instrument"] == {"SZSE:etf:000001": 2, "SZSE:etf:000002": 0}
    assert any("observed_bar_steps" in item and "overnight" in item for item in manifest["limitations"])


def test_lunch_and_overnight_do_not_count_as_missing_minutes(tmp_path):
    from quanto.learning.dataset import prepare_dataset
    spec, _ = learning_case(tmp_path / "market", minutes=12, days=2)
    manifest = prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    assert manifest["trading_days"] == ["2024-01-02", "2024-01-03"]
    assert manifest["statistics"]["expected_minutes"] == 44
    assert manifest["statistics"]["missing_minutes"] == 0
    assert manifest["statistics"]["gap_events"] == 0
