"""Render the audited fourth-round breadth research as a standalone dashboard."""
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
ROOT = PROJECT / "outputs/strategy-research-20261002-round4"
DOCS = PROJECT / "docs/research/2026-10-02-round4"
PERIODS = ("development", "validation", "late", "full")
FAMILY_LABELS = {"hysteresis": "不同进出场阈值", "volatility": "波动率控制", "combined": "进出阈值＋波动率"}
BASELINE = "breadth_original"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fmt(value, percent=False):
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:.2%}" if percent else f"{value:.2f}"


def difference(value, reference):
    if value is None or reference is None:
        return None
    return value - reference


def esc(value):
    return html.escape(str(value), quote=True)


def validate(root: Path, docs: Path):
    protocol_path = root / "protocol.json"
    protocol_hash = digest(protocol_path)
    require(digest(docs / "protocol.json") == protocol_hash, "Documented protocol differs from frozen protocol")
    protocol = read_json(protocol_path)
    families = protocol["families"]
    candidates = protocol["candidates"]
    baselines = protocol["baselines"]
    require(list(families) == list(FAMILY_LABELS), "Unexpected family order")
    require([len(families[f]) for f in families] == [4, 3, 3], "Expected ten candidate configurations")
    names = [name for group in families.values() for name in group] + list(baselines)
    require(len(names) == len(set(names)) == 12 and set(names) == set(candidates), "Expected twelve distinct configurations")
    require(baselines == [BASELINE, "breadth_static60"], "Unexpected baseline definitions")
    require(protocol["selection"]["target"] == {
        "full_annual_return_min": .08, "full_max_drawdown_max": .15,
        "full_sharpe_min": .7, "all_three_periods_positive": True}, "Quality gate changed")

    selection = read_json(root / "selection.json")
    require(selection.get("protocol_sha256") == protocol_hash, "Selection protocol hash differs")
    require(read_json(docs / "selection.json") == selection, "Documented selection differs from frozen selection")
    selected_by_family = selection.get("selected_by_family")
    require(isinstance(selected_by_family, dict) and set(selected_by_family) == set(families), "Invalid family selection")
    selected = [selected_by_family[family] for family in families]
    require(all(name in families[family] for family, name in selected_by_family.items()), "Selected configuration is outside its family")
    require(selection.get("selected") == selected, "Selection order differs from family order")

    results_path = docs / "results.json"
    verification = read_json(docs / "verification.json")
    require(verification.get("verified") is True and verification.get("runs") == 56, "Audit is incomplete")
    require(verification.get("protocol_sha256") == protocol_hash, "Audit protocol hash differs")
    require(verification.get("results_sha256") == digest(results_path), "Audit results hash differs")
    results = read_json(results_path)
    expected = {(name, period, 1) for name in names for period in PERIODS}
    expected |= {(name, period, 2) for name in selected + [BASELINE] for period in ("full", "late")}
    require(len(results) == len(expected) == 56, "Expected 56 audited result rows")
    lookup = {}
    required_metrics = ("total_return", "annual_return", "max_drawdown", "sharpe",
                        "average_invested_ratio", "execution_cost")
    for row in results:
        key = row["candidate"], row["period"], row["cost_multiplier"]
        require(key in expected and key not in lookup, f"Unexpected or repeated result: {key}")
        require(row.get("protocol_sha256") == protocol_hash, f"Protocol mismatch: {key}")
        require(row.get("parameters") == candidates[key[0]], f"Parameters mismatch: {key}")
        require(all(metric in row.get("metrics", {}) for metric in required_metrics), f"Missing metrics: {key}")
        folder = root / "runs" / f"{key[1]}__{key[0]}__cost{key[2]}"
        for filename, recorded_hash in row.get("artifacts", {}).items():
            require(filename in ("curves.csv", "equity.csv", "fills.csv", "average_weights.csv"),
                    f"Unexpected artifact: {filename}")
            path = folder / filename
            require(path.is_file() and digest(path) == recorded_hash, f"Artifact differs: {path}")
        require(set(row.get("artifacts", {})) == {"curves.csv", "equity.csv", "fills.csv", "average_weights.csv"},
                f"Incomplete artifacts: {key}")
        lookup[key] = row
    require(set(lookup) == expected, "Result matrix is incomplete")
    neighbor_path = docs / "neighbor_stress.json"
    if neighbor_path.exists():
        neighbors = read_json(neighbor_path)
        plan_path = docs / "neighbor_stress_plan.json"
        plan = read_json(plan_path)
        require(neighbors.get("verified") is True and neighbors["protocol_sha256"] == protocol_hash,
                "Neighbor audit is not verified against this protocol")
        require(neighbors["plan_sha256"] == digest(plan_path), "Neighbor diagnostic plan changed")
        expected_neighbors = {(name, period, 2) for name in plan["candidates"] for period in plan["periods"]}
        seen = set()
        for row in neighbors["runs"]:
            key = row["candidate"], row["period"], row["cost_multiplier"]
            require(key in expected_neighbors and key not in seen and key not in lookup, "Invalid neighbor result")
            require(row["protocol_sha256"] == protocol_hash and row["parameters"] == candidates[key[0]],
                    "Neighbor result identity differs")
            folder = root / "runs" / f"{key[1]}__{key[0]}__cost2"
            require(row == read_json(folder / "summary.json"), "Neighbor summary differs")
            for filename, recorded_hash in row["artifacts"].items():
                require(digest(folder / filename) == recorded_hash, "Neighbor artifact differs")
            lookup[key] = row
            seen.add(key)
        require(seen == expected_neighbors, "Incomplete neighbor diagnostic")
    return protocol, selection, lookup


def read_curve(root: Path, name: str):
    path = root / "runs" / f"full__{name}__cost1" / "curves.csv"
    # validate() has already matched the file to its audited digest.
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    require(rows and {"date", "equity", "drawdown"} <= set(rows[0]), f"Invalid curve columns: {path}")
    dates = [datetime.strptime(row["date"], "%Y-%m-%d") for row in rows]
    equity = [float(row["equity"]) for row in rows]
    drawdown = [float(row["drawdown"]) for row in rows]
    require(all(math.isfinite(value) for value in equity + drawdown), f"Nonfinite curve: {path}")
    require(equity[0] > 0 and all(value > 0 for value in equity), f"Invalid equity: {path}")
    return dates, equity, drawdown


def chart(root: Path, docs: Path, selected):
    names = selected + [BASELINE, "breadth_static60"]
    series = [(name, *read_curve(root, name)) for name in names]
    require(all(dates == series[0][1] for _, dates, _, _ in series), "Full-period curves have different dates")
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, (equity_ax, drawdown_ax) = plt.subplots(2, 1, figsize=(13, 8), sharex=True, constrained_layout=True)
    colors = ("#156f72", "#a46b30", "#654e8c", "#344b61", "#8a9298")
    for index, (name, dates, equity, drawdown) in enumerate(series):
        style = "--" if index >= len(selected) else "-"
        width = 2.3 if index < len(selected) else 1.6
        equity_ax.plot(dates, [value / 1_000_000 for value in equity], label=name,
                       color=colors[index], linewidth=width, linestyle=style)
        drawdown_ax.plot(dates, drawdown, color=colors[index], linewidth=width, linestyle=style)
    equity_ax.set(title="Full period · net equity", ylabel="Equity (CNY millions)")
    drawdown_ax.set(title="Full period · drawdown", ylabel="Drawdown")
    drawdown_ax.yaxis.set_major_formatter(PercentFormatter(1))
    locator = mdates.AutoDateLocator()
    drawdown_ax.xaxis.set_major_locator(locator)
    drawdown_ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    for ax in (equity_ax, drawdown_ax):
        ax.grid(color="#d8dfe2", alpha=.7)
    equity_ax.legend(ncol=3, fontsize=9, frameon=False)
    path = docs / "comparison.png"
    fig.savefig(path, dpi=160, facecolor="white")
    plt.close(fig)
    return path


def table(headers, rows):
    head = "".join(f"<th scope='col'>{esc(value)}</th>" for value in headers)
    body = "".join("<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows)
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def workbench_links(docs: Path, selected):
    records = []
    for name in ("workbench_verified.json", "workbench_neighbors_verified.json"):
        path = docs / name
        if path.exists():
            records.extend(read_json(path))
    links = []
    for row in records:
        if (row.get("candidate") in selected and row.get("period") in ("full", "late")
                and row.get("cost_multiplier") == 1 and row.get("all_metrics_equal") is True):
            url = row.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                links.append(f"<li>{esc(row['candidate'])} · {esc(row['period'])}: "
                             f"<a href='{esc(url)}'>{esc(row.get('run_id', '工作台记录'))}</a></li>")
    return "<h2>工作台记录</h2><ul>" + "".join(links) + "</ul>" if links else ""


def render(docs: Path, protocol, selection, lookup):
    families = protocol["families"]
    selected = selection["selected"]
    base = lookup[BASELINE, "full", 1]["metrics"]
    rows = []
    for family, names in families.items():
        for name in names:
            m = lookup[name, "full", 1]["metrics"]
            sub = [lookup[name, period, 1]["metrics"]["total_return"] for period in PERIODS[:3]]
            passed = (m["annual_return"] >= .08 and m["max_drawdown"] <= .15
                      and m["sharpe"] is not None and math.isfinite(m["sharpe"])
                      and m["sharpe"] >= .7 and all(value > 0 for value in sub))
            cells = [esc(FAMILY_LABELS[family]), esc(name), "入选" if name in selected else "—",
                     esc(json.dumps(protocol["candidates"][name]["params"], ensure_ascii=False, separators=(",", ":"))),
                     fmt(m["annual_return"], True), fmt(m["max_drawdown"], True), fmt(m["sharpe"]),
                     fmt(difference(m["annual_return"], base["annual_return"]), True),
                     fmt(difference(m["max_drawdown"], base["max_drawdown"]), True),
                     fmt(difference(m["sharpe"], base["sharpe"])),
                     *(fmt(value, True) for value in sub), fmt(m["average_invested_ratio"], True),
                     fmt(m["execution_cost"]), "通过" if passed else "未通过"]
            rows.append(cells)
    headers = ("家族", "配置", "预选", "冻结参数", "Full CAGR", "Full 回撤", "Full Sharpe",
               "Δ CAGR", "Δ 回撤", "Δ Sharpe", "开发收益", "验证收益", "后段收益",
               "平均仓位", "交易成本/元", "旧质量门槛")
    baseline_rows = []
    for name in protocol["baselines"]:
        m = lookup[name, "full", 1]["metrics"]
        sub = [lookup[name, period, 1]["metrics"]["total_return"] for period in PERIODS[:3]]
        baseline_rows.append([esc(name), fmt(m["annual_return"], True), fmt(m["max_drawdown"], True),
                              fmt(m["sharpe"]), *(fmt(v, True) for v in sub),
                              fmt(m["average_invested_ratio"], True), fmt(m["execution_cost"])])
    stress_rows = []
    neighbors = [name for name in families["combined"] if name not in selected and (name, "full", 2) in lookup]
    for name in selected + [BASELINE] + neighbors:
        for period in ("full", "late"):
            normal = lookup[name, period, 1]["metrics"]
            stress = lookup[name, period, 2]["metrics"]
            key = "annual_return" if period == "full" else "total_return"
            label = name + ("（追加邻域检查）" if name in neighbors else "")
            stress_rows.append([esc(label), esc(period), fmt(normal[key], True), fmt(stress[key], True),
                                fmt(difference(stress[key], normal[key]), True), fmt(stress["max_drawdown"], True),
                                fmt(stress["sharpe"]), fmt(stress["execution_cost"])])
    evidence = " · ".join(f"<a href='{esc(name)}'>{esc(label)}</a>" for name, label in (
        ("protocol.json", "冻结协议"), ("selection.json", "预选记录"),
        ("results.json", "全部结果"), ("verification.json", "审计验证")))
    blocks = ["<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
              "<meta name='viewport' content='width=device-width,initial-scale=1'>",
              "<title>第四轮 · 市场宽度与波动率研究</title>",
              "<style>body{margin:0;background:#f4f6f5;color:#243239;font:15px/1.65 system-ui,'Microsoft YaHei',sans-serif}"
              "main{max-width:1440px;margin:auto;padding:32px 24px 70px}h1{font-size:30px;margin:0 0 6px}h2{font-size:21px;margin-top:38px}"
              ".lead{color:#52646a;margin:0 0 20px}.panel{background:white;border:1px solid #dce3e2;border-radius:10px;padding:20px 24px;margin:22px 0}"
              ".note{border-left:4px solid #ad8446;background:#fcf8ef;padding:12px 16px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}"
              "th,td{border-bottom:1px solid #e2e8e6;padding:8px 10px;text-align:right}th{background:#edf2f0;color:#42565b;position:sticky;top:0}"
              "th:first-child,td:first-child,th:nth-child(2),td:nth-child(2),th:nth-child(4),td:nth-child(4){text-align:left}"
              "tr:hover td{background:#f5f9f7}a{color:#176f74}img{max-width:100%;height:auto}small{color:#607277}</style></head><body><main>",
              "<h1>第四轮 · 市场宽度与波动率研究</h1>",
              "<p class='lead'>固定协议下的 12 组配置、56 次主回测；三组新策略在开发与验证期后预选。"
              "相邻组合参数另追加 4 次成本诊断，单独标记。</p>",
              f"<p>{evidence} · <a href='http://127.0.0.1:8767/runs/6a2eab55-4495-4c3f-8e73-4220646a635a'>原始工作台基准</a></p>",
              "<p>全期：2023-10-09—2026-09-30；开发段：2023-10-09—2024-12-31；"
              "验证段：2025-01-02—2025-12-31；后段：2026-01-05—2026-09-30。收益均已扣佣金和滑点。</p>",
              "<div class='note'>开发、验证、后段均各自从现金和未激活宽度状态启动，区间收益不能连乘成 Full 收益。"
              "这些日期及历史数据已用于探索，不是独立留出样本。ETF 池按当前资料回溯筛选，含幸存者和目录偏差；"
              "历史复权口径及供应商价格锚点尚未完全核实，交易状态与结算规则是研究假设。</div>",
              "<section class='panel'><h2>结论与取舍</h2>"
              "<p>预选三组中，55% 进场、45% 退出并限制组合波动率至 10% 的配置通过了原先全部质量门槛。"
              "12% 和 15% 是事先列入完整参数矩阵的相邻配置，其全期比较及追加成本检查属于事后探索；预选名单保持不变。</p>"
              "<p>保留原宽度开关、仅加波动率控制的 12% 与 15% 配置也通过了正常成本门槛。"
              "组合控制的平均仓位明显低于原策略，表内同时列出固定降仓对照，便于区分降低仓位和改变择时的效果。</p>"
              "<p>单独改变宽度进出阈值的四组配置在本样本中做出了相同决定。与原策略相比，仅在 2026-06-12、"
              "宽度为 45% 时出现一次不同的周度目标：原策略清仓，新规则继续持有。"
              "因此宽度缓冲带来的增益集中于一次判断，不能把四组相同结果当成四份独立的稳健证据。"
              "<a href='hysteresis-diagnostic.json'>完整历史上下文的信号核对</a>；"
              "<a href='june-price-audit.json'>关键 6 月行情的独立价格核对</a>。</p>"
              "<p>本轮方法假设参考 <a href='https://www.nber.org/papers/w22208'>Moreira 与 Muir 的波动率管理研究</a>；"
              "这里使用当前候选篮子的过去 60 日波动率，只减仓、不加杠杆。论文中的市场与本 ETF 样本不同，实测结果见下表。</p></section>",
              "<section class='panel'><h2>全期间净值与回撤</h2><p>实线为开发和验证期预选的三组配置，虚线为两个基准；均为正常成本。</p>"
              "<img src='comparison.png' alt='五组配置的全期间净值与回撤曲线'></section>",
              "<section class='panel'><h2>全部候选配置</h2>",
              "<p>顺序按冻结家族与参数表。Δ 相对 breadth_original，正回撤差值表示回撤更大。"
              "旧质量门槛：Full CAGR ≥ 8%、最大回撤 ≤ 15%、Sharpe ≥ 0.7，且三个独立子期间收益均为正。"
              "Full 与后段的其他优胜点属于事后探索，不改变预选名单。</p>", table(headers, rows), "</section>",
              "<section class='panel'><h2>两个基准</h2>",
              table(("基准", "Full CAGR", "Full 回撤", "Full Sharpe", "开发收益", "验证收益", "后段收益", "平均仓位", "交易成本/元"), baseline_rows), "</section>",
              "<section class='panel'><h2>双倍交易成本压力测试</h2><p>佣金率、最低佣金和滑点均乘以 2；收益列 Full 为 CAGR，后段为区间收益。"
              "追加邻域检查是在查看正常成本结果后进行的诊断，不改变预选名单；"
              "<a href='neighbor_stress_plan.json'>追加方案</a> · <a href='neighbor_stress.json'>核验结果</a>。"
              "费用变化还可能改变整手数量与超限减仓后的执行路径，所以个别高成本收益反而更高；"
              "<a href='../2026-10-02-round3/cost-path-diagnostic.json'>原策略已有逐日诊断</a>。本轮三个组合的双倍成本收益均下降。</p>",
              table(("配置", "区间", "正常收益", "双倍成本收益", "收益差", "双倍成本回撤", "双倍成本 Sharpe", "双倍交易成本/元"), stress_rows), "</section>",
              workbench_links(docs, selected + neighbors),
              "<small>数值按审计结果生成；表内四舍五入仅用于展示。交易成本为佣金与滑点之和。</small></main></body></html>"]
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
    png = chart(root, docs, selection["selected"])
    page = render(docs, protocol, selection, lookup)
    print(f"Wrote {png}\nWrote {page}")


if __name__ == "__main__":
    main()
