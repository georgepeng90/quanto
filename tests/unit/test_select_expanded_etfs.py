import pandas as pd
import pytest
import json

from scripts.select_expanded_etfs import classify, select


def test_industry_classification_precedes_broad_market():
    assert classify("半导体ETF", "中证500") == "semiconductor"
    assert classify("中证500质量ETF") == "factor"


@pytest.mark.parametrize(("name", "index", "category"), [
    ("中药ETF", "", "healthcare"),
    ("稀有金属ETF", "", "cyclical"),
    ("工业母机ETF", "", "advanced_manufacturing"),
    ("电信ETF", "", "communications"),
    ("信创ETF", "", "technology"),
    ("粮食ETF", "", "agriculture"),
    ("券商ETF", "", "financials"),
    ("碳中和ETF", "", "utilities"),
    ("科创创业ETF", "", "broad_market"),
    ("之江凤凰ETF", "", "other"),
])
def test_added_theme_keywords(name, index, category):
    assert classify(name, index) == category


def test_szse_scale_converts_shares_times_nav_to_cny(tmp_path):
    input_dir = tmp_path
    (input_dir / "sse_funds.json").write_text('{"result": [{"FUND_CODE":"", "CATEGORY":"", "FUND_EXPANSION_ABBR":"", "LISTING_DATE":"", "SCALE":"", "INDEX_NAME":""}]}', encoding="utf-8")
    pd.DataFrame([{
        "基金代码": "159901", "基金简称": "计算机ETF", "基金类别": "ETF", "投资类别": "股票基金",
        "上市日期": "2020-01-01", "当前规模(份)": "300000000", "净值": "1.0",
    }]).to_csv(input_dir / "szse_funds.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([{"代码": "sz159901", "名称": "计算机ETF", "最新价": "1", "成交额": "12000000"}]).to_csv(
        input_dir / "sina_etfs.csv", index=False, encoding="utf-8-sig"
    )
    with pytest.raises(ValueError, match="Only 1 symbols selected"):
        select(input_dir, input_dir / "selection.json")
    result = json.loads((input_dir / "selection.json").read_text(encoding="utf-8"))
    assert result["symbols"][0]["scale_cny"] == 300_000_000


def test_cross_border_candidates_are_written_to_excluded(tmp_path):
    rows = [
        {"FUND_CODE": code, "CATEGORY": "F111", "FUND_EXPANSION_ABBR": name, "LISTING_DATE": "2020-01-01", "SCALE": "5", "INDEX_NAME": index}
        for code, name, index in (("512480", "半导体ETF", "中证半导体"), ("513100", "纳指ETF", "纳斯达克100"), ("517000", "沪港深科技ETF", "中证科技"))
    ]
    (tmp_path / "sse_funds.json").write_text(json.dumps({"result": rows}), encoding="utf-8")
    pd.DataFrame(columns=["基金代码", "基金简称", "基金类别", "投资类别", "上市日期", "当前规模(份)", "净值"]).to_csv(
        tmp_path / "szse_funds.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame([
        {"代码": f"sh{code}", "名称": name, "最新价": "1", "成交额": "12000000"}
        for code, name, _ in (("512480", "半导体ETF", ""), ("513100", "纳指ETF", ""), ("517000", "沪港深科技ETF", ""))
    ]).to_csv(
        tmp_path / "sina_etfs.csv", index=False, encoding="utf-8-sig"
    )
    with pytest.raises(ValueError, match="Only 1 symbols selected"):
        select(tmp_path, tmp_path / "selection.json")
    selected = json.loads((tmp_path / "selection.json").read_text(encoding="utf-8"))["symbols"]
    excluded = pd.read_csv(tmp_path / "excluded.csv", encoding="utf-8-sig")
    assert [item["symbol"] for item in selected] == ["512480"]
    assert set(excluded["symbol"].astype(str).str.zfill(6)) == {"513100", "517000"}
