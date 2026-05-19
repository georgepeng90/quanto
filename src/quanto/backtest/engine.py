from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from quanto.factors import compute_factor_panel, score_factors
from quanto.reports.backtest_report import write_backtest_outputs
from quanto.risk.market_regime import classify_market_regime
from quanto.risk.position_sizing import size_positions, trailing_volatility

from .metrics import compute_metrics


@dataclass
class BacktestResult:
    run_dir: Path
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    positions: pd.DataFrame
    signals: pd.DataFrame
    metrics: dict


def _weekly_signal_dates(prices: pd.DataFrame) -> pd.Series:
    dates = pd.Series(sorted(prices["date"].drop_duplicates()))
    # Calendar week end maps the last available trading day in each week.
    return dates.groupby(dates.dt.to_period("W-FRI")).tail(1).reset_index(drop=True)


def _build_targets(scores: pd.DataFrame, prices: pd.DataFrame, top_n: int) -> pd.DataFrame:
    signal_dates = _weekly_signal_dates(prices)
    trading_dates = pd.Series(sorted(prices["date"].drop_duplicates()))
    targets: list[pd.DataFrame] = []
    for signal_date in signal_dates:
        future_dates = trading_dates[trading_dates > signal_date]
        if future_dates.empty:
            continue
        trade_date = future_dates.iloc[0]
        ranked = scores[(scores["signal_date"] == signal_date) & (scores["rank"] <= top_n)].copy()
        if ranked.empty:
            continue
        ranked["trade_date"] = trade_date
        ranked["strategy_weight"] = 1.0 / len(ranked)
        ranked["exit_reason"] = ""
        targets.append(ranked)
    if not targets:
        return pd.DataFrame(columns=["signal_date", "trade_date", "symbol", "rank", "score", "strategy_weight"])
    return pd.concat(targets, ignore_index=True)


def _execute_rebalances(
    prices: pd.DataFrame,
    sized_targets: pd.DataFrame,
    initial_cash: float,
    commission_rate: float,
    slippage_rate: float,
    max_symbol_weight: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    price_panel = prices.pivot(index="date", columns="symbol", values="close").sort_index()
    open_panel = prices.pivot(index="date", columns="symbol", values="open").sort_index()
    dates = list(price_panel.index)
    all_symbols = list(price_panel.columns)
    target_by_date = {
        date: group.set_index("symbol")["target_weight"].to_dict()
        for date, group in sized_targets.groupby("trade_date")
    }

    cash = float(initial_cash)
    shares = {symbol: 0 for symbol in all_symbols}
    trades: list[dict] = []
    equity_rows: list[dict] = []
    position_rows: list[dict] = []

    for date in dates:
        close_prices = price_panel.loc[date]
        open_prices = open_panel.loc[date]
        equity_before = cash + sum(shares[s] * close_prices.get(s, 0) for s in all_symbols if pd.notna(close_prices.get(s)))

        if date in target_by_date:
            desired = {symbol: 0.0 for symbol in all_symbols}
            desired.update(target_by_date[date])
            for symbol in all_symbols:
                price = open_prices.get(symbol)
                if pd.isna(price) or price <= 0:
                    continue
                current_value = shares[symbol] * price
                target_value = equity_before * desired.get(symbol, 0.0)
                delta_value = target_value - current_value
                if abs(delta_value) < price:
                    continue
                side = "buy" if delta_value > 0 else "sell"
                trade_price = price * (1 + slippage_rate if side == "buy" else 1 - slippage_rate)
                qty = int(abs(delta_value) // trade_price)
                if qty <= 0:
                    continue
                if side == "buy":
                    max_affordable = int(cash // (trade_price * (1 + commission_rate)))
                    qty = min(qty, max_affordable)
                    if qty <= 0:
                        continue
                    notional = qty * trade_price
                    cost = notional * commission_rate
                    cash -= notional + cost
                    shares[symbol] += qty
                else:
                    qty = min(qty, shares[symbol])
                    if qty <= 0:
                        continue
                    notional = qty * trade_price
                    cost = notional * commission_rate
                    cash += notional - cost
                    shares[symbol] -= qty
                trades.append(
                    {
                        "trade_date": date,
                        "symbol": symbol,
                        "side": side,
                        "price": trade_price,
                        "shares": qty,
                        "notional": notional,
                        "cost": cost,
                        "reason": "rebalance",
                    }
                )

        close_prices = price_panel.loc[date]
        equity = cash + sum(shares[s] * close_prices.get(s, 0) for s in all_symbols if pd.notna(close_prices.get(s)))
        for symbol in all_symbols:
            price = close_prices.get(symbol)
            if pd.isna(price) or price <= 0 or shares[symbol] <= 0 or equity <= 0:
                continue
            market_value = shares[symbol] * price
            if market_value / equity <= max_symbol_weight:
                continue
            excess_value = market_value - equity * max_symbol_weight
            trade_price = price * (1 - slippage_rate)
            qty = min(shares[symbol], int(excess_value // trade_price) + 1)
            if qty <= 0:
                continue
            notional = qty * trade_price
            cost = notional * commission_rate
            cash += notional - cost
            shares[symbol] -= qty
            trades.append(
                {
                    "trade_date": date,
                    "symbol": symbol,
                    "side": "sell",
                    "price": trade_price,
                    "shares": qty,
                    "notional": notional,
                    "cost": cost,
                    "reason": "risk_cap",
                }
            )
            equity = cash + sum(shares[s] * close_prices.get(s, 0) for s in all_symbols if pd.notna(close_prices.get(s)))
        equity_rows.append({"date": date, "cash": cash, "equity": equity})
        for symbol in all_symbols:
            price = close_prices.get(symbol)
            market_value = 0.0 if pd.isna(price) else shares[symbol] * price
            position_rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "shares": shares[symbol],
                    "market_value": market_value,
                    "weight": market_value / equity if equity else 0.0,
                }
            )

    return pd.DataFrame(equity_rows), pd.DataFrame(trades), pd.DataFrame(position_rows)


def run_backtest(
    prices: pd.DataFrame,
    benchmark: pd.DataFrame,
    config: dict,
    output_dir: str | Path,
    run_id: str = "latest",
) -> BacktestResult:
    prices = prices.sort_values(["date", "symbol"]).copy()
    benchmark = benchmark.sort_values("date").copy()

    factors = compute_factor_panel(prices, config["factors"])
    scores = score_factors(factors, config["factors"])
    raw_targets = _build_targets(scores, prices, int(config["strategy"].get("top_n", 3)))
    regime_config = {**config.get("market_regime", {}), "cautious_total_weight": config["risk"].get("cautious_total_weight", 0.30)}
    regime = classify_market_regime(benchmark, regime_config)
    vol = trailing_volatility(prices)
    sized = size_positions(raw_targets, vol, regime, config["risk"])

    signals = raw_targets.merge(
        sized[["trade_date", "symbol", "target_weight", "regime"]],
        on=["trade_date", "symbol"],
        how="left",
    )
    equity_curve, trades, positions = _execute_rebalances(
        prices=prices,
        sized_targets=sized,
        initial_cash=float(config["backtest"].get("initial_cash", 100000)),
        commission_rate=float(config["backtest"].get("commission_rate", 0.0005)),
        slippage_rate=float(config["backtest"].get("slippage_rate", 0.0005)),
        max_symbol_weight=float(config["risk"].get("max_symbol_weight", 0.15)),
    )
    metrics = compute_metrics(equity_curve, trades)
    run_dir = write_backtest_outputs(output_dir, run_id, equity_curve, trades, positions, signals, metrics)
    return BacktestResult(run_dir, equity_curve, trades, positions, signals, metrics)
