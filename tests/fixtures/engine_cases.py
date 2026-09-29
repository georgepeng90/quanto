import pandas as pd
from quanto.config import BacktestSpec
from quanto.data.schemas import DatasetBundle, DatasetManifest
from quanto.strategies.base import Hold, TargetPortfolio


def target(**weights):
    return TargetPortfolio(weights, {}, {})


class ScriptedStrategy:
    warmup_bars = 0

    def __init__(self, intents, evaluate=True):
        self.intents, self.evaluate, self.contexts = intents, evaluate, []

    def should_evaluate(self, ctx):
        return self.evaluate

    def on_close(self, ctx):
        self.contexts.append(ctx)
        return self.intents.get(ctx.asof.day, Hold("保持"))


def bundle_case(values=None, symbols=("A",), days=5):
    dates = pd.bdate_range("2024-01-01", periods=days)
    rows = []
    for date in dates:
        for symbol in symbols:
            op, close = (values or {}).get((date.day, symbol), (10, 10))
            rows.append(dict(date=date, symbol=symbol, open=op, close=close, high=max(op, close), low=min(op, close), volume=10000, amount=100000))
    prices = pd.DataFrame(rows)
    manifest = DatasetManifest(source="fixture", is_demo=True, price_mode="raw_cash", currency="CNY",
        calendar_start=dates[0].date(), calendar_end=pd.Timestamp("2024-01-15").date(), data_start=dates[0].date(), data_end=dates[-1].date(),
        volume_unit="share", amount_unit="CNY", event_coverage="complete", benchmark_type="price_index", field_semantics={}, universe_rule={})
    return DatasetBundle(manifest, prices, pd.DataFrame({"date": dates, "close": 10, "turnover": 100}),
        pd.DataFrame({"date": pd.bdate_range(dates[0], "2024-01-15")}),
        pd.DataFrame([dict(symbol=s, lot_size=1, settlement_sessions=1, listing_date=dates[0], group="G") for s in symbols]),
        prices[["date", "symbol"]].assign(status="trading", can_buy=True, can_sell=True),
        pd.DataFrame(columns=["symbol", "event_type", "record_date", "ex_date", "pay_date", "cash_per_share", "split_ratio"]),
        prices[["date", "symbol"]].assign(eligible=True))


def spec_case(bundle, **changes):
    data = dict(start=bundle.manifest.data_start, end=bundle.manifest.data_end, symbols=bundle.instruments.symbol.tolist(), initial_cash=1000, seed=1,
        execution=dict(commission_rate=0, minimum_commission=0, slippage_rate=0, max_stale_sessions=3, model="next_open"),
        risk=dict(max_total_weight=1, max_symbol_weight=1, group_caps={}, stress_scenarios=[], pause_action="block_new", recovery_mode="never"),
        metrics=dict(annual_sessions=252, risk_free_rate=0, cash_rate=0, definition_version="1"))
    for key, value in changes.items():
        if isinstance(value, dict):
            data[key].update(value)
        else:
            data[key] = value
    return BacktestSpec.model_validate(data)
