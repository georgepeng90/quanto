import json
from dataclasses import replace

import pandas as pd
import pytest

from quanto.backtest.engine import run_backtest
from quanto.reports.backtest_report import build_report, write_backtest_outputs
from tests.fixtures.engine_cases import ScriptedStrategy, bundle_case, spec_case, target


def _case(days=5, intents=None, values=None):
    bundle = bundle_case(days=days, values=values)
    spec = spec_case(bundle)
    result = run_backtest(bundle, ScriptedStrategy(intents or {}), spec, lambda event: None, lambda: False)
    return result, bundle, spec


def test_report_rejects_overwrite(tmp_path):
    result, bundle, spec = _case()
    report = build_report(result, bundle, spec)
    target_dir = tmp_path / "run"
    write_backtest_outputs(target_dir, result, report)
    with pytest.raises(FileExistsError):
        write_backtest_outputs(target_dir, result, report)


def test_export_records_schema_and_mode_for_empty_trade_tables(tmp_path):
    result, bundle, spec = _case()
    report = build_report(result, bundle, spec)
    files = write_backtest_outputs(tmp_path / "run", result, report)
    metadata = json.loads((tmp_path / "run" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["schema_version"] == "1"
    assert metadata["price_mode"] == "raw_cash"
    assert metadata["benchmark_type"] == "price_index"
    assert metadata["metric_definition_version"] == spec.metrics.definition_version
    assert metadata["table_columns"]["fills"] == list(result.fills.columns)
    assert "metadata.json" in files
    assert (tmp_path / "run" / "fills.csv").read_text(encoding="utf-8").startswith("order_id,trade_date")


def test_benchmark_is_normalized_and_period_returns_compound():
    result, bundle, spec = _case(days=4, values={(2, "A"): (10, 11), (3, "A"): (11, 12), (4, "A"): (12, 12)})
    bundle.benchmark.loc[:, "close"] = [100, 120, 90, 110]
    report = build_report(result, bundle, spec)
    assert report.curves["benchmark_equity"].iloc[0] == pytest.approx(spec.initial_cash)
    assert report.curves["benchmark_equity"].iloc[-1] == pytest.approx(1100)
    assert report.metric_definitions["benchmark_equity"]["kind"] == "price_index"
    assert report.metric_definitions["benchmark_equity"]["period"] == "spec trading sessions"
    assert (1 + report.monthly_returns["return"]).prod() - 1 == pytest.approx(report.metrics["total_return"])
    assert (1 + report.annual_returns["return"]).prod() - 1 == pytest.approx(report.metrics["total_return"])


def test_split_adjusts_fifo_matching_and_round_trip_counts_episodes():
    result, bundle, spec = _case(days=5)
    fills = pd.DataFrame([
        dict(order_id="b1", trade_date="2024-01-02", symbol="A", side="buy", quantity=100, price=10, commission=10, slippage_cost=0, sellable_date="2024-01-03"),
        dict(order_id="b2", trade_date="2024-01-03", symbol="A", side="buy", quantity=100, price=10, commission=0, slippage_cost=0, sellable_date="2024-01-04"),
        dict(order_id="s1", trade_date="2024-01-04", symbol="A", side="sell", quantity=100, price=8, commission=5, slippage_cost=0, sellable_date=None),
        dict(order_id="s2", trade_date="2024-01-05", symbol="A", side="sell", quantity=300, price=8, commission=0, slippage_cost=0, sellable_date=None),
    ])
    events = pd.DataFrame([dict(date="2024-01-04", event_type="split", symbol="A", amount=0, details={"split_ratio": 2})])
    report = build_report(replace(result, fills=fills, account_events=events), bundle, spec)
    assert report.metrics["fill_count"] == 4
    assert report.metrics["round_trip_count"] == 1
    assert report.matched_trades["quantity"].sum() == 400
    assert report.matched_trades["realized_pnl"].sum() == pytest.approx(1185)
    assert report.open_lots.empty


def test_html_escapes_embedded_script_closer_and_outputs_are_deterministic(tmp_path):
    result, bundle, spec = _case()
    signals = result.signals.copy()
    signals.at[0, "diagnostics"] = {"label": "</script><img src=x onerror=alert(1)>"}
    result = replace(result, signals=signals)
    report = build_report(result, bundle, spec)
    first = write_backtest_outputs(tmp_path / "a", result, report)
    second = write_backtest_outputs(tmp_path / "b", result, report)
    assert first == second
    html = (tmp_path / "a" / "report.html").read_text(encoding="utf-8")
    assert "</script><img" not in html
    assert "\\u003c/script\\u003e" in html
    assert "<svg" in html
    assert "<h2>指标定义</h2>" in html
    assert "https://" not in html
    assert json.loads((tmp_path / "a" / "metrics.json").read_text(encoding="utf-8"))["schema_version"] == "1"


def test_canonical_csv_hash_ignores_order_table_row_and_column_order(tmp_path):
    result, bundle, spec = _case(intents={1: target(A=0.5)})
    report = build_report(result, bundle, spec)
    baseline = write_backtest_outputs(tmp_path / "base", result, report)
    reordered = replace(result, orders=result.orders.iloc[::-1, ::-1].reset_index(drop=True))
    changed = write_backtest_outputs(tmp_path / "reordered", reordered, report)
    assert changed["orders.csv"] == baseline["orders.csv"]


def test_nullable_fill_date_is_blank_in_csv(tmp_path):
    result, bundle, spec = _case()
    fill = pd.DataFrame([dict(order_id="s1", trade_date=pd.Timestamp("2024-01-02"), symbol="A", side="sell",
        quantity=1, price=10, commission=0, slippage_cost=0, sellable_date=pd.NaT)])
    report = build_report(result, bundle, spec)
    write_backtest_outputs(tmp_path / "run", replace(result, fills=fill), report)
    assert "NaT" not in (tmp_path / "run" / "fills.csv").read_text(encoding="utf-8")
