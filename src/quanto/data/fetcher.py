from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from quanto.data.cache import (
    append_fetch_log,
    normalize_benchmark,
    normalize_prices,
    write_processed_benchmark,
    write_processed_prices,
)


PRICE_COLUMNS = ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]
BENCHMARK_COLUMNS = ["date", "close", "turnover"]


@dataclass(frozen=True)
class FetchResult:
    prices_path: Path
    benchmark_path: Path
    fetch_log_path: Path
    price_rows: int
    benchmark_rows: int


def _ak_date(value: str) -> str:
    return value.replace("-", "")


class AKShareDataProvider:
    """Small AKShare adapter; tests use fake providers instead of network."""

    source = "akshare"

    def __init__(self) -> None:
        try:
            import akshare as ak  # type: ignore
        except ImportError as exc:
            raise RuntimeError('Install AKShare first: pip install -e ".[data]"') from exc
        self.ak = ak

    def fetch_etf_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        df = self.ak.fund_etf_hist_em(
            symbol=symbol,
            period="daily",
            start_date=_ak_date(start_date),
            end_date=_ak_date(end_date),
            adjust="qfq",
        )
        out = df.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
            }
        ).copy()
        out["symbol"] = symbol
        return normalize_prices(out[PRICE_COLUMNS])

    def fetch_benchmark_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        df = self.ak.stock_zh_index_daily_em(symbol=symbol)
        out = df.rename(
            columns={
                "日期": "date",
                "date": "date",
                "收盘": "close",
                "close": "close",
                "成交额": "turnover",
                "amount": "turnover",
            }
        ).copy()
        out["date"] = pd.to_datetime(out["date"]).dt.normalize()
        start = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)
        out = out[(out["date"] >= start) & (out["date"] <= end)].copy()
        return normalize_benchmark(out[BENCHMARK_COLUMNS])


def _csv_cache_path(raw_dir: str | Path, category: str, name: str) -> Path:
    return Path(raw_dir) / category / f"{name}.csv"


def _cache_covers(df: pd.DataFrame, start_date: str, end_date: str) -> bool:
    if df.empty or "date" not in df.columns:
        return False
    dates = pd.to_datetime(df["date"])
    return dates.min() <= pd.to_datetime(start_date) and dates.max() >= pd.to_datetime(end_date)


def _read_cached_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    return pd.read_csv(path)


def _write_raw_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def fetch_cached_etf(
    provider: AKShareDataProvider,
    symbol: str,
    start_date: str,
    end_date: str,
    raw_dir: str | Path,
    force_refresh: bool = False,
) -> pd.DataFrame:
    cache_path = _csv_cache_path(raw_dir, "etf", symbol)
    cached = _read_cached_csv(cache_path)
    if cached is not None and not force_refresh and _cache_covers(cached, start_date, end_date):
        return normalize_prices(cached)
    df = provider.fetch_etf_daily(symbol, start_date, end_date)
    _write_raw_csv(cache_path, df)
    return df


def fetch_cached_benchmark(
    provider: AKShareDataProvider,
    symbol: str,
    start_date: str,
    end_date: str,
    raw_dir: str | Path,
    force_refresh: bool = False,
) -> pd.DataFrame:
    cache_path = _csv_cache_path(raw_dir, "benchmark", symbol)
    cached = _read_cached_csv(cache_path)
    if cached is not None and not force_refresh and _cache_covers(cached, start_date, end_date):
        return normalize_benchmark(cached)
    df = provider.fetch_benchmark_daily(symbol, start_date, end_date)
    _write_raw_csv(cache_path, df)
    return df


def build_processed_dataset(
    symbols: list[str],
    start_date: str,
    end_date: str,
    processed_dir: str | Path,
    raw_dir: str | Path,
    benchmark_symbol: str = "sh000300",
    force_refresh: bool = False,
    provider: AKShareDataProvider | None = None,
) -> FetchResult:
    provider = provider or AKShareDataProvider()
    price_frames = [
        fetch_cached_etf(provider, symbol, start_date, end_date, raw_dir, force_refresh)
        for symbol in symbols
    ]
    prices = normalize_prices(pd.concat(price_frames, ignore_index=True))
    benchmark = fetch_cached_benchmark(provider, benchmark_symbol, start_date, end_date, raw_dir, force_refresh)

    prices_path = write_processed_prices(prices, processed_dir)
    benchmark_path = write_processed_benchmark(benchmark, processed_dir)
    fetch_log_path = append_fetch_log(
        processed_dir,
        [
            {
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "source": getattr(provider, "source", "unknown"),
                "dataset": "prices",
                "symbols": ",".join(symbols),
                "start_date": start_date,
                "end_date": end_date,
                "rows": len(prices),
                "force_refresh": force_refresh,
            },
            {
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "source": getattr(provider, "source", "unknown"),
                "dataset": "benchmark",
                "symbols": benchmark_symbol,
                "start_date": start_date,
                "end_date": end_date,
                "rows": len(benchmark),
                "force_refresh": force_refresh,
            },
        ],
    )
    return FetchResult(prices_path, benchmark_path, fetch_log_path, len(prices), len(benchmark))


def fetch_etf_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    return AKShareDataProvider().fetch_etf_daily(symbol, start_date, end_date)
