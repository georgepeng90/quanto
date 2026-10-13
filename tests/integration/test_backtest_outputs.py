import json

from quanto.backtest.engine import run_backtest
from quanto.backtest.models import TABLE_COLUMNS
from quanto.reports.backtest_report import build_report, write_backtest_outputs
from quanto.strategies.buy_hold import BuyHold
from quanto.strategies.trend_rotation import TrendRotation
from quanto.workbench.worker import validate_backtest_output
from tests.fixtures.engine_cases import bundle_case, spec_case


def test_both_templates_write_standalone_reports(tmp_path):
    bundle = bundle_case(days=8)
    spec = spec_case(bundle)
    strategies = (BuyHold(), TrendRotation(2, 1, 1, 1, "daily"))
    for index, strategy in enumerate(strategies):
        result = run_backtest(bundle, strategy, spec, lambda event: None, lambda: False)
        report = build_report(result, bundle, spec)
        output = tmp_path / str(index)
        manifest = write_backtest_outputs(output, result, report)
        assert json.loads((output / "metadata.json").read_bytes())["schema_version"] == "4"
        validate_backtest_output(output, manifest)
        assert "report.html" in manifest
        assert (output / "report.html").read_text(encoding="utf-8").startswith("<!doctype html>")
        for name, columns in TABLE_COLUMNS.items():
            assert list(getattr(result, name).columns) == columns
            assert (output / f"{name}.csv").exists()
