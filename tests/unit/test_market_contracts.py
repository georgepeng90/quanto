from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


def request_values():
    from quanto.data.contracts import InstrumentId, PriceBasis

    return dict(kind="bars", instruments=(InstrumentId(exchange="SZSE", instrument_type="etf", code="000001"),),
                start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2024, 1, 2, tzinfo=timezone.utc),
                columns=("close",), frequency="1d", price_basis=PriceBasis(mode="raw"))


def test_identity_keeps_leading_zero_and_instrument_type():
    from quanto.data.contracts import InstrumentId

    etf = InstrumentId(exchange="SZSE", instrument_type="etf", code="000001")
    index = InstrumentId(exchange="SZSE", instrument_type="index", code="000001")
    assert etf.code == "000001"
    assert etf != index
    assert etf.key == "SZSE:etf:000001"


@pytest.mark.parametrize("change", [{"start": datetime(2024, 1, 1)}, {"end": datetime(2024, 1, 1, tzinfo=timezone.utc)},
                                    {"frequency": "tick"}, {"price_basis": None}])
def test_requirement_rejects_naive_or_empty_interval(change):
    from quanto.data.contracts import DataRequest

    with pytest.raises(ValidationError):
        DataRequest(**(request_values() | change))


@pytest.mark.parametrize("field", ["raw_dir", "cache_version", "output_path"])
def test_refs_and_requests_reject_paths(field):
    from quanto.data.contracts import DataRequest, SnapshotRef

    with pytest.raises(ValidationError):
        DataRequest(**(request_values() | {field: "arbitrary"}))
    with pytest.raises(ValidationError):
        SnapshotRef(id="a" * 64, manifest_digest="a" * 64, **{field: "arbitrary"})


def test_requirement_identity_is_independent_of_request_order():
    from quanto.data.contracts import DataRequest, DataRequirement

    a = DataRequest(**request_values())
    b = a.model_copy(update={"columns": ("open", "close")})
    assert DataRequirement(requests=(a, b)).key == DataRequirement(requests=(b, a)).key


def test_relocated_backtest_loader_keeps_current_dataset_contract(tmp_path):
    from quanto.backtest.dataset_loaders import load_dataset
    from tests.fixtures.backtest_cases import dataset_case

    bundle = load_dataset(dataset_case(tmp_path))
    assert bundle.prices.symbol.iloc[0] == "000001"
    assert not bundle.prices.empty
