# quanto：A 股行业 ETF 趋势轮动研究

本项目研究场内行业 ETF 的中期多头趋势策略。目标交易设计见[策略设计](docs/02_策略设计.md)，研究需求见[需求分析](docs/01_需求分析.md)。这些文档描述的是待验证的规则，不表示当前代码已实现该设计，也不表示策略已达到实盘条件。

近期系统目标是建设网页回测工作台，让用户和 Agent 使用同一套能力编辑策略代码与参数、反复回测、查看过程并比较结果。待评审的完整设计见[工作台架构设计](docs/planning-artifacts/architecture.md)，概览见[系统架构](docs/03_量化系统架构.md)。首版方案按本机单用户、ETF 日线研究设计；网页与实验服务尚未实现。

## 目标交易设计

- 候选 ETF 先通过自身的中期趋势条件，再在合格行业之间以相对动量排序。波动率主要用于仓位与风险预算；量能用于流动性和行情诊断，不被无条件视为利好。
- 以周评估和数周至数月持有为待验证基线，不强制每周换仓。已有持仓有保留缓冲；先处理退出，再用合格候选补充可用名额。每天检查持仓和账户风险，收盘后决定在后续可交易时点执行。
- 按行业及相关暴露控制集中度。候选不足或风险预算不允许时，可以少持仓或全现金。一般市场环境变化主要调整风险预算，极端风险可以触发退出；每类风险退出都要有相应的复核和恢复条件。
- VaR、ATR 和回撤阈值用于估计风险或触发处置，不能保证实际最大亏损。趋势窗口、排名范围、持仓数量、风险阈值和收益目标仍需研究并事先明确，没有已确认的最优取值。

景气信息和 GRU 是后续独立研究方向，见[GRU 研究备忘录](docs/04_深度学习方案.md)。它们目前不属于目标交易规则。

研究先检验收益持续时间是否支持周评估与中期持有，并明确组合共同损失约束；再检验行情广度、头部贡献和热度的增量价值。这些诊断暂不构成买卖信号；周期证据不足时可以否定方案，不自动转为短线。

## 当前仓库状态

仓库包含数据拉取、离线样例数据和命令行回测原型。现有 `configs/strategy.yaml` 与回测代码仍采用早期多因子参数和周频轮动逻辑；下列命令运行的是这个**现有原型**，不能用于验证上面的新设计已落地。新规则的实现、参数校准、样本外验证和实盘可行性均尚待完成。

## 快速启动

安装开发环境：

```bash
pip install -e ".[dev]"
```

如需通过 AKShare 拉取真实数据，安装数据源依赖：

```bash
pip install -e ".[dev,data]"
```

拉取并缓存真实 ETF 与基准数据：

```bash
python -m quanto.cli fetch-data --start-date 2018-01-01 --end-date 2026-05-19
```

该命令会优先复用 `data/raw` 下的本地缓存；只有缺失覆盖区间或使用 `--force-refresh` 时才重新走网络。标准化后的回测输入会写到 `data/processed/prices.csv` 和 `data/processed/benchmark.csv`。

生成离线样例数据并运行现有回测原型：

```bash
python -m quanto.cli generate-sample-data --output-dir data/processed
python -m quanto.cli run-backtest --config configs/strategy.yaml --universe configs/universe.yaml
```

默认读取 `data/processed` 下的本地缓存数据：

- `prices.parquet` 或 `prices.csv`，列为 `date,symbol,open,high,low,close,volume,amount`
- `benchmark.parquet` 或 `benchmark.csv`，列为 `date,close,turnover`

回测输出写入 `outputs/backtests/{run_id}`，包含 `equity_curve.csv`、`trades.csv`、`positions.csv`、`signals.csv`、`metrics.json` 和 `report.md`。

运行测试：

```bash
pytest -q
```
