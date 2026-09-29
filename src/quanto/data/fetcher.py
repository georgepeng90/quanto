from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import time
from typing import Callable

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
    failed_symbols: tuple[str, ...] = ()
    complete: bool = False
    coverage: dict | None = None


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

    def fetch_calendar(self, start_date: str, end_date: str) -> list[str]:
        dates = pd.to_datetime(self.ak.tool_trade_date_hist_sina()["trade_date"])
        start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
        horizon = end + pd.Timedelta(days=7)
        if dates.empty or dates.min() > start or dates.max() < horizon:
            raise ValueError("CALENDAR_INCOMPLETE: 交易日历未覆盖请求区间")
        observed = [day.date().isoformat() for day in dates if start <= day <= horizon]
        if not any(day > end.date().isoformat() for day in observed):
            raise ValueError("CALENDAR_INCOMPLETE: 缺少后续交易日用于判断周末")
        return observed


def _csv_cache_path(raw_dir: str | Path, category: str, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.startswith("."):
        raise ValueError("invalid cache identity")
    return Path(raw_dir) / category / f"{name}.csv"


def _cache_covers(df: pd.DataFrame, start_date: str, end_date: str, expected_sessions: list[str] | None = None) -> bool:
    if df.empty or "date" not in df.columns:
        return False
    dates = pd.to_datetime(df["date"])
    if expected_sessions is not None:
        return set(pd.to_datetime(expected_sessions)).issubset(set(dates))
    return dates.min() <= pd.to_datetime(start_date) and dates.max() >= pd.to_datetime(end_date)


def _read_cached_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    return pd.read_csv(path, dtype={"symbol": str})


def _observed(frame: pd.DataFrame, source: str, error: str | None = None) -> pd.DataFrame:
    frame.attrs["source"] = source
    if error:
        frame.attrs["error"] = error
    return frame


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
    max_retries: int = 3,
    retry_delay_seconds: float = 2.0,
    log: bool = False,
    expected_sessions: list[str] | None = None,
    cache_version: str | None = None,
) -> pd.DataFrame:
    cache_path = _csv_cache_path(raw_dir, "etf", symbol if cache_version is None else f"{symbol}-{cache_version}")
    cached = _read_cached_csv(cache_path)
    if cached is not None and not force_refresh and _cache_covers(cached, start_date, end_date, expected_sessions):
        if log:
            print(f"[cache] ETF {symbol}: {cache_path}")
        return _observed(normalize_prices(cached), "cache")
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            if log:
                print(f"[fetch] ETF {symbol}: attempt {attempt + 1}/{max_retries + 1}")
            df = provider.fetch_etf_daily(symbol, start_date, end_date)
            break
        except Exception as exc:
            last_error = exc
            if log:
                print(f"[retry] ETF {symbol}: {exc}")
            if attempt < max_retries:
                time.sleep(retry_delay_seconds)
    else:
        if cached is not None and not force_refresh:
            if log:
                print(f"[fallback] ETF {symbol}: using partial cache after fetch failure")
            return _observed(normalize_prices(cached), "fallback", type(last_error).__name__ if last_error else None)
        raise last_error or RuntimeError(f"Failed to fetch ETF {symbol}")
    _write_raw_csv(cache_path, df)
    if log:
        print(f"[write] ETF {symbol}: {len(df)} rows -> {cache_path}")
    return _observed(df, "fetched")


def fetch_cached_benchmark(
    provider: AKShareDataProvider,
    symbol: str,
    start_date: str,
    end_date: str,
    raw_dir: str | Path,
    force_refresh: bool = False,
    max_retries: int = 3,
    retry_delay_seconds: float = 2.0,
    log: bool = False,
    expected_sessions: list[str] | None = None,
    cache_version: str | None = None,
) -> pd.DataFrame:
    cache_path = _csv_cache_path(raw_dir, "benchmark", symbol if cache_version is None else f"{symbol}-{cache_version}")
    cached = _read_cached_csv(cache_path)
    if cached is not None and not force_refresh and _cache_covers(cached, start_date, end_date, expected_sessions):
        if log:
            print(f"[cache] benchmark {symbol}: {cache_path}")
        return _observed(normalize_benchmark(cached), "cache")
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            if log:
                print(f"[fetch] benchmark {symbol}: attempt {attempt + 1}/{max_retries + 1}")
            df = provider.fetch_benchmark_daily(symbol, start_date, end_date)
            break
        except Exception as exc:
            last_error = exc
            if log:
                print(f"[retry] benchmark {symbol}: {exc}")
            if attempt < max_retries:
                time.sleep(retry_delay_seconds)
    else:
        if cached is not None and not force_refresh:
            if log:
                print(f"[fallback] benchmark {symbol}: using partial cache after fetch failure")
            return _observed(normalize_benchmark(cached), "fallback", type(last_error).__name__ if last_error else None)
        raise last_error or RuntimeError(f"Failed to fetch benchmark {symbol}")
    _write_raw_csv(cache_path, df)
    if log:
        print(f"[write] benchmark {symbol}: {len(df)} rows -> {cache_path}")
    return _observed(df, "fetched")


def build_processed_dataset(
    symbols: list[str],
    start_date: str,
    end_date: str,
    processed_dir: str | Path,
    raw_dir: str | Path,
    benchmark_symbol: str = "sh000300",
    force_refresh: bool = False,
    provider: AKShareDataProvider | None = None,
    request_delay_seconds: float = 1.0,
    max_retries: int = 3,
    retry_delay_seconds: float = 2.0,
    continue_on_error: bool = True,
    log: bool = False,
    expected_sessions: list[str] | None = None,
    cache_version: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> FetchResult:
    provider = provider or AKShareDataProvider()
    price_frames: list[pd.DataFrame] = []
    failed_symbols: list[str] = []
    coverage: dict = {}
    for index, symbol in enumerate(symbols):
        if cancelled is not None and cancelled():
            raise RuntimeError("CANCELLED: 数据采集已取消")
        if log:
            print(f"[{index + 1}/{len(symbols)}] ETF {symbol}")
        if index > 0 and request_delay_seconds > 0:
            time.sleep(request_delay_seconds)
        try:
            frame = fetch_cached_etf(
                    provider,
                    symbol,
                    start_date,
                    end_date,
                    raw_dir,
                    force_refresh,
                    max_retries=max_retries,
                    retry_delay_seconds=retry_delay_seconds,
                    log=log,
                    expected_sessions=expected_sessions,
                    cache_version=cache_version,
                )
            source = frame.attrs.get("source", "unknown")
            error = frame.attrs.get("error")
            frame = frame.loc[pd.to_datetime(frame["date"]).between(pd.Timestamp(start_date), pd.Timestamp(end_date))].copy()
            price_frames.append(frame)
            observed = {day.date().isoformat() for day in pd.to_datetime(frame["date"])}
            coverage[symbol] = {"observed": sorted(observed), "missing": sorted(set(expected_sessions or []) - observed),
                                "source": source, "cache_version": cache_version}
            if error:
                coverage[symbol]["error"] = error
            if coverage[symbol]["missing"]:
                failed_symbols.append(symbol)
        except Exception:
            if not continue_on_error:
                raise
            failed_symbols.append(symbol)
            coverage[symbol] = {"observed": [], "missing": sorted(expected_sessions or []), "error": "FETCH_FAILED", "cache_version": cache_version}
            if log:
                print(f"[skip] ETF {symbol}: failed after retries and no usable cache")
    prices = normalize_prices(pd.concat(price_frames, ignore_index=True) if price_frames else pd.DataFrame(columns=PRICE_COLUMNS))
    if cancelled is not None and cancelled():
        raise RuntimeError("CANCELLED: 数据采集已取消")
    try:
        benchmark = fetch_cached_benchmark(
            provider, benchmark_symbol, start_date, end_date, raw_dir, force_refresh,
            max_retries=max_retries, retry_delay_seconds=retry_delay_seconds, log=log,
            expected_sessions=expected_sessions, cache_version=cache_version)
        benchmark_error = None
        benchmark_source = benchmark.attrs.get("source", "unknown")
        benchmark = benchmark.loc[pd.to_datetime(benchmark["date"]).between(pd.Timestamp(start_date), pd.Timestamp(end_date))].copy()
    except Exception as exc:
        benchmark = pd.DataFrame(columns=BENCHMARK_COLUMNS)
        benchmark_error = type(exc).__name__
        benchmark_source = "failed"
    benchmark_observed = {day.date().isoformat() for day in pd.to_datetime(benchmark["date"])}
    coverage["benchmark"] = {"observed": sorted(benchmark_observed), "missing": sorted(set(expected_sessions or []) - benchmark_observed),
                             "source": benchmark_source, "cache_version": cache_version}
    if benchmark_error:
        coverage["benchmark"]["error"] = benchmark_error
    complete = bool(expected_sessions) and not failed_symbols and all(not item["missing"] for item in coverage.values())

    prices_path = write_processed_prices(prices, processed_dir)
    benchmark_path = write_processed_benchmark(benchmark, processed_dir)
    if log:
        print(f"[processed] prices: {len(prices)} rows -> {prices_path}")
        print(f"[processed] benchmark: {len(benchmark)} rows -> {benchmark_path}")
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
                "failed_symbols": ",".join(failed_symbols),
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
    return FetchResult(prices_path, benchmark_path, fetch_log_path, len(prices), len(benchmark), tuple(failed_symbols), complete, coverage)


def fetch_etf_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    return AKShareDataProvider().fetch_etf_daily(symbol, start_date, end_date)
