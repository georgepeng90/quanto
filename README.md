# quanto：A 股行业 ETF 趋势轮动研究

本项目研究场内行业 ETF 的中期多头趋势策略。目标交易设计见[策略设计](docs/02_策略设计.md)，研究需求见[需求分析](docs/01_需求分析.md)。这些文档描述的是待验证的规则，不表示当前代码已实现该设计，也不表示策略已达到实盘条件。

本机网页回测工作台已实现：用户和 Agent 共用数据版本、策略修订、实验、队列、结果与导出服务。首版验证平台为 Windows，采用本机单用户、ETF 日线研究。第一次了解项目，先看[工作台入门与完整讲解](docs/00_工作台入门与完整讲解.md)，其中说明页面用途、一次回测的过程、结果解读和研究边界。具体操作见[使用指南](docs/05_回测工作台使用指南.md)，设计见[工作台架构](docs/planning-artifacts/architecture.md)，逐阶段验证与取舍见[实施计划](docs/superpowers/plans/2026-09-29-web-backtest-workbench.md)。

## 目标交易设计

- 候选 ETF 先通过自身的中期趋势条件，再在合格行业之间以相对动量排序。波动率主要用于仓位与风险预算；量能用于流动性和行情诊断，不被无条件视为利好。
- 以周评估和数周至数月持有为待验证基线，不强制每周换仓。已有持仓有保留缓冲；先处理退出，再用合格候选补充可用名额。每天检查持仓和账户风险，收盘后决定在后续可交易时点执行。
- 按行业及相关暴露控制集中度。候选不足或风险预算不允许时，可以少持仓或全现金。一般市场环境变化主要调整风险预算，极端风险可以触发退出；每类风险退出都要有相应的复核和恢复条件。
- VaR、ATR 和回撤阈值用于估计风险或触发处置，不能保证实际最大亏损。趋势窗口、排名范围、持仓数量、风险阈值和收益目标仍需研究并事先明确，没有已确认的最优取值。

景气信息和 GRU 是后续独立研究方向，见[GRU 研究备忘录](docs/04_深度学习方案.md)。它们目前不属于目标交易规则。

研究先检验收益持续时间是否支持周评估与中期持有，并明确组合共同损失约束；再检验行情广度、头部贡献和热度的增量价值。这些诊断暂不构成买卖信号；周期证据不足时可以否定方案，不自动转为短线。

## 当前仓库状态

工作台包含独立策略协议、买入持有与趋势轮动模板、按时间推进的账本、不可变快照、持久队列和中文网页。`configs/strategy.yaml` 不是工作台实验输入；实验使用 JSON 契约。模板可运行不表示目标交易设计的全部研究规则或盈利能力已经验证。参数校准、样本外验证和实盘可行性仍待研究。

新登记的行情数据使用 Parquet，SQLite 管理版本、实验和任务。新回测共享一份经校验的数据对象，分别冻结策略与配置；运行时读取所选标的至结束日的行情，保留起点前全部历史。CSV 继续用于导入、下载缓存及报告导出。旧数据和旧快照不改写；完整工作区备份须包含共享数据对象与结果目录。设计与实测见[存储优化方案](docs/superpowers/specs/2026-09-30-market-data-storage-design.md)和[基准记录](docs/superpowers/benchmarks/2026-09-30-market-data-storage-baseline.md)。

## 快速启动

代码已合入 `v2`。在仓库根目录 `D:\Projects\quanto` 执行（Python 3.13、Node.js 24、uv）：

```powershell
uv sync --locked --extra dev --extra web --extra data
npm.cmd --prefix web ci
npm.cmd --prefix web run build
.\scripts\start-workbench.ps1
```

打开 [本地工作台](http://127.0.0.1:8765)。一键脚本检查依赖，CLI 检查构建源码/产物哈希并明确拒绝缺失、过期构建或占用端口；不另开前端端口。直接启动等价命令为 `uv run --no-sync quanto serve --host 127.0.0.1 --port 8765 --workspace data/workbench`，须从当前 checkout 根目录执行。默认数据位于 `data/workbench`，产物位于 `outputs/workbench`，路径保存在工作区的 `workspace.json`。

若 `8765` 被已有服务占用，可明确使用 `.\scripts\start-workbench.ps1 -Port 8767`，然后访问 [8767 工作台](http://127.0.0.1:8767)。Git 合并不会迁移被忽略的本地数据库和运行产物；主目录默认工作区与此前 worktree 的工作区各自独立。2026-09-30 的当前 `8765` 预览已切换到主仓 `v2` 源码和前端，继续使用旧 worktree 的 Python 环境、历史数据及产物；应保留该目录。继续使用这份预览的重启命令和验收结果见[切换记录](docs/superpowers/plans/2026-09-30-preview-cutover.md)。

首次启动自动登记两套策略模板。在“数据版本”点击“生成演示”，登记成功后进入“策略与实验”，选择数据和模板、填写有效范围/资金/用途并保存；随后在“回测任务”运行，查看订单、图表、克隆实验、比较和下载。可用演示区间为 `2018-01-02` 至 `2018-01-31`，代码 `512480`；示例金额和参数只用于流程演示。

同源 `GET /api/session` 返回本机令牌；浏览器后续请求和 Agent CLI 均以 `Authorization: Bearer <token>` 调用 `/api`。CLI 可通过 `QUANTO_TOKEN` 环境变量读取令牌，例如 `uv run --extra web quanto datasets list`。任务提交和结果查询共用这套 API；`quanto runs --help` 可查看运行命令。

旧版 `fetch-data`、`generate-sample-data` 和 `run-backtest` 命令已由任务 API 取代。演示数据使用 `POST /api/jobs/data`，请求体为 `{"kind":"demo","payload":{"years":1}}`；真实采集使用 `kind:"fetch"`，并显式检查返回的覆盖与 `result.state`。不完整采集不会注册数据版本。

运行测试：

```powershell
uv run pytest -q
npm.cmd --prefix web run test -- --run
npm.cmd --prefix web run typecheck
npm.cmd --prefix web run build
npm.cmd --prefix web exec -- playwright install chromium
npm.cmd --prefix web run test:e2e
```

浏览器测试自行启动 `127.0.0.1:8766`，使用独立临时工作区和真实后端。已有 Chrome 可设置 `$env:PLAYWRIGHT_CHANNEL='chrome'`，不影响日常启动。

真实数据的有限检查：2026-09-29 拉取 ETF `512480` 的 `2024-01-08..12` 五个交易日成功，但默认 `sh000300` 基准出现 `ConnectionError`；最终为 `incomplete / COVERAGE_INCOMPLETE`，未登记数据版本。演示/导入闭环不依赖此次联网成功，不能据此宣称真实数据整体就绪。策略以受信任本地 Python 执行，子进程不是恶意代码沙箱。复现边界、价格模式及 Windows/非 Windows 生命周期差异见使用指南。
