from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path

import numpy as np
import pandas as pd

from quanto.backtest.metrics import MATCH_COLUMNS, OPEN_COLUMNS, compute_metrics, match_trades
from quanto.backtest.models import BacktestResult, TABLE_COLUMNS
from quanto.config import BacktestSpec
from quanto.data.schemas import DatasetBundle


SCHEMA_VERSION = "2"
REPORT_TABLE_COLUMNS = {"curves": ["date", "equity", "return", "drawdown", "benchmark_equity", "benchmark_return"],
    "monthly_returns": ["period", "return"], "annual_returns": ["period", "return"],
    "matched_trades": MATCH_COLUMNS, "open_lots": OPEN_COLUMNS}
OUTPUT_TABLES = (*TABLE_COLUMNS, *REPORT_TABLE_COLUMNS)
OUTPUT_FILES = tuple(f"{name}.csv" for name in OUTPUT_TABLES) + ("metrics.json", "metadata.json", "report.html", "manifest.json")


@dataclass(frozen=True)
class ReportBundle:
    metrics: dict[str, float | int | None]
    metric_definitions: dict[str, dict]
    curves: pd.DataFrame
    monthly_returns: pd.DataFrame
    annual_returns: pd.DataFrame
    matched_trades: pd.DataFrame
    open_lots: pd.DataFrame
    warnings: list[str]
    metadata: dict
    artifact_manifest: tuple[str, ...]


def _period_returns(curves: pd.DataFrame, frequency: str) -> pd.DataFrame:
    period = pd.to_datetime(curves.date).dt.to_period(frequency).astype(str)
    return pd.DataFrame({"period": period, "return": curves["return"]}).groupby("period", sort=True)["return"].apply(
        lambda values: float((1 + values).prod() - 1)).reset_index()


def _definitions(spec: BacktestSpec, bundle: DatasetBundle) -> dict[str, dict]:
    return {
        "total_return": {"description": "最终权益相对初始现金的累计收益；首个交易日从初始现金计算", "formula": "final_equity / initial_cash - 1"},
        "annual_return": {"description": "按观测交易日年化；不足两个绩效日为空", "formula": "(final_equity / initial_cash) ** (annual_sessions / observed_sessions) - 1", "annual_sessions": spec.metrics.annual_sessions},
        "volatility": {"description": "包含首日收益的日收益总体标准差乘交易日数平方根；不足两个绩效日为空", "formula": "std(returns, ddof=0) * sqrt(annual_sessions)"},
        "sharpe": {"description": "日超额收益均值除日收益总体标准差再乘交易日数平方根；零方差或不足两个绩效日为空", "risk_free_period_rate": spec.metrics.risk_free_rate / spec.metrics.annual_sessions, "risk_free_convention": "名义年利率除以 annual_sessions；与引擎现金利息 cash_rate / annual_sessions 使用同一交易日口径"},
        "max_drawdown": {"description": "初始现金及完整日权益序列的峰谷最大回撤", "formula": "max(1 - equity / running_peak)"},
        "calmar": {"description": "年化收益除最大回撤；零回撤或不足两个绩效日为空"},
        "fill_count": {"description": "实际成交记录数；部分成交各记一笔"},
        "final_equity": {"description": "最后一个已完成交易日的现金、应收和持仓市值之和"},
        "round_trip_count": {"description": "每个代码从空仓到持仓再回到空仓算一次完整往返；多笔建仓或部分卖出不增加次数；拆分延续同一持仓阶段"},
        "open_lot_count": {"description": "期末未平仓 FIFO 批次数，按每笔买入留存数量计"},
        "realized_pnl": {"description": "按 FIFO 匹配的已平仓价差，成交价已包含滑点；买卖佣金分别计入成本和扣减收入；不包含现金分红"},
        "unrealized_pnl": {"description": "最终已完成绩效日 positions 的未实现盈亏之和；当天没有持仓则为零，与已实现盈亏分列", "period": "final observed session", "formula": "sum(final_positions.unrealized_pnl)"},
        "turnover": {"description": "观察区间内买卖成交额之和除以日均权益；没有成交为零", "period": "all observed sessions", "denominator": "arithmetic mean of daily equity", "formula": "sum(fill.quantity * fill.price) / mean(daily_equity)"},
        "commission_paid": {"description": "观察区间内所有成交佣金之和；无成交为零", "period": "all observed sessions", "unit": bundle.manifest.currency},
        "slippage_cost": {"description": "观察区间内成交记录的滑点成本之和；已包含在成交价和权益中，不再扣减", "period": "all observed sessions", "unit": bundle.manifest.currency},
        "execution_cost": {"description": "佣金加记录的滑点成本，仅展示执行代价；不从已含成本的净收益再次扣减", "period": "all observed sessions", "formula": "commission_paid + slippage_cost", "unit": bundle.manifest.currency},
        "average_invested_ratio": {"description": "观察期每天持仓市值除当天权益，再对所有绩效日做算术平均；空仓日计零", "period": "all observed sessions", "denominator": "observed session count", "formula": "mean(daily_market_value / daily_equity)"},
        "average_holding_sessions": {"description": "FIFO 已平仓匹配行按平仓数量加权的持有交易日；没有平仓匹配时为空，未平仓批次单列", "denominator": "matched closed quantity"},
        "dividend_income": {"description": "现金分红支付事件之和，与交易价差分开"},
        "interest_income": {"description": "现金利息事件之和；引擎在开盘成交前的现金基数上按 cash_rate / annual_sessions 计息，收盘记账"},
        "benchmark_equity": {"description": "基准首个绩效日收盘价归一到初始现金；基准首日收益为零", "kind": bundle.manifest.benchmark_type, "period": "spec trading sessions", "source": bundle.manifest.source},
        "benchmark_return": {"description": "基准首个至最后一个已观测绩效日的累计收益；首日归一后收益为零", "period": "first through final observed benchmark close", "formula": "last_benchmark_close / first_benchmark_close - 1", "kind": bundle.manifest.benchmark_type},
        "relative_return_difference": {"description": "策略总收益减同区间基准累计收益，单位为收益率差（百分点），不是财富比值", "period": "first through final observed benchmark close", "formula": "total_return - benchmark_return (percentage-point difference)"},
        "schema_version": {"value": SCHEMA_VERSION, "metric_definition_version": spec.metrics.definition_version},
    }


def build_report(result: BacktestResult, bundle: DatasetBundle, spec: BacktestSpec) -> ReportBundle:
    metrics = compute_metrics(result, spec)
    daily = result.equity.sort_values("date", kind="stable").reset_index(drop=True)
    dates = pd.to_datetime(daily.date)
    if dates.duplicated().any():
        raise ValueError("DUPLICATE_EQUITY_DATE: 权益日期重复")
    benchmark = bundle.benchmark.set_index(pd.to_datetime(bundle.benchmark.date))["close"].reindex(dates).astype(float)
    if benchmark.isna().any() or (benchmark <= 0).any() or not np.isfinite(benchmark.to_numpy()).all():
        raise ValueError("INVALID_BENCHMARK: 基准缺少绩效日或价格无效")
    equity = daily.equity.astype(float).reset_index(drop=True)
    returns = equity.div(pd.concat([pd.Series([spec.initial_cash]), equity.iloc[:-1]], ignore_index=True)).sub(1)
    benchmark = benchmark.reset_index(drop=True)
    benchmark_ratio = float(benchmark.iloc[-1]) / float(benchmark.iloc[0])
    if not np.isfinite(benchmark_ratio):
        raise ValueError("INVALID_BENCHMARK: 基准收益溢出")
    benchmark_equity = benchmark.div(benchmark.iloc[0]).mul(spec.initial_cash)
    benchmark_return = benchmark.pct_change().fillna(0.0)
    metrics["benchmark_return"] = benchmark_ratio - 1
    metrics["relative_return_difference"] = metrics["total_return"] - metrics["benchmark_return"]
    if not np.isfinite(benchmark_equity.to_numpy()).all() or not np.isfinite(benchmark_return.to_numpy()).all() or not np.isfinite(metrics["relative_return_difference"]):
        raise ValueError("INVALID_BENCHMARK: 归一化基准或相对收益无效")
    peak = pd.concat([pd.Series([spec.initial_cash]), equity], ignore_index=True).cummax().iloc[1:].reset_index(drop=True)
    curves = pd.DataFrame({"date": dates, "equity": equity, "return": returns,
        "drawdown": equity.div(peak).sub(1), "benchmark_equity": benchmark_equity,
        "benchmark_return": benchmark_return})
    matched, open_lots, _ = match_trades(result)
    warnings = list(bundle.manifest.limitations)
    if bundle.manifest.is_demo:
        warnings.insert(0, "演示数据：合成绩效，不代表真实市场表现")
    if bundle.manifest.benchmark_type != "total_return":
        warnings.append("基准为价格指数或其他口径，未必包含分红；比较收益时请核对口径。")
    metadata = {"schema_version": SCHEMA_VERSION, "metric_definition_version": spec.metrics.definition_version,
        "price_mode": bundle.price_mode, "benchmark_type": bundle.manifest.benchmark_type,
        "benchmark_source": bundle.manifest.source, "currency": bundle.manifest.currency,
        "is_demo": bundle.manifest.is_demo, "source": bundle.manifest.source,
        "sample_start": spec.start, "sample_end": spec.end,
        "completed_through": result.completed_through,
        "table_columns": {name: columns for name, columns in TABLE_COLUMNS.items()},
        "csv_convention": "UTF-8; header in canonical column order; ISO dates; 12 decimal places for floats; stable row sorting",
        "metric_period": "observed trading sessions", "market_timezone": "Asia/Shanghai"}
    return ReportBundle(metrics, _definitions(spec, bundle), curves,
        _period_returns(curves, "M"), _period_returns(curves, "Y"),
        matched, open_lots, warnings, metadata, OUTPUT_FILES)


def _normalized(value):
    if value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat() if value.tzinfo is None else value.tz_convert("UTC").isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat() if value.tzinfo is None else value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.generic):
        return _normalized(value.item())
    if isinstance(value, dict):
        return {str(key): _normalized(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not np.isfinite(value):
            raise ValueError("NONFINITE_OUTPUT: 产物含非有限数值")
        return value
    if pd.isna(value):
        return None
    raise TypeError(f"Unsupported report value: {type(value).__name__}")


def _json_bytes(value) -> bytes:
    return (json.dumps(_normalized(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _records(frame: pd.DataFrame) -> list[dict]:
    return [_normalized(row) for row in frame.to_dict("records")]


def _csv_bytes(frame: pd.DataFrame) -> bytes:
    out = frame.copy()
    for column in out.columns:
        out[column] = out[column].map(lambda value: json.dumps(_normalized(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if isinstance(value, (dict, list, tuple)) else _normalized(value))
    if len(out):
        out = out.sort_values(list(out.columns), kind="stable", key=lambda column: column.map(str)).reset_index(drop=True)
    return out.to_csv(index=False, lineterminator="\n", float_format="%.12f").encode("utf-8")


def _svg(curves: pd.DataFrame) -> str:
    values = curves.equity.to_numpy(dtype=float)
    benchmark = curves.benchmark_equity.to_numpy(dtype=float)
    low, high = min(values.min(), benchmark.min()), max(values.max(), benchmark.max())
    span = high - low or 1.0
    def points(series):
        return " ".join(f"{40 + index * 700 / max(1, len(series) - 1):.1f},{220 - (value - low) * 180 / span:.1f}"
            for index, value in enumerate(series))
    return ("<svg viewBox=\"0 0 780 250\" role=\"img\" aria-label=\"策略与基准权益曲线\">"
        "<rect width=\"780\" height=\"250\" fill=\"#fff\"/>"
        f"<polyline fill=\"none\" stroke=\"#1463a5\" stroke-width=\"2\" points=\"{points(values)}\"/>"
        f"<polyline fill=\"none\" stroke=\"#c17b1d\" stroke-width=\"2\" points=\"{points(benchmark)}\"/>"
        "</svg>")


def _html(report: ReportBundle, result: BacktestResult) -> bytes:
    embedded = _json_bytes({"metrics": report.metrics, "metric_definitions": report.metric_definitions,
        "curves": _records(report.curves), "signals": _records(result.signals), "warnings": report.warnings, "metadata": report.metadata}).decode("utf-8")
    # A literal closing script tag would terminate application/json content in the HTML parser.
    embedded = embedded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    rows = "".join(f"<tr><th>{escape(key)}</th><td>{'不可用' if value is None else escape(str(value))}</td></tr>"
        for key, value in report.metrics.items())
    definitions = "".join(f"<tr><th>{escape(key)}</th><td>{escape(str(value['description']))}</td></tr>"
        for key, value in report.metric_definitions.items() if "description" in value)
    warnings = "".join(f"<li>{escape(warning)}</li>" for warning in report.warnings)
    provenance = "".join(f"<tr><th>{label}</th><td>{escape(str(report.metadata[key]))}</td></tr>"
        for key, label in (("source", "数据来源"), ("price_mode", "价格口径"), ("dataset_version_id", "数据版本"), ("strategy_revision_id", "策略修订")) if key in report.metadata)
    html = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>回测报告</title>"
        "<style>body{font:16px system-ui,sans-serif;max-width:960px;margin:2rem auto;color:#18324a}"
        "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:.5rem;text-align:left}"
        "svg{max-width:100%;height:auto}</style></head><body><h1>回测报告</h1>"
        "<p>策略权益（蓝）与基准权益（橙）；基准按首个绩效日归一化。</p>"
        f"{'<p><strong>演示数据：合成绩效，不代表真实市场表现</strong></p>' if report.metadata['is_demo'] else ''}<h2>来源</h2><table>{provenance}</table>{_svg(report.curves)}<h2>指标</h2><table>{rows}</table>"
        f"<h2>指标定义</h2><table>{definitions}</table><h2>说明</h2><ul>{warnings}</ul>"
        f"<script type=\"application/json\" id=\"report-data\">{embedded}</script></body></html>\n")
    return html.encode("utf-8")


def write_backtest_outputs(target_dir: Path, result: BacktestResult, report: ReportBundle) -> dict[str, str]:
    target_dir = Path(target_dir)
    frames = {name: getattr(result, name)[TABLE_COLUMNS[name]] if name in TABLE_COLUMNS else getattr(report, name)[REPORT_TABLE_COLUMNS[name]]
        for name in OUTPUT_TABLES}
    payloads = {f"{name}.csv": _csv_bytes(frame) for name, frame in frames.items()}
    payloads["metrics.json"] = _json_bytes({"schema_version": SCHEMA_VERSION, "metrics": report.metrics,
        "metric_definitions": report.metric_definitions, "warnings": report.warnings,
        "completed_through": result.completed_through, "artifacts": report.artifact_manifest})
    payloads["metadata.json"] = _json_bytes(report.metadata)
    payloads["report.html"] = _html(report, result)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payloads.items())}
    payloads["manifest.json"] = _json_bytes({"schema_version": SCHEMA_VERSION, "files": hashes})
    hashes["manifest.json"] = hashlib.sha256(payloads["manifest.json"]).hexdigest()
    target_dir.mkdir(parents=True, exist_ok=False)
    for name, data in payloads.items():
        (target_dir / name).write_bytes(data)
    return hashes
