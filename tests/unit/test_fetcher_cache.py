import pandas as pd

from quanto.data.fetcher import build_processed_dataset, fetch_cached_etf


class FakeProvider:
    source = "fake"

    def __init__(self):
        self.etf_calls = 0
        self.benchmark_calls = 0

    def fetch_etf_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        self.etf_calls += 1
        dates = pd.bdate_range(start_date, end_date)
        return pd.DataFrame(
            {
                "date": dates,
                "symbol": symbol,
                "open": [1.0] * len(dates),
                "high": [1.1] * len(dates),
                "low": [0.9] * len(dates),
                "close": [1.0] * len(dates),
                "volume": [1000] * len(dates),
                "amount": [10000] * len(dates),
            }
        )

    def fetch_benchmark_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        self.benchmark_calls += 1
        dates = pd.bdate_range(start_date, end_date)
        return pd.DataFrame(
            {
                "date": dates,
                "close": [4000.0] * len(dates),
                "turnover": [900_000_000_000.0] * len(dates),
            }
        )


def test_build_processed_dataset_reuses_raw_cache(tmp_path):
    provider = FakeProvider()
    kwargs = {
        "symbols": ["512480"],
        "start_date": "2024-01-01",
        "end_date": "2024-01-05",
        "processed_dir": tmp_path / "processed",
        "raw_dir": tmp_path / "raw",
        "provider": provider,
        "request_delay_seconds": 0,
    }

    first = build_processed_dataset(**kwargs)
    second = build_processed_dataset(**kwargs)

    assert first.prices_path.exists()
    assert first.benchmark_path.exists()
    assert second.fetch_log_path.exists()
    assert provider.etf_calls == 1
    assert provider.benchmark_calls == 1


def test_build_processed_dataset_falls_back_to_existing_cache_when_network_fails(tmp_path):
    provider = FakeProvider()
    kwargs = {
        "symbols": ["512480"],
        "start_date": "2024-01-01",
        "end_date": "2024-01-05",
        "processed_dir": tmp_path / "processed",
        "raw_dir": tmp_path / "raw",
        "provider": provider,
        "request_delay_seconds": 0,
    }
    build_processed_dataset(**kwargs)

    class BrokenProvider(FakeProvider):
        source = "broken"

        def fetch_etf_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
            raise RuntimeError("network down")

        def fetch_benchmark_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
            raise RuntimeError("network down")

    kwargs["provider"] = BrokenProvider()
    kwargs["end_date"] = "2024-01-08"

    result = build_processed_dataset(**kwargs)

    assert result.price_rows > 0
    assert result.benchmark_rows > 0
    assert not result.complete


def test_build_processed_dataset_skips_uncached_failed_symbol_when_allowed(tmp_path):
    class PartiallyBrokenProvider(FakeProvider):
        def fetch_etf_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
            if symbol == "BROKEN":
                raise RuntimeError("rate limited")
            return super().fetch_etf_daily(symbol, start_date, end_date)

    result = build_processed_dataset(
        symbols=["512480", "BROKEN"],
        start_date="2024-01-01",
        end_date="2024-01-05",
        processed_dir=tmp_path / "processed",
        raw_dir=tmp_path / "raw",
        provider=PartiallyBrokenProvider(),
        request_delay_seconds=0,
        max_retries=0,
        continue_on_error=True,
    )

    assert result.price_rows > 0
    assert result.failed_symbols == ("BROKEN",)
    assert not result.complete


def test_fetch_cache_preserves_leading_zero_symbol(tmp_path):
    provider = FakeProvider()
    first = fetch_cached_etf(provider, "000001", "2024-01-01", "2024-01-05", tmp_path, max_retries=0)
    second = fetch_cached_etf(provider, "000001", "2024-01-01", "2024-01-05", tmp_path, max_retries=0)
    assert first.iloc[0]["symbol"] == "000001"
    assert second.iloc[0]["symbol"] == "000001"
    assert provider.etf_calls == 1


def test_interior_cache_gap_triggers_refetch(tmp_path):
    provider = FakeProvider()
    sessions = [day.date().isoformat() for day in pd.bdate_range("2024-01-01", "2024-01-05")]
    kwargs = {"symbols": ["512480"], "start_date": sessions[0], "end_date": sessions[-1],
              "processed_dir": tmp_path / "processed", "raw_dir": tmp_path / "raw",
              "provider": provider, "request_delay_seconds": 0, "expected_sessions": sessions}
    assert build_processed_dataset(**kwargs).complete
    for path in (tmp_path / "raw/etf/512480.csv", tmp_path / "raw/benchmark/sh000300.csv"):
        frame = pd.read_csv(path, dtype={"symbol": str})
        frame.loc[frame.date != sessions[2]].to_csv(path, index=False)
    assert build_processed_dataset(**kwargs).complete
    assert provider.etf_calls == 2
    assert provider.benchmark_calls == 2


def test_failed_refresh_keeps_partial_provenance(tmp_path):
    provider = FakeProvider()
    sessions = [day.date().isoformat() for day in pd.bdate_range("2024-01-01", "2024-01-05")]
    kwargs = {"symbols": ["512480"], "start_date": sessions[0], "end_date": sessions[-1],
              "processed_dir": tmp_path / "processed", "raw_dir": tmp_path / "raw",
              "provider": provider, "request_delay_seconds": 0, "expected_sessions": sessions, "max_retries": 0}
    build_processed_dataset(**kwargs)
    for path in (tmp_path / "raw/etf/512480.csv", tmp_path / "raw/benchmark/sh000300.csv"):
        frame = pd.read_csv(path, dtype={"symbol": str})
        frame.loc[frame.date != sessions[2]].to_csv(path, index=False)
    class Broken(FakeProvider):
        def fetch_etf_daily(self, *args):
            raise RuntimeError("offline")
        def fetch_benchmark_daily(self, *args):
            raise RuntimeError("offline")
    result = build_processed_dataset(**{**kwargs, "provider": Broken()})
    assert not result.complete
    assert result.coverage["512480"]["missing"] == [sessions[2]]
    assert result.coverage["benchmark"]["missing"] == [sessions[2]]
