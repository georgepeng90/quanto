"""Build a static comparison report from the completed round-three audit."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


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
    "diversified_200_inverse_vol": "200日逆波动分散", "buy_hold": "全额买入持有",
    "cash_buy_hold_40": "40%买入持有", "group_equal_80": "分组等权80%",
    "group_equal_40": "分组等权40%",
}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def fmt(value, percent=False):
    if value is None:
        return "—"
    return f"{value:.2%}" if percent else f"{value:.3f}"


def validate(results, protocol):
    candidates = set(protocol["candidates"])
    keys = [(r["candidate"], r["period"], r["cost_multiplier"]) for r in results]
    expected = {(name, period, 1) for name in candidates for period in ("development", "validation", "late", "full")}
    # Final stress scope is fixed by protocol, while the names come from the saved shortlist.
    return keys, expected


def read_curve(root, name):
    path = root / "runs" / f"full__{name}__cost1" / "curves.csv"
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or "date" not in rows[0] or "equity" not in rows[0]:
        raise ValueError(f"Invalid curve file: {path}")
    return [r["date"] for r in rows], [float(r["equity"]) for r in rows]


def font(size):
    for path in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def chart(root, docs, protocol, results, selected):
    W, H = 1500, 760
    im = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(im)
    title, label, small = font(25), font(17), font(13)
    left = (85, 100, 780, 665)
    right = (865, 100, 1445, 665)
    draw.text((85, 28), "Round 3: full-period equity and risk/return", fill="#142338", font=title)

    chart_names = list(selected) + FINALIST_BASELINES
    series = [(name, *read_curve(root, name)) for name in chart_names]
    dates0 = series[0][1]
    if any(dates != dates0 for _, dates, _ in series):
        raise ValueError("Full-period chart curves do not share the same dates")
    vals = [v for _, _, line in series for v in line]
    lo, hi = min(vals), max(vals)
    if lo == hi:
        hi += 1
    palette = ["#1261a0", "#de6b35", "#419d78", "#845ec2", "#a87825", "#52616b"]
    draw.rectangle(left, outline="#6b7785", width=2)
    for frac in (0, .25, .5, .75, 1):
        y = left[3] - round(frac * (left[3] - left[1]))
        value = lo + (hi - lo) * frac
        draw.line((left[0], y, left[2], y), fill="#e7ebef")
        draw.text((left[0] - 77, y - 8), f"{value / 1e6:.2f}m", fill="#34495e", font=small)
    for idx, (name, _, line) in enumerate(series):
        pts = [(left[0] + round(i * (left[2] - left[0]) / (len(line) - 1)),
                left[3] - round((v - lo) / (hi - lo) * (left[3] - left[1]))) for i, v in enumerate(line)]
        draw.line(pts, fill=palette[idx % len(palette)], width=3 if name in selected else 2)
        draw.text((left[0] + (idx % 2) * 310, 680 + (idx // 2) * 21),
                  f"{'★ ' if name in selected else ''}{name}", fill=palette[idx % len(palette)], font=small)
    draw.text((left[0], 72), "Equity (CNY)", fill="#34495e", font=label)
    draw.text((left[0], 728), f"{dates0[0]}  →  {dates0[-1]}", fill="#34495e", font=small)

    full = {r["candidate"]: r["metrics"] for r in results if r["period"] == "full" and r["cost_multiplier"] == 1}
    scatter_names = [name for name in protocol["candidates"] if name.startswith(tuple(FAMILY_NAMES.keys()))]
    # Use the frozen family map to define exactly the twenty experimental variants.
    scatter_names = [name for names in protocol["families"].values() for name in names]
    points = [(full[n]["annual_return"], full[n]["max_drawdown"], n) for n in scatter_names]
    points += [(full[n]["annual_return"], full[n]["max_drawdown"], n) for n in protocol["baselines"]]
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymin, ymax = min(p[1] for p in points), max(p[1] for p in points)
    if xmin == xmax: xmax += .01
    if ymin == ymax: ymax += .01
    def xy(x, y):
        return (right[0] + round((x - xmin) / (xmax - xmin) * (right[2] - right[0])),
                right[3] - round((y - ymin) / (ymax - ymin) * (right[3] - right[1])))
    draw.rectangle(right, outline="#6b7785", width=2)
    for i in range(5):
        x = xmin + (xmax - xmin) * i / 4
        y = ymin + (ymax - ymin) * i / 4
        px, py = xy(x, y)
        draw.line((px, right[1], px, right[3]), fill="#e7ebef")
        draw.line((right[0], py, right[2], py), fill="#e7ebef")
        draw.text((px - 22, right[3] + 6), f"{x:.0%}", fill="#34495e", font=small)
        draw.text((right[0] - 55, py - 7), f"{y:.0%}", fill="#34495e", font=small)
    for x, y, name in points:
        px, py = xy(x, y)
        if name in protocol["baselines"]:
            rad, color = 7, "#7b8794"
            draw.polygon([(px, py-rad), (px+rad, py), (px, py+rad), (px-rad, py)], fill=color)
        else:
            rad, color = 6, "#1261a0"
            draw.ellipse((px-rad, py-rad, px+rad, py+rad), fill=color)
        if name in selected:
            draw.ellipse((px-11, py-11, px+11, py+11), outline="#e05858", width=3)
    draw.text((right[0], 72), "All 20 family variants and seven baselines", fill="#34495e", font=label)
    draw.text((right[0], 680), "Annual return →     Max drawdown ↑     Red ring: preselected top three", fill="#34495e", font=small)
    draw.text((right[0], 703), "Blue circle: family variant     Gray diamond: baseline", fill="#34495e", font=small)
    out = docs / "comparison.png"
    im.save(out, format="PNG", optimize=True)
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
    chunks.append('<table><thead><tr><th>家族</th><th>候选</th><th>参数说明</th><th>冻结参数</th><th>训练入选</th><th>前三</th><th>Full年化</th><th>Full回撤</th><th>Full Sharpe</th><th>Late年化</th><th>Late回撤</th><th>Late Sharpe</th><th>Full成本</th><th>Late成本</th></tr></thead><tbody>')
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
                    fmt(l["annual_return"], True), fmt(l["max_drawdown"], True), fmt(l["sharpe"]),
                    fmt(f["execution_cost"], True), fmt(l["execution_cost"], True)]
            chunks.append("<tr>" + "".join(f'<td class="{"param" if i == 2 else ""}">{cell(v)}</td>' for i, v in enumerate(vals)) + "</tr>")
    chunks.append("</tbody></table><h2>基准策略</h2><table><thead><tr><th>基准</th><th>Full年化</th><th>Full回撤</th><th>Full Sharpe</th><th>Late年化</th><th>Late回撤</th><th>Late Sharpe</th><th>Full成本</th><th>Late成本</th></tr></thead><tbody>")
    for name in protocol["baselines"]:
        f, l = lookup[name, "full", 1]["metrics"], lookup[name, "late", 1]["metrics"]
        chunks.append("<tr>" + "".join(f"<td>{cell(v)}</td>" for v in [BASELINE_NAMES.get(name, name), fmt(f['annual_return'], True), fmt(f['max_drawdown'], True), fmt(f['sharpe']), fmt(l['annual_return'], True), fmt(l['max_drawdown'], True), fmt(l['sharpe']), fmt(f['execution_cost'], True), fmt(l['execution_cost'], True)]) + "</tr>")
    chunks.append("</tbody></table><h2>双倍成本压力测试</h2><table><thead><tr><th>预选配置</th><th>时段</th><th>成本倍数</th><th>年化收益</th><th>最大回撤</th><th>Sharpe</th><th>交易成本</th></tr></thead><tbody>")
    stress_names = selected + ["group_equal_80", "group_equal_40"]
    for name in stress_names:
        for period in ("full", "late"):
            m = lookup[name, period, 2]["metrics"]
            chunks.append("<tr>" + "".join(f"<td>{cell(v)}</td>" for v in [name, period, "2×", fmt(m['annual_return'], True), fmt(m['max_drawdown'], True), fmt(m['sharpe']), fmt(m['execution_cost'], True)]) + "</tr>")
    chunks.append("</tbody></table><p>成本列为佣金与滑点合计相对于初始资金的比例。报告只呈现冻结筛选名单与全部结果，不据 late 表现重选。</p></body></html>")
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
    if not verification.get("verified") or verification.get("results_sha256") != result_hash:
        raise ValueError("Final audit verification is missing or does not match results.json")
    keys, expected = validate(results, protocol)
    stress_names = selection["selected"] + ["group_equal_80", "group_equal_40"]
    expected |= {(name, period, 2) for name in stress_names for period in ("full", "late")}
    if len(results) != 118 or len(set(keys)) != len(keys) or set(keys) != expected:
        raise ValueError(f"Expected 118 unique audited rows; found {len(results)} rows and {len(set(keys))} identities")
    if len(protocol["families"]) != 10 or sum(map(len, protocol["families"].values())) != 20:
        raise ValueError("Protocol does not define the expected ten families and twenty variants")
    chart_path = chart(root, docs, protocol, results, selection["selected"])
    html_path = report(docs, protocol, results, training, selection)
    print(f"Wrote {chart_path}\nWrote {html_path}")


if __name__ == "__main__":
    main()
