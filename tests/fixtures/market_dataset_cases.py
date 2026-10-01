import pandas as pd

from quanto.data import MarketData, MarketConfig
from quanto.data.providers.akshare import AKShareProvider


def research_market(root, mode="complete"):
    def sdk(function, kwargs, context):
        context.remaining()
        if mode == "broken":
            raise RuntimeError("provider unavailable")
        if function == "tool_trade_date_hist_sina":
            return pd.DataFrame({"trade_date": pd.bdate_range("2024-01-01", "2024-01-31")})
        days = pd.bdate_range("2024-01-01", "2024-01-03" if mode == "prefix" and function == "fund_etf_hist_em" else "2024-01-05")
        if function == "fund_etf_hist_em":
            return pd.DataFrame({"日期": days, "开盘": 1., "最高": 1.1, "最低": .9,
                                 "收盘": 1., "成交量": 1000., "成交额": 10000.})
        return pd.DataFrame({"date": days, "close": 4000., "amount": 900000.})
    return MarketData(root, MarketConfig(max_retries=0, retry_delay_seconds=0), (AKShareProvider(sdk_call=sdk),))
