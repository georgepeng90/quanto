"""Select a liquidity- and size-screened ETF universe for historical research."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

import pandas as pd


ASOF = "2026-09-30"
LISTING_CUTOFF = "2026-03-31"
WINDOW = "2023-10-02..2026-10-02"
TARGET = 350
MINIMUM = 200
SSE_CATEGORIES = {"F111", "F112", "F114", "F115"}
EXCLUDED_TERMS = (
    "港", "恒生", "恒指", "纳指", "纳斯达克", "标普", "美国", "美股", "日经", "德国",
    "法国", "印度", "越南", "沙特", "巴西", "海外", "全球", "亚太", "沪港深",
    "黄金", "豆粕", "商品", "原油", "货币", "债",
)
INDUSTRIES = (
    ("semiconductor", ("半导体", "芯片")),
    ("electronics", ("电子",)),
    ("automotive", ("汽车", "智能车")),
    ("technology", ("计算机", "软件", "云", "人工智能", "大数据", "数字", "科技", "互联网", "信创")),
    ("communications", ("通信", "电信", "5G")),
    ("media", ("传媒", "游戏")),
    ("advanced_manufacturing", ("机器人", "机床", "机械", "工业母机", "高端装备", "高端制造")),
    ("new_energy", ("新能源", "光伏", "电池", "风电", "储能")),
    ("defense", ("军工", "国防", "航空航天")),
    ("financials", ("银行", "证券", "券商", "金融", "保险")),
    ("real_estate", ("地产", "房地产")),
    ("healthcare", ("医药", "医疗", "医", "创新药", "生物", "中药")),
    ("consumer", ("消费", "酒", "食品", "家电")),
    ("agriculture", ("农业", "养殖", "畜牧", "种业", "粮食")),
    ("cyclical", ("有色", "煤炭", "钢铁", "化工", "稀土", "稀有金属", "矿", "资源", "石油", "油气", "煤油", "石化", "能源")),
    ("utilities", ("电力", "公用", "环保", "绿色电", "碳中和")),
    ("infrastructure", ("基建", "建材", "建筑", "工程", "水泥")),
    ("services", ("旅游", "交通", "物流", "航运", "运输", "教育", "服务")),
)
BROAD_TERMS = (
    "300", "500", "1000", "2000", "上证", "深证", "深成", "创业板", "科创50", "科创100", "科创综",
    "科创创业", "创业大盘", "中小100", "中证100", "中证A", "50ETF", "中国A", "800", "综指",
)
FACTOR_TERMS = ("红利", "低波", "价值", "成长", "质量", "央企", "国企", "治理", "ESG", "中证盈利", "战略新兴")


def classify(name: str, index: str = "") -> str:
    text = f"{name} {index}"
    if "新能源车" in text or "新能源 汽车" in text:
        return "new_energy"
    for category, terms in INDUSTRIES:
        if any(term in text for term in terms):
            return category
    if any(term in text for term in FACTOR_TERMS):
        return "factor"
    if any(term in text for term in BROAD_TERMS):
        return "broad_market"
    return "other"


def _number(value: object) -> float:
    cleaned = re.sub(r"[,，\s]", "", str(value or ""))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _load_rows(input_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    sse_data = json.loads((input_dir / "sse_funds.json").read_text(encoding="utf-8"))
    sse = pd.DataFrame(sse_data["result"])
    sse = sse[sse["CATEGORY"].isin(SSE_CATEGORIES)].copy()
    sse_rows = pd.DataFrame({
        "symbol": sse["FUND_CODE"].astype(str).str.zfill(6),
        "name": sse["FUND_EXPANSION_ABBR"].fillna(""),
        "listing_date": sse["LISTING_DATE"],
        "scale_cny": pd.to_numeric(sse["SCALE"].map(_number)) * 100_000_000,
        "tracking_index": sse["INDEX_NAME"].replace("-", "").fillna(""),
        "source": "SSE",
    })

    szse = pd.read_csv(input_dir / "szse_funds.csv", encoding="utf-8-sig", dtype=str)
    szse = szse[(szse["基金类别"] == "ETF") & (szse["投资类别"] == "股票基金")].copy()
    szse_rows = pd.DataFrame({
        "symbol": szse["基金代码"].astype(str).str.zfill(6),
        "name": szse["基金简称"].fillna(""),
        "listing_date": szse["上市日期"],
        "scale_cny": pd.to_numeric(szse["当前规模(份)"].map(_number)) * pd.to_numeric(szse["净值"].map(_number)),
        "tracking_index": None,
        "source": "SZSE",
    })

    sina = pd.read_csv(input_dir / "sina_etfs.csv", encoding="utf-8-sig", dtype=str)
    sina = sina.rename(columns={"成交额": "latest_turnover_cny"})
    sina["symbol"] = sina["代码"].str[2:].str.zfill(6)
    sina["market"] = sina["代码"].str[:2]
    # Distinguish exchange listings when joining, because codes can overlap.
    sse_rows["market"] = "sh"
    szse_rows["market"] = "sz"
    market = pd.concat([sse_rows, szse_rows], ignore_index=True)
    market = market.merge(sina[["symbol", "market", "名称", "latest_turnover_cny"]], on=["symbol", "market"], how="left")
    market["sina_name"] = market["名称"].fillna("")
    market["latest_turnover_cny"] = market["latest_turnover_cny"].map(_number)
    market["scale_cny"] = pd.to_numeric(market["scale_cny"], errors="coerce").fillna(0)
    market["listing_date"] = pd.to_datetime(market["listing_date"], errors="coerce")
    market["tracking_index"] = market["tracking_index"].fillna("")
    return market, sina


def select(input_dir: Path, output_path: Path, target: int = TARGET) -> dict:
    rows, _ = _load_rows(input_dir)
    # Prefer the official short name, while Sina is retained as an alias for keyword screens.
    rows["name"] = rows["name"].fillna("")
    rows["search_text"] = [f"{name} {alias} {index}" for name, alias, index in zip(rows["name"], rows["sina_name"], rows["tracking_index"])]
    rows["category"] = [classify(f"{name} {alias}", index) for name, alias, index in zip(rows["name"], rows["sina_name"], rows["tracking_index"])]
    reasons: list[str] = []
    eligible_flags: list[bool] = []
    for row in rows.itertuples(index=False):
        reason = ""
        if any(term in row.search_text for term in EXCLUDED_TERMS):
            reason = "excluded foreign, commodity, cash, or bond exposure"
        elif pd.isna(row.listing_date) or row.listing_date.strftime("%Y-%m-%d") > LISTING_CUTOFF:
            reason = "listed after cutoff or listing date missing"
        elif row.scale_cny < 200_000_000:
            reason = "scale below CNY 200m"
        elif row.latest_turnover_cny < 1_000_000:
            reason = "latest single-day turnover below CNY 1m"
        elif row.category == "other":
            reason = "unclassified category"
        reasons.append(reason)
        eligible_flags.append(not reason)
    rows["exclusion_reason"] = reasons
    rows["eligible"] = eligible_flags
    rows["scale_rank"] = rows["scale_cny"].rank(method="min", ascending=False)
    rows["turnover_rank"] = rows["latest_turnover_cny"].rank(method="min", ascending=False)
    rows["rank"] = (rows["scale_rank"] + rows["turnover_rank"]) / 2
    rows["rank"] = rows["rank"].rank(method="first", ascending=True).astype(int)
    ordered = rows[rows["eligible"]].sort_values(["rank", "symbol"], kind="stable")

    picked: list[int] = []
    category_counts: dict[str, int] = {}
    index_counts: dict[str, int] = {}

    def add_candidates(candidates: pd.DataFrame, limit: int | None = None) -> None:
        added = 0
        for index, row in candidates.iterrows():
            if len(picked) >= target or (limit is not None and added >= limit):
                break
            if index in picked:
                continue
            category = row["category"]
            if category_counts.get(category, 0) >= 30:
                continue
            if category in {"broad_market", "factor"} and sum(category_counts.get(item, 0) for item in ("broad_market", "factor")) >= 80:
                continue
            tracking_index = row["tracking_index"]
            if row["source"] == "SSE" and tracking_index and index_counts.get(tracking_index, 0) >= 4:
                continue
            picked.append(index)
            category_counts[category] = category_counts.get(category, 0) + 1
            if row["source"] == "SSE" and tracking_index:
                index_counts[tracking_index] = index_counts.get(tracking_index, 0) + 1
            added += 1

    for category in ordered["category"].unique():
        if category not in {"broad_market", "factor"}:
            add_candidates(ordered[ordered["category"] == category], 4)
    add_candidates(ordered)

    chosen = rows.loc[picked].copy()
    chosen["selection_reason"] = chosen.apply(
        lambda row: f"rank {row['rank']}; size/turnover rank average; category {row['category']}", axis=1
    )
    output = {
        "metadata": {
            "asof": ASOF,
            "quote_asof": ASOF,
            "directory_collected_on": "2026-10-02",
            "scale_asof": "Official latest directory; source does not supply its valuation date",
            "information_window": WINDOW,
            "listing_cutoff": LISTING_CUTOFF,
            "target": target,
            "eligible_candidate_count": int(rows["eligible"].sum()),
            "selected_count": len(chosen),
            "category_counts": dict(sorted(chosen["category"].value_counts().to_dict().items())),
            "unknown_classification_count": int((rows["exclusion_reason"] == "unclassified category").sum()),
            "unknown_source_universe_count": int(rows["category"].eq("other").sum()),
            "selection_bias": "Historical exploration has point-in-time selection and survivorship bias.",
            "turnover_note": "Latest single-day turnover >= CNY 1m is only a relaxed candidate discovery screen; final research filtering uses cached 60-day average turnover >= CNY 10m.",
            "listing_note": "Listings after the three-year window start may be included if listed by 2026-03-31; no prices are backfilled before each ETF listing date and it cannot participate before listing.",
            "deduplication_note": "SSE tracking index limited to four selections; SZSE tracking index is unavailable, so reliable index deduplication is not possible there.",
        },
        "symbols": [
            {
                "symbol": row.symbol,
                "name": row.name,
                "category": row.category,
                "listing_date": row.listing_date.strftime("%Y-%m-%d"),
                "scale_cny": round(float(row.scale_cny), 2),
                "latest_turnover_cny": round(float(row.latest_turnover_cny), 2),
                "rank": int(row.rank),
                "source": row.source,
                "tracking_index": row.tracking_index or None,
                "selection_reason": row.selection_reason,
            }
            for row in chosen.itertuples(index=False)
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    detail_columns = ["symbol", "name", "category", "listing_date", "scale_cny", "latest_turnover_cny", "rank", "source", "tracking_index", "eligible", "exclusion_reason"]
    details = rows[detail_columns].copy()
    details["listing_date"] = details["listing_date"].dt.strftime("%Y-%m-%d")
    details.to_csv(output_path.with_name("all_eligible.csv"), index=False, encoding="utf-8-sig")
    rows.loc[~rows["eligible"], detail_columns].to_csv(output_path.with_name("excluded.csv"), index=False, encoding="utf-8-sig")
    if len(chosen) < MINIMUM:
        raise ValueError(f"Only {len(chosen)} symbols selected ({int(rows['eligible'].sum())} eligible candidates); fewer than required {MINIMUM}.")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="directory containing official SSE/SZSE and Sina metadata")
    parser.add_argument("--output", type=Path, required=True, help="selection.json output path")
    parser.add_argument("--target", type=int, default=TARGET)
    args = parser.parse_args()
    try:
        result = select(args.input, args.output, args.target)
    except ValueError as error:
        parser.exit(2, f"error: {error}\n")
    print(f"Selected {result['metadata']['selected_count']} of {result['metadata']['eligible_candidate_count']} eligible ETF candidates.")


if __name__ == "__main__":
    main()
