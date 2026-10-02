"""Build a static comparison report from the completed round-three audit."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "outputs/strategy-research-20261002-round3"
DOCS = PROJECT / "docs/research/2026-10-02-round3"
FINALIST_BASELINES = ["group_equal_80", "group_equal_40", "guarded_momentum_200_v0.1"]
FAMILY_NAMES = {
    "momentum": "单周期动量", "dual_momentum": "双周期动量", "ma_trend": "均线趋势",
    "breakout": "突破延续", "low_vol": "低波趋势", "regression": "回归趋势",
    "pullback": "趋势内回调", "sector_rotation": "分组轮动", "breadth_timing": "市场宽度择时",
    "correlation": "相关性约束动量",
}
BASELINE_NAMES = {
    "trend_20_20_k3": "20日轮动", "guarded_momentum_200_v0.1": "200日防守动量",
    "diversified_200_inverse_vol": "200\u65e5\u9006\u6ce2\u52a8\u5206\u6563", "buy_hold": "\u4e70\u5165\u6301\u6709\uff08\u4e0a\u965080%\uff09",
    "cash_buy_hold_40": "40%买入持有", "group_equal_80": "分组等权80%",
    "group_equal_40": "分组等权40%",
}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def fmt(value, percent=False):
    if value is None:
        return "—"
    return f"{value:.2%}" if percent else f"{value:.3f}"


def fmt_cost(value):
    return "\u2014" if value is None else f"{value:,.2f} \u5143"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(results, protocol):
    candidates = set(protocol["candidates"])
    keys = [(r["candidate"], r["period"], r["cost_multiplier"]) for r in results]
    expected = {(name, period, 1) for name in candidates for period in ("development", "validation", "late", "full")}
    # Final stress scope is fixed by protocol, while the names come from the saved shortlist.
    return keys, expected


def read_curve(root, name, result):
    path = root / "runs" / f"full__{name}__cost1" / "curves.csv"
    expected_hash = result["artifacts"].get("curves.csv")
    if not expected_hash or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        raise ValueError(f"Curve is absent from or differs from audited artifacts: {path}")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or "date" not in rows[0] or "equity" not in rows[0]:
        raise ValueError(f"Invalid curve file: {path}")
    return [r["date"] for r in rows], [float(r["equity"]) for r in rows]


def chart(root, docs, protocol, results, selected):
    lookup = {(r["candidate"], r["period"], r["cost_multiplier"]): r for r in results}
    fig, (equity_ax, scatter_ax) = plt.subplots(1, 2, figsize=(16, 7), constrained_layout=True)
    chart_names = list(selected) + FINALIST_BASELINES
    series = [(name, *read_curve(root, name, lookup[name, "full", 1])) for name in chart_names]
    dates0 = series[0][1]
    if any(dates != dates0 for _, dates, _ in series):
        raise ValueError("Full-period chart curves do not share the same dates")
    dates = [datetime.strptime(value, "%Y-%m-%d") for value in dates0]
    palette = plt.get_cmap("tab10").colors
    for idx, (name, _, line) in enumerate(series):
        baseline = name not in selected
        equity_ax.plot(dates, line, label=("* " if name in selected else "") + name,
                       color=palette[idx], linewidth=2.6 if name in selected else 1.7,
                       linestyle="--" if baseline else "-")
    equity_ax.set_title("Full-period net value (same start date)")
    equity_ax.set_ylabel("Equity (CNY, millions)")
    equity_ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda value, _: f"{value / 1e6:.1f}"))
    locator = mdates.AutoDateLocator()
    equity_ax.xaxis.set_major_locator(locator)
    equity_ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    equity_ax.grid(alpha=.25)
    equity_ax.legend(fontsize=8, loc="best")

    full = {r["candidate"]: r["metrics"] for r in results if r["period"] == "full" and r["cost_multiplier"] == 1}
    scatter_names = [name for names in protocol["families"].values() for name in names]
    points = [(full[n]["annual_return"], full[n]["max_drawdown"], n) for n in scatter_names]
    points += [(full[n]["annual_return"], full[n]["max_drawdown"], n) for n in protocol["baselines"]]
    baseline_points = [point for point in points if point[2] in protocol["baselines"]]
    candidate_points = [point for point in points if point[2] not in protocol["baselines"]]
    scatter_ax.scatter([x for x, _, _ in candidate_points], [y for _, y, _ in candidate_points],
                       marker="o", color="#1769aa", alpha=.78, label="20 family variants")
    scatter_ax.scatter([x for x, _, _ in baseline_points], [y for _, y, _ in baseline_points],
                       marker="D", color="#778899", alpha=.88, label="7 baselines")
    for x, y, name in points:
        if name in selected:
            scatter_ax.scatter([x], [y], s=150, facecolors="none", edgecolors="#d64045", linewidths=2)
            scatter_ax.annotate(name, (x, y), xytext=(5, 6), textcoords="offset points", fontsize=8)
        elif name in FINALIST_BASELINES:
            scatter_ax.annotate(name, (x, y), xytext=(5, 6), textcoords="offset points", fontsize=8)
    scatter_ax.set_title("Full-period return and drawdown")
    scatter_ax.set_xlabel("Annual return")
    scatter_ax.set_ylabel("Maximum drawdown")
    scatter_ax.xaxis.set_major_formatter(PercentFormatter(1))
    scatter_ax.yaxis.set_major_formatter(PercentFormatter(1))
    scatter_ax.grid(alpha=.25)
    scatter_ax.legend(fontsize=9)
    fig.suptitle("Round 3: preselected finalists and complete comparison", fontsize=15)
    out = docs / "comparison.png"
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out


def cell(value):
    return html.escape(str(value))


def report(docs, protocol, results, training, selection):
    lookup = {(r["candidate"], r["period"], r["cost_multiplier"]): r for r in results}
    selected = selection["selected"]
    nav = '<p><a href="protocol.json">冻结协议</a> · <a href="results.json">完整审计结果</a> · <a href="plan.md">研究方案</a></p>'
    chunks = ["<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>第三轮策略比较</title>",
              "<style>body{font:15px/1.55 system-ui,'Microsoft YaHei',sans-serif;color:#202b38;max-width:1480px;margin:28px auto;padding:0 22px}h1,h2{line-height:1.25}img{max-width:100%;height:auto}table{border-collapse:collapse;width:100%;margin:14px 0 30px;font-size:13px}th,td{border:1px solid #d6dde5;padding:6px 8px;text-align:right}th{background:#eef3f7;position:sticky;top:0}th:first-child,td:first-child{text-align:left}td.param{text-align:left;max-width:400px;word-break:break-word} .note{background:#fff8df;padding:12px 16px;border-left:4px solid #d9aa3a}a{color:#1261a0}</style><body>",
              "<h1>第三轮策略比较</h1>", nav,
              '<p class="note">所有时段都曾用于本次探索，不能视为全新独立留出样本。这里展示冻结协议、逐阶段选出的名单以及完整的候选对比；不按 late 结果重新排序或宣称单一赢家。</p>',
              '<img src="comparison.png" alt="Full-period equity curves and annual return versus drawdown scatter">',
              "<h2>逐阶段入选名单</h2><p>训练阶段每个家族选一项；validation 后按冻结规则确定前三。★ 表示最终预选前三。</p>",
              "<table><thead><tr><th>家族</th><th>训练阶段入选</th><th>前三名单</th></tr></thead><tbody>"]
    for family, names in protocol["families"].items():
        winner = training["selected_by_family"][family]
        finalist = "★" if winner in selected else ""
        chunks.append(f"<tr><td>{cell(FAMILY_NAMES.get(family, family))}</td><td>{cell(winner)}</td><td>{finalist}</td></tr>")
    chunks.append("</tbody></table><h2>全部20组候选策略</h2><p>参数来自冻结协议；结果按家族及预先固定的 variant 顺序呈现，供完整比较。</p>")
    chunks.append('<table><thead><tr><th>\u5bb6\u65cf</th><th>\u5019\u9009</th><th>\u53c2\u6570\u8bf4\u660e</th><th>\u51bb\u7ed3\u53c2\u6570</th><th>\u8bad\u7ec3\u5165\u9009</th><th>\u524d\u4e09</th><th>Full\u5e74\u5316\u6536\u76ca</th><th>Full\u6700\u5927\u56de\u64a4</th><th>Full Sharpe</th><th>Late\u533a\u95f4\u6536\u76ca</th><th>Late\u6700\u5927\u56de\u64a4</th><th>Late Sharpe</th><th>Full\u6210\u672c\uff08\u5143\uff09</th><th>Late\u6210\u672c\uff08\u5143\uff09</th></tr></thead><tbody>')
    for family, names in protocol["families"].items():
        for name in names:
            f = lookup[name, "full", 1]["metrics"]
            l = lookup[name, "late", 1]["metrics"]
            trainmark = "✓" if training["selected_by_family"][family] == name else ""
            finalmark = "★" if name in selected else ""
            params = json.dumps(protocol["candidates"][name]["params"], ensure_ascii=False, separators=(",", ":"))
            variant = protocol["candidates"][name]["params"].get("variant")
            description = protocol["descriptions"][family][variant] if variant is not None else ""
            vals = [FAMILY_NAMES.get(family, family), name, description, params, trainmark, finalmark,
                    fmt(f["annual_return"], True), fmt(f["max_drawdown"], True), fmt(f["sharpe"]),
                    fmt(l["total_return"], True), fmt(l["max_drawdown"], True), fmt(l["sharpe"]),
                    fmt_cost(f["execution_cost"]), fmt_cost(l["execution_cost"])]
            chunks.append("<tr>" + "".join(f'<td class="{"param" if i == 2 else ""}">{cell(v)}</td>' for i, v in enumerate(vals)) + "</tr>")
    chunks.append("</tbody></table><h2>\u57fa\u51c6\u7b56\u7565</h2><table><thead><tr><th>\u57fa\u51c6</th><th>Full\u5e74\u5316\u6536\u76ca</th><th>Full\u6700\u5927\u56de\u64a4</th><th>Full Sharpe</th><th>Late\u533a\u95f4\u6536\u76ca</th><th>Late\u6700\u5927\u56de\u64a4</th><th>Late Sharpe</th><th>Full\u6210\u672c\uff08\u5143\uff09</th><th>Late\u6210\u672c\uff08\u5143\uff09</th></tr></thead><tbody>")
    for name in protocol["baselines"]:
        f, l = lookup[name, "full", 1]["metrics"], lookup[name, "late", 1]["metrics"]
        chunks.append("<tr>" + "".join(f"<td>{cell(v)}</td>" for v in [BASELINE_NAMES.get(name, name), fmt(f['annual_return'], True), fmt(f['max_drawdown'], True), fmt(f['sharpe']), fmt(l['total_return'], True), fmt(l['max_drawdown'], True), fmt(l['sharpe']), fmt_cost(f['execution_cost']), fmt_cost(l['execution_cost'])]) + "</tr>")
    chunks.append("</tbody></table><h2>\u53cc\u500d\u6210\u672c\u538b\u529b\u6d4b\u8bd5</h2><table><thead><tr><th>\u9884\u9009\u914d\u7f6e</th><th>\u65f6\u6bb5</th><th>\u6210\u672c\u500d\u6570</th><th>\u6536\u76ca\uff08Full\u5e74\u5316 / Late\u533a\u95f4\uff09</th><th>\u6700\u5927\u56de\u64a4</th><th>Sharpe</th><th>\u4ea4\u6613\u6210\u672c\uff08\u5143\uff09</th></tr></thead><tbody>")
    stress_names = selected + ["group_equal_80", "group_equal_40"]
    for name in stress_names:
        for period in ("full", "late"):
            m = lookup[name, period, 2]["metrics"]
            chunks.append("<tr>" + "".join(f"<td>{cell(v)}</td>" for v in [name, period, "2×", (fmt(m['annual_return'] if period == "full" else m['total_return'], True)), fmt(m['max_drawdown'], True), fmt(m['sharpe']), fmt_cost(m['execution_cost'])]) + "</tr>")
    chunks.append("</tbody></table><p>\u6210\u672c\u4e3a\u4f63\u91d1\u4e0e\u6ed1\u70b9\u5408\u8ba1\u7684\u4eba\u6c11\u5e01\u91d1\u989d\u3002</p></body></html>")
    path = docs / "dashboard.html"
    path.write_text("\n".join(chunks), encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="research output directory")
    parser.add_argument("--docs", type=Path, default=DOCS, help="report and audit directory")
    args = parser.parse_args()
    root, docs = args.root.resolve(), args.docs.resolve()
    protocol = load(root / "protocol.json")
    results = load(docs / "results.json")
    training = load(docs / "training_selection.json")
    selection = load(docs / "selection.json")
    verification = load(docs / "verification.json")
    result_hash = hashlib.sha256((docs / "results.json").read_bytes()).hexdigest()
    protocol_hash = sha256(root / "protocol.json")
    training_hash = sha256(docs / "training_selection.json")
    if sha256(docs / "protocol.json") != protocol_hash:
        raise ValueError("Documentation protocol differs from the frozen research protocol")
    if training.get("protocol_sha256") != protocol_hash:
        raise ValueError("Training selection does not match the frozen protocol")
    if selection.get("protocol_sha256") != protocol_hash or selection.get("training_selection_sha256") != training_hash:
        raise ValueError("Final selection does not match the frozen protocol and training selection")
    if (not verification.get("verified") or verification.get("protocol_sha256") != protocol_hash
            or verification.get("results_sha256") != result_hash):
        raise ValueError("Final audit verification is missing or does not match results.json")
    keys, expected = validate(results, protocol)
    stress_names = selection["selected"] + ["group_equal_80", "group_equal_40"]
    expected |= {(name, period, 2) for name in stress_names for period in ("full", "late")}
    if len(results) != 118 or len(set(keys)) != len(keys) or set(keys) != expected:
        raise ValueError(f"Expected 118 unique audited rows; found {len(results)} rows and {len(set(keys))} identities")
    if any(row.get("protocol_sha256") != protocol_hash for row in results):
        raise ValueError("One or more result rows do not match the frozen protocol")
    if len(protocol["families"]) != 10 or sum(map(len, protocol["families"].values())) != 20:
        raise ValueError("Protocol does not define the expected ten families and twenty variants")
    chart_path = chart(root, docs, protocol, results, selection["selected"])
    html_path = report(docs, protocol, results, training, selection)
    print(f"Wrote {chart_path}\nWrote {html_path}")


if __name__ == "__main__":
    main()
