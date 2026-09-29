# quanto：A 股行业 ETF 趋势轮动研究

本项目研究场内行业 ETF 的中期多头趋势策略。目标交易设计见[策略设计](docs/02_策略设计.md)，研究需求见[需求分析](docs/01_需求分析.md)。这些文档描述的是待验证的规则，不表示当前代码已实现该设计，也不表示策略已达到实盘条件。

近期系统目标是建设网页回测工作台，让用户和 Agent 使用同一套能力编辑策略代码与参数、反复回测、查看过程并比较结果。已认可的设计见[工作台架构设计](docs/planning-artifacts/architecture.md)，开发任务见[实施计划](docs/superpowers/plans/2026-09-29-web-backtest-workbench.md)，概览见[系统架构](docs/03_量化系统架构.md)。首版采用本机单用户、ETF 日线研究；实验 API 已实现，网页仍在开发。

## 目标交易设计

- 候选 ETF 先通过自身的中期趋势条件，再在合格行业之间以相对动量排序。波动率主要用于仓位与风险预算；量能用于流动性和行情诊断，不被无条件视为利好。
- 以周评估和数周至数月持有为待验证基线，不强制每周换仓。已有持仓有保留缓冲；先处理退出，再用合格候选补充可用名额。每天检查持仓和账户风险，收盘后决定在后续可交易时点执行。
- 按行业及相关暴露控制集中度。候选不足或风险预算不允许时，可以少持仓或全现金。一般市场环境变化主要调整风险预算，极端风险可以触发退出；每类风险退出都要有相应的复核和恢复条件。
- VaR、ATR 和回撤阈值用于估计风险或触发处置，不能保证实际最大亏损。趋势窗口、排名范围、持仓数量、风险阈值和收益目标仍需研究并事先明确，没有已确认的最优取值。

景气信息和 GRU 是后续独立研究方向，见[GRU 研究备忘录](docs/04_深度学习方案.md)。它们目前不属于目标交易规则。

研究先检验收益持续时间是否支持周评估与中期持有，并明确组合共同损失约束；再检验行情广度、头部贡献和热度的增量价值。这些诊断暂不构成买卖信号；周期证据不足时可以否定方案，不自动转为短线。

## 当前仓库状态

仓库包含数据拉取、离线样例数据和旧版回测原型代码。现有 `configs/strategy.yaml` 与回测代码仍采用早期多因子参数和周频轮动逻辑，不能用于验证上面的新设计已落地。新规则的实现、参数校准、样本外验证和实盘可行性均尚待完成。

## 快速启动

安装开发环境与本机 API：

```bash
uv sync --extra dev --extra web
```

如需通过 AKShare 拉取真实数据，另安装数据源依赖：

```bash
uv sync --extra dev --extra web --extra data
```

启动本机服务：

```bash
uv run --extra web quanto serve --workspace data/workbench
```

同源 `GET /api/session` 返回本机令牌；浏览器后续请求和 Agent CLI 均以 `Authorization: Bearer <token>` 调用 `/api`。CLI 可通过 `QUANTO_TOKEN` 环境变量读取令牌，例如 `uv run --extra web quanto datasets list`。任务提交和结果查询共用这套 API；`quanto runs --help` 可查看运行命令。

旧版 `fetch-data`、`generate-sample-data` 和 `run-backtest` 命令已由任务 API 取代。演示数据使用 `POST /api/jobs/data`，请求体为 `{"kind":"demo","payload":{"years":1}}`；真实采集使用 `kind:"fetch"`，并显式检查返回的覆盖与 `result.state`。不完整采集不会注册数据版本。

运行测试：

```bash
uv run --extra dev --extra web pytest -q
```
