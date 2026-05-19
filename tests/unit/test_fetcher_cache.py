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
    }

    first = build_processed_dataset(**kwargs)
    second = build_processed_dataset(**kwargs)

    assert first.prices_path.exists()
    assert first.benchmark_path.exists()
    assert second.fetch_log_path.exists()
    assert provider.etf_calls == 1
    assert provider.benchmark_calls == 1
