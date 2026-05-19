import pandas as pd

from quanto.data.fetcher import build_processed_dataset


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
