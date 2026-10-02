"""Render the audited fifth-round volatility-neighborhood research."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
from datetime import datetime
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "outputs/strategy-research-20261002-round5"
DOCS = PROJECT / "docs/research/2026-10-02-round5"
BASELINE = "baseline"
PERIODS = ("development", "validation", "late", "full")
RESTARTS = ("restart_20240701", "restart_20250701", "restart_20260401")
NAMES = (BASELINE, *(f"w{window}_v{vol}" for window in (30, 60, 90)
                     for vol in (11, 12, 13) if (window, vol) != (60, 12)),
         "w20_v12", "w120_v12", "entry50", "entry65", "exit35", "exit50")
ARTIFACTS = {"curves.csv", "equity.csv", "fills.csv", "average_weights.csv"}
METRICS = ("total_return", "annual_return", "max_drawdown", "sharpe",
           "average_invested_ratio", "execution_cost")
BASELINE_URL = "http://127.0.0.1:8767/runs/8e32c5e8-43f1-4d0b-9f9d-0802f7e0672e"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def esc(value):
    return html.escape(str(value), quote=True)


def fmt(value, percent=False):
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:+.2%}" if percent and value < 0 else (f"{value:.2%}" if percent else f"{value:,.2f}")


def delta(value, baseline, percent=False):
    if (value is None or baseline is None or not isinstance(value, (int, float))
            or not isinstance(baseline, (int, float))):
        return "—"
    difference = value - baseline
    if not math.isfinite(difference):
        return "—"
    return f"{difference * 100:+.2f} pp" if percent else f"{difference:+.2f}"


def table(headers, rows):
    head = "".join(f"<th scope='col'>{esc(value)}</th>" for value in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(value)}</td>" for value in row) + "</tr>" for row in rows)
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def validate(root: Path, docs: Path):
    protocol_path = root / "protocol.json"
    protocol_hash = digest(protocol_path)
    require(digest(docs / "protocol.json") == protocol_hash, "Documented protocol differs from frozen protocol")
    protocol = load(protocol_path)
    require(set(protocol["candidates"]) == set(NAMES) and len(protocol["candidates"]) == 15,
            "Expected fifteen frozen configurations")
    require(set(PERIODS) <= set(protocol["periods"]), "Primary periods missing from protocol")
    for name in NAMES:
        require(protocol["candidates"][name]["kind"] == "breadth_risk", f"Unexpected strategy: {name}")

    selection_path = root / "selection.json"
    selection = load(selection_path)
    require(load(docs / "selection.json") == selection, "Documented selection differs from frozen selection")
    require(selection.get("protocol_sha256") == protocol_hash, "Selection protocol hash differs")
    selected = selection.get("selected")
    require(isinstance(selected, list) and len(selected) <= 3 and len(set(selected)) == len(selected)
            and all(name in NAMES and name != BASELINE for name in selected), "Invalid early selection")
    groups = selection.get("groups")
    require(isinstance(groups, list), "Selection path groups missing")
    represented = set()
    for group in groups:
        require(isinstance(group, dict) and {"representative", "aliases", "curve_hashes",
                "minimum_improvement", "minimum_sharpe", "validation_sharpe"} <= set(group),
                "Incomplete selection path group")
        aliases = group["aliases"]
        require(isinstance(aliases, list) and aliases and group["representative"] in aliases,
                "Invalid path aliases")
        require(all(name in NAMES and name != BASELINE and name not in represented for name in aliases),
                "Repeated or unknown path alias")
        represented.update(aliases)
    require(set(selected) <= {g["representative"] for g in groups}, "Selected path absent from groups")
    require(isinstance(selection.get("baseline_aliases"), list), "Baseline aliases missing")

    results_path = docs / "results.json"
    verification = load(docs / "verification.json")
    require(verification.get("verified") is True and verification.get("protocol_sha256") == protocol_hash
            and verification.get("selection_sha256") == digest(selection_path)
            and verification.get("results_sha256") == digest(results_path), "Audited result hashes differ")
    expected = {(name, period, 1) for name in NAMES for period in PERIODS}
    stressed = [BASELINE] + selected
    expected |= {(name, period, 2) for name in stressed for period in ("full", "late")}
    expected |= {(name, period, 1) for name in stressed for period in RESTARTS}
    require(verification.get("runs") == len(expected), "Audit run count differs")
    results = load(results_path)
    require(isinstance(results, list) and len(results) == len(expected), "Incomplete result matrix")
    lookup = {}
    for row in results:
        key = row["candidate"], row["period"], row["cost_multiplier"]
        require(key in expected and key not in lookup, f"Unexpected or repeated result: {key}")
        require(row.get("protocol_sha256") == protocol_hash and row.get("parameters") == protocol["candidates"][key[0]],
                f"Result identity differs: {key}")
        require(all(metric in row.get("metrics", {}) for metric in METRICS), f"Incomplete metrics: {key}")
        folder = root / "runs" / f"{key[1]}__{key[0]}__cost{key[2]}"
        require(load(folder / "summary.json") == row, f"Audited summary differs: {key}")
        require(set(row.get("artifacts", {})) == ARTIFACTS, f"Incomplete artifact manifest: {key}")
        for filename, hash_value in row["artifacts"].items():
            path = folder / filename
            require(path.is_file() and digest(path) == hash_value, f"Artifact differs: {path}")
        lookup[key] = row
    require(set(lookup) == expected, "Result identities incomplete")
    baseline_hashes = [lookup[BASELINE, period, 1]["artifacts"]["curves.csv"]
                       for period in PERIODS[:2]]
    baseline_aliases = selection["baseline_aliases"]
    require(baseline_aliases and baseline_aliases[0] == BASELINE
            and len(baseline_aliases) == len(set(baseline_aliases))
            and all(name in NAMES for name in baseline_aliases),
            "Unknown baseline-path alias")
    for name in baseline_aliases:
        require([lookup[name, period, 1]["artifacts"]["curves.csv"] for period in PERIODS[:2]] == baseline_hashes,
                f"Baseline-path alias differs: {name}")
    covered = set(baseline_aliases) - {BASELINE}
    for group in groups:
        pair = group["curve_hashes"]
        require(isinstance(pair, list) and len(pair) == 2 and all(isinstance(value, str) for value in pair),
                "Invalid early curve hash pair")
        for name in group["aliases"]:
            require([lookup[name, period, 1]["artifacts"]["curves.csv"] for period in PERIODS[:2]] == pair,
                    f"Selection-path alias differs: {name}")
            covered.add(name)
    require(covered == set(NAMES) - {BASELINE}, "Early path grouping does not cover every variant")
    require(selected == [group["representative"] for group in groups[:3]],
            "Selected paths differ from early group order")
    return protocol, selection, lookup


def validate_followup(root: Path, docs: Path, protocol, selection, lookup):
    plan_path = docs / "window120_followup_plan.json"
    if not plan_path.exists():
        return None
    plan = load(plan_path)
    protocol_hash = digest(root / "protocol.json")
    selection_hash = digest(root / "selection.json")
    require(plan.get("protocol_sha256") == protocol_hash
            and plan.get("selection_sha256") == selection_hash
            and plan.get("main_results_sha256") == digest(docs / "results.json")
            and plan.get("candidate") == "w120_v12", "Follow-up plan differs from frozen evidence")
    expected = {("w120_v12", "full", 2), ("w120_v12", "late", 2)}
    expected |= {("w120_v12", period, 1) for period in RESTARTS}
    require({("w120_v12", job["period"], job["cost_multiplier"]) for job in plan["jobs"]} == expected
            and len(plan["jobs"]) == len(expected), "Unexpected follow-up plan jobs")
    result = load(docs / "window120_followup.json")
    require(result.get("verified") is True and result.get("plan_sha256") == digest(plan_path)
            and result.get("protocol_sha256") == protocol_hash
            and result.get("selection_sha256") == selection_hash, "Follow-up verification differs")
    rows = result.get("results")
    require(isinstance(rows, list) and len(rows) == len(expected), "Incomplete follow-up results")
    extra = {}
    for row in rows:
        key = row["candidate"], row["period"], row["cost_multiplier"]
        require(key in expected and key not in extra and key not in lookup, f"Unexpected follow-up run: {key}")
        require(row.get("protocol_sha256") == protocol_hash
                and row.get("parameters") == protocol["candidates"][key[0]]
                and all(metric in row.get("metrics", {}) for metric in METRICS),
                f"Follow-up identity or metrics differ: {key}")
        folder = root / "runs" / f"{key[1]}__{key[0]}__cost{key[2]}"
        require(load(folder / "summary.json") == row, f"Follow-up summary differs: {key}")
        require(set(row.get("artifacts", {})) == ARTIFACTS, f"Follow-up artifacts incomplete: {key}")
        for filename, hash_value in row["artifacts"].items():
            path = folder / filename
            require(path.is_file() and digest(path) == hash_value, f"Follow-up artifact differs: {path}")
        extra[key] = row
    require(set(extra) == expected, "Follow-up result identities incomplete")

    diagnostic_path = docs / "target13-diagnostic.json"
    diagnostic = load(diagnostic_path)
    replay = diagnostic.get("replay_verification", {})
    trigger = diagnostic.get("trigger", {})
    swing = diagnostic.get("largest_daily_gap_change", {})
    require(diagnostic.get("protocol_sha256") == protocol_hash
            and replay.get("baseline_metric_dictionary_equal") is True
            and replay.get("w60_v13_metric_dictionary_equal") is True
            and trigger.get("risk_code") == "EXPOSURE_REDUCTION"
            and trigger.get("baseline_current_exposure", 1) < .8
            and trigger.get("w60_v13_current_exposure", 0) > .8
            and swing.get("date") == "2025-07-15"
            and isinstance(swing.get("change"), (int, float))
            and math.isfinite(swing["change"]),
            "Target-13 diagnostic is not tied to frozen replay metrics")
    for name in (BASELINE, "w60_v13"):
        audited = lookup[name, "validation", 1]["metrics"]
        recorded = diagnostic.get("metrics", {}).get(name, {})
        require({"annual_return", "max_drawdown", "sharpe", "final_equity"} <= set(recorded)
                and all(recorded[metric] == audited[metric] for metric in recorded),
                f"Target-13 diagnostic metrics differ: {name}")
    return extra


def curve(root: Path, name: str, lookup):
    path = root / "runs" / f"full__{name}__cost1" / "curves.csv"
    require(digest(path) == lookup[name, "full", 1]["artifacts"]["curves.csv"], f"Curve hash differs: {name}")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    require(rows and {"date", "equity", "drawdown"} <= set(rows[0]), f"Invalid curve: {name}")
    dates = [datetime.strptime(row["date"], "%Y-%m-%d") for row in rows]
    equity = [float(row["equity"]) for row in rows]
    drawdown = [float(row["drawdown"]) for row in rows]
    require(all(math.isfinite(value) for value in equity + drawdown) and all(value > 0 for value in equity),
            f"Invalid curve values: {name}")
    return dates, equity, drawdown


def chart(root: Path, docs: Path, selected, lookup):
    names = [BASELINE] + selected
    series = [(name, *curve(root, name, lookup)) for name in names]
    require(all(dates == series[0][1] for _, dates, _, _ in series), "Full curves have different dates")
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(13, 8), sharex=True, constrained_layout=True)
    colors = ("#344b61", "#156f72", "#a46b30", "#654e8c")
    for index, (name, dates, equity, drawdown) in enumerate(series):
        style = "--" if name == BASELINE else "-"
        top.plot(dates, [value / 1_000_000 for value in equity], label=name,
                 color=colors[index], linewidth=2, linestyle=style)
        bottom.plot(dates, drawdown, color=colors[index], linewidth=2, linestyle=style)
    top.set(title="Full period · net equity", ylabel="Equity (CNY millions)")
    bottom.set(title="Full period · drawdown", ylabel="Drawdown")
    bottom.yaxis.set_major_formatter(PercentFormatter(1))
    locator = mdates.AutoDateLocator()
    bottom.xaxis.set_major_locator(locator)
    bottom.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    for axis in (top, bottom):
        axis.grid(color="#d8dfe2", alpha=.7)
    top.legend(ncol=4, frameon=False)
    path = docs / "comparison.png"
    fig.savefig(path, dpi=160, facecolor="white")
    plt.close(fig)
    return path


def workbench_links(docs: Path, selected):
    path = docs / "workbench_verified.json"
    if not path.exists():
        return ""
    state = load(path)
    require(isinstance(state, dict)
            and state.get("protocol_sha256") == digest(docs / "protocol.json")
            and state.get("selection_sha256") == digest(docs / "selection.json"),
            "Workbench verification differs from frozen evidence")
    records = state.get("runs")
    require(isinstance(records, list), "Invalid workbench verification")
    expected = {(name, period) for name in selected for period in ("full", "late")}
    seen = set()
    links = []
    for row in records:
        key = row.get("candidate"), row.get("period")
        require(key in expected and key not in seen and row.get("cost_multiplier") == 1
                and row.get("verified") is True and row.get("all_metrics_equal") is True,
                f"Invalid workbench run verification: {key}")
        url = row.get("url")
        require(isinstance(url, str) and url.startswith(("http://", "https://")),
                f"Invalid workbench run URL: {key}")
        seen.add(key)
        links.append(f"<li>{esc(row['candidate'])} · {esc(row['period'])}: <a href='{esc(url)}'>{esc(row.get('run_id', '工作台记录'))}</a></li>")
    require(seen == expected, "Workbench verification is incomplete")
    return "<section class='panel'><h2>工作台核验记录</h2><ul>" + "".join(links) + "</ul></section>" if links else ""


def followup_workbench_links(root: Path, docs: Path, extra):
    path = docs / "workbench_followup_verified.json"
    if not path.exists():
        return ""
    state = load(path)
    require(isinstance(state, dict) and state.get("plan_sha256") == digest(docs / "window120_followup_plan.json")
            and state.get("protocol_sha256") == digest(root / "protocol.json")
            and state.get("selection_sha256") == digest(root / "selection.json")
            and state.get("main_results_sha256") == digest(docs / "results.json")
            and state.get("candidate") == "w120_v12", "Follow-up workbench verification differs")
    records = state.get("runs")
    require(isinstance(records, list) and len(records) == 2, "Incomplete follow-up workbench records")
    seen = set()
    links = []
    for row in records:
        key = row.get("candidate"), row.get("period"), row.get("cost_multiplier")
        require(key in {("w120_v12", "full", 1), ("w120_v12", "late", 1)} and key not in seen
                and row.get("verified") is True and row.get("all_metrics_equal") is True,
                f"Invalid follow-up workbench run: {key}")
        summary = root / "runs" / f"{key[1]}__{key[0]}__cost1" / "summary.json"
        require(row.get("summary_sha256") == digest(summary), f"Follow-up workbench source differs: {key}")
        url = row.get("url")
        require(isinstance(url, str) and url.startswith(("http://", "https://")),
                f"Invalid follow-up workbench URL: {key}")
        seen.add(key)
        links.append(f"<li>{esc(key[1])}: <a href='{esc(url)}'>{esc(row.get('run_id', '工作台记录'))}</a></li>")
    require(seen == {("w120_v12", "full", 1), ("w120_v12", "late", 1)},
            "Follow-up workbench identities incomplete")
    return "<p>工作台核验：</p><ul>" + "".join(links) + "</ul>"


def followup_section(root: Path, docs: Path, lookup, extra):
    if extra is None:
        return ""
    baseline = lookup[BASELINE, "full", 1]["metrics"]
    window = lookup["w120_v12", "full", 1]["metrics"]
    validation_base = lookup[BASELINE, "validation", 1]["metrics"]
    validation_window = lookup["w120_v12", "validation", 1]["metrics"]
    validation_score_delta = ((validation_window["annual_return"] - .5 * validation_window["max_drawdown"])
                              - (validation_base["annual_return"] - .5 * validation_base["max_drawdown"]))
    full_rows = []
    for name, metrics in ((BASELINE, baseline), ("w120_v12", window)):
        full_rows.append([name, fmt(metrics["annual_return"], True), fmt(metrics["max_drawdown"], True),
                          fmt(metrics["sharpe"]), fmt(metrics["average_invested_ratio"], True),
                          fmt(metrics["execution_cost"])])
    cost_rows = []
    for period in ("full", "late"):
        for name in (BASELINE, "w120_v12"):
            normal = lookup[name, period, 1]["metrics"]
            high = (lookup if name == BASELINE else extra)[name, period, 2]["metrics"]
            metric = "annual_return" if period == "full" else "total_return"
            cost_rows.append([period, name, fmt(normal[metric], True), fmt(high[metric], True),
                              delta(high[metric], normal[metric], True), fmt(high["max_drawdown"], True),
                              fmt(high["sharpe"]), fmt(high["execution_cost"])])
    restart_rows = []
    for period in RESTARTS:
        base = lookup[BASELINE, period, 1]["metrics"]
        for name in (BASELINE, "w120_v12"):
            metrics = (lookup if name == BASELINE else extra)[name, period, 1]["metrics"]
            restart_rows.append([period.removeprefix("restart_"), name, fmt(metrics["total_return"], True),
                                 fmt(metrics["annual_return"], True), fmt(metrics["max_drawdown"], True),
                                 fmt(metrics["sharpe"]), fmt(metrics["average_invested_ratio"], True),
                                 delta(metrics["total_return"], base["total_return"], True)])
    validation_note = ("验证段得分低于基准" if validation_score_delta < 0 else "验证段得分未低于基准")
    return ("<section class='panel'><h2>120 日窗口追加诊断（事后）</h2>"
            "<p>在查看完整 80 次主研究结果后，追加 w120_v12 的双倍成本与三个现金重启检查；"
            "这组不在开发/验证期冻结的预选名单中，也不改变原有净值图或 80 次审计。"
            f"{validation_note}（相差 {delta(validation_window['annual_return'] - .5 * validation_window['max_drawdown'], validation_base['annual_return'] - .5 * validation_base['max_drawdown'], True)}）。"
            "下表呈现收益、回撤与 Sharpe 的取舍，不能据此认定稳健优胜。</p>"
            + table(("配置", "全期 CAGR", "最大回撤", "Sharpe", "平均仓位", "交易成本/元"), full_rows)
            + "<h3>双倍成本</h3><p>Full 比较 CAGR，late 比较区间收益；差值单位为百分点。</p>"
            + table(("区间", "配置", "正常收益", "双倍成本收益", "收益差", "双倍成本回撤", "双倍成本 Sharpe", "交易成本/元"), cost_rows)
            + "<h3>现金重启</h3><p>每次单独从现金和未激活宽度状态运行；区间收益差相对同起点基准，单位为百分点。</p>"
            + table(("重启日期", "配置", "区间收益", "CAGR", "最大回撤", "Sharpe", "平均仓位", "Δ 区间收益"), restart_rows)
            + "<p><a href='window120_followup_plan.json'>冻结追加方案</a> · "
              "<a href='window120_followup.json'>五次追加核验</a>。</p>"
            + followup_workbench_links(root, docs, extra) + "</section>")


def render(root: Path, docs: Path, protocol, selection, lookup, extra):
    selected = selection["selected"]
    parameter_rows = []
    for name in NAMES:
        params = protocol["candidates"][name]["params"]
        parameter_rows.append([name, fmt(params["entry_breadth"], True),
                               fmt(params["exit_breadth"], True), fmt(params["target_vol"], True),
                               params["vol_window"], fmt(params["gross_target"], True)])
    quality = {}
    for name in NAMES:
        full = lookup[name, "full", 1]["metrics"]
        sharpe = full["sharpe"]
        quality[name] = (full["annual_return"] >= .08 and full["max_drawdown"] <= .15
                         and isinstance(sharpe, (int, float)) and math.isfinite(sharpe) and sharpe >= .7
                         and all(lookup[name, period, 1]["metrics"]["total_return"] > 0
                                 for period in PERIODS[:3]))
    gate_pass = [name for name in NAMES if quality[name]]
    labels = {"development": "开发段", "validation": "验证段", "late": "后段", "full": "全期"}
    period_tables = []
    for period in PERIODS:
        base = lookup[BASELINE, period, 1]["metrics"]
        rows = []
        for name in NAMES:
            m = lookup[name, period, 1]["metrics"]
            rows.append([name, "预选" if name in selected else "基准" if name == BASELINE else "—",
                         fmt(m["total_return"], True), fmt(m["annual_return"], True),
                         fmt(m["max_drawdown"], True), fmt(m["sharpe"]),
                         fmt(m["average_invested_ratio"], True), fmt(m["execution_cost"]),
                         delta(m["annual_return"], base["annual_return"], True),
                         delta(m["max_drawdown"], base["max_drawdown"], True),
                         delta(m["sharpe"], base["sharpe"]), "通过" if quality[name] else "未通过"])
        period_tables.append(f"<h3>{labels[period]} · {esc('—'.join(protocol['periods'][period]))}</h3>" +
                             table(("配置", "身份", "区间收益", "CAGR", "最大回撤", "Sharpe", "平均仓位",
                                    "交易成本/元", "Δ CAGR", "Δ 回撤", "Δ Sharpe", "旧质量门槛*"), rows))

    selection_rows = []
    for rank, group in enumerate(selection["groups"], 1):
        aliases = ", ".join(group["aliases"])
        selection_rows.append([rank, group["representative"], aliases,
                               ", ".join(group["curve_hashes"]) if isinstance(group["curve_hashes"], list)
                               else str(group["curve_hashes"]),
                               fmt(group["minimum_improvement"], True),
                               fmt(group["minimum_sharpe"]), fmt(group["validation_sharpe"]),
                               "预选" if group["representative"] in selected else "—"])
    cost_rows = []
    for name in [BASELINE] + selected:
        for period in ("full", "late"):
            normal = lookup[name, period, 1]["metrics"]
            stress = lookup[name, period, 2]["metrics"]
            key = "annual_return" if period == "full" else "total_return"
            cost_rows.append([name, labels[period], fmt(normal[key], True), fmt(stress[key], True),
                              delta(stress[key], normal[key], True), fmt(stress["max_drawdown"], True),
                              fmt(stress["sharpe"]), fmt(stress["average_invested_ratio"], True),
                              fmt(stress["execution_cost"])])
    restart_rows = []
    for period in RESTARTS:
        base = lookup[BASELINE, period, 1]["metrics"]
        for name in [BASELINE] + selected:
            m = lookup[name, period, 1]["metrics"]
            restart_rows.append([period.removeprefix("restart_"), name, fmt(m["total_return"], True),
                                 fmt(m["annual_return"], True), fmt(m["max_drawdown"], True),
                                 fmt(m["sharpe"]), fmt(m["average_invested_ratio"], True),
                                 fmt(m["execution_cost"]), delta(m["total_return"], base["total_return"], True)])
    evidence = " · ".join(f"<a href='{name}'>{label}</a>" for name, label in (
        ("protocol.json", "冻结协议"), ("selection.json", "早期预选与路径分组"),
        ("results.json", "全部审计结果"), ("verification.json", "审计验证")))
    selected_label = "、".join(selected) if selected else "无独立候选路径"
    diagnostic = load(docs / "target13-diagnostic.json") if extra is not None else None
    diagnostic_note = ""
    if diagnostic is not None:
        trigger = diagnostic["trigger"]
        swing = diagnostic["largest_daily_gap_change"]
        diagnostic_note = (
            "<section class='panel'><h2>13% 目标波动率的路径诊断</h2>"
            f"<p>冻结验证段的两次内存重跑与原指标字典完全一致。{esc(trigger['signal_date'])} 评估时，"
            f"w60_v13 已有持仓占净值 {fmt(trigger['w60_v13_current_exposure'], True)}，触发 {fmt(.8, True)} 风险上限修复；"
            f"其新目标约 {fmt(trigger['w60_v13_proposed_target_total_approx'], True)} 未执行，"
            f"{esc(trigger['execution_date'])} 的修复改变了再平衡路径。"
            f"{esc(swing['date'])} 的 w60_v13 相对基准净值差"
            f"{'减少' if swing['change'] < 0 else '增加'} {fmt(abs(swing['change']))} 元。"
            "后续路径与费用也影响最终差距，这一事件并非完整因果归因。"
            "<a href='target13-diagnostic.json'>逐项诊断证据</a>。</p></section>")
    blocks = ["<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
              "<meta name='viewport' content='width=device-width,initial-scale=1'>",
              "<title>第五轮 · 波动率邻域研究</title>",
              "<style>body{margin:0;background:#f4f6f5;color:#243239;font:15px/1.65 system-ui,'Microsoft YaHei',sans-serif}"
              "main{max-width:1500px;margin:auto;padding:32px 24px 70px}h1{font-size:30px;margin:0 0 6px}h2{font-size:21px;margin-top:38px}h3{margin-top:30px}"
              ".lead{color:#52646a;margin:0 0 20px}.panel{background:white;border:1px solid #dce3e2;border-radius:10px;padding:20px 24px;margin:22px 0}"
              ".note{border-left:4px solid #ad8446;background:#fcf8ef;padding:12px 16px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}"
              "th,td{border-bottom:1px solid #e2e8e6;padding:8px 10px;text-align:right}th{background:#edf2f0;color:#42565b;position:sticky;top:0}"
              "th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}tr:hover td{background:#f5f9f7}a{color:#176f74}img{max-width:100%;height:auto}small{color:#607277}</style></head><body><main>",
              "<h1>第五轮 · 波动率邻域研究</h1>",
              "<p class='lead'>固定 15 组参数，在四个独立启动的区间内比较；开发和验证结果先确定路径预选，再查看后段及全期。</p>",
              f"<p>{evidence} · <a href='{esc(BASELINE_URL)}'>已选原始工作台基准</a></p>",
              "<div class='note'>本报告属于已查看历史价格上的回溯研究。开发、验证、后段及全期均已被历史探索使用，均非独立留出样本；"
              "各区间从现金及未激活宽度状态独立启动，区间收益不能拼接为全期收益。所有收益含研究模型中的交易费用。</div>",
              "<section class='panel'><h2>预选结果与解释</h2>",
              f"<p>开发与验证期按逐期相对基准的最小得分改善排序，去除相同净值路径后预选：{esc(selected_label)}。"
              "得分是 CAGR − 0.5 × 最大回撤，最小改善为两个早期区间各自与基准相减后的较小值。"
              "负改善仍照实记录。下表同一路径的别名不算独立证据。</p>",
              f"<p>原质量门槛在全期通过的配置：{esc('、'.join(gate_pass) if gate_pass else '无')}。"
              "质量门槛是描述性比较，不是统计显著性检验。后段或全期才看到的优胜配置属于事后探索，不改变预选名单。</p>",
              table(("排序", "路径代表", "全部别名", "开发/验证曲线 SHA256", "最小改善", "最小早期 Sharpe", "验证 Sharpe", "预选"), selection_rows),
              f"<p>与基准完全相同的早期路径别名：{esc('、'.join(selection['baseline_aliases']) if selection['baseline_aliases'] else '无')}。</p></section>",
              "<section class='panel'><h2>全期净值和回撤</h2><p>虚线为基准，实线为早期预选；均为正常成本。</p>"
              "<img src='comparison.png' alt='基准与早期预选配置的全期净值和回撤'></section>",
              "<section class='panel'><h2>冻结参数</h2><p>五个字段均来自冻结协议；每组仅改变配置名所示的一项或两项。</p>",
              table(("配置", "进场宽度", "退出宽度", "目标波动率", "波动率窗口/日", "目标总仓位"), parameter_rows), "</section>",
              "<section class='panel'><h2>全部 15 组配置 · 四个区间</h2><p>每个表均列出 15 组；Δ 相对该区间的 baseline，"
              "Δ CAGR 与 Δ 回撤单位为百分点，正回撤差值表示回撤变大；Δ Sharpe 为绝对差值。*旧质量门槛按全期 CAGR ≥ 8%、最大回撤 ≤ 15%、Sharpe ≥ 0.7 和三个子区间收益均正判定，"
              "故四表同一配置的门槛状态相同。</p>", *period_tables, "</section>",
              "<section class='panel'><h2>双倍交易成本</h2><p>仅基准与早期预选，在全期和后段分别把佣金率、最低佣金和滑点乘二。"
              "Full 收益列为 CAGR，后段为区间收益；收益差以百分点表示，与同区间正常成本比较。</p>",
              table(("配置", "区间", "正常收益", "双倍成本收益", "收益差", "双倍成本回撤", "双倍成本 Sharpe", "平均仓位", "交易成本/元"), cost_rows), "</section>",
              "<section class='panel'><h2>现金重启日期敏感性</h2><p>每次从标示日期重新以现金和未激活宽度状态运行至 2026-09-30；"
              "这不是全期收益拼接，也不是未查看留出样本。Δ 区间收益以百分点表示，相对同起点 baseline。</p>",
              table(("重启日期", "配置", "区间收益", "CAGR", "最大回撤", "Sharpe", "平均仓位", "交易成本/元", "Δ 区间收益"), restart_rows), "</section>",
              followup_section(root, docs, lookup, extra), diagnostic_note,
              "<section class='panel'><h2>研究边界</h2><p>ETF 历史目录与存续样本选择可能带来幸存者偏差；流动性筛选也使用当前可得资料。"
              "供应商价格、复权和公司行为口径尚未得到全历史认证，先前六月五只证券的核对仅是局部抽查。"
              "现金利息设为零，投资范围仅为 ETF 股票类资产；每周信号、次日开盘成交、整手及费用假设可能偏离实际。"
              "参数搜索和重复路径不会产生独立的样本外证据，也不构成实盘建议。</p></section>",
              workbench_links(docs, selected),
              "<small>数值直接从审计结果生成，显示值经四舍五入；交易成本为佣金与滑点合计。</small></main></body></html>"]
    path = docs / "dashboard.html"
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--docs", type=Path, default=DOCS)
    args = parser.parse_args()
    root, docs = args.root.resolve(), args.docs.resolve()
    protocol, selection, lookup = validate(root, docs)
    extra = validate_followup(root, docs, protocol, selection, lookup)
    png = chart(root, docs, selection["selected"], lookup)
    page = render(root, docs, protocol, selection, lookup, extra)
    print(f"Wrote {png}\nWrote {page}")


if __name__ == "__main__":
    main()
