# quanto：A 股行业 ETF 趋势轮动研究

本项目研究场内行业 ETF 的中期多头趋势策略。目标交易设计见[策略设计](docs/02_策略设计.md)，研究需求见[需求分析](docs/01_需求分析.md)。这些文档描述的是待验证的规则，不表示当前代码已实现该设计，也不表示策略已达到实盘条件。

本机网页回测工作台已实现：用户和 Agent 共用数据版本、策略修订、实验、队列、结果与导出服务。首版验证平台为 Windows，采用本机单用户、ETF 日线研究。第一次了解项目，先看[工作台入门与完整讲解](docs/00_工作台入门与完整讲解.md)，其中说明页面用途、一次回测的过程、结果解读和研究边界。具体操作见[使用指南](docs/05_回测工作台使用指南.md)，设计见[工作台架构](docs/planning-artifacts/architecture.md)，逐阶段验证与取舍见[实施计划](docs/superpowers/plans/2026-09-29-web-backtest-workbench.md)。

工作台主导航为“行情数据、固定策略、强化学习模型、回测”。强化学习采用固定 ETF 篮子的专用模型：日线观察、自然周/月决策、持有与现金等待，使用 SB3 PPO 和 Gymnasium，复用日线账本与任务 API。模型创建不启动训练；成功会话保存最终训练状态和验证最优快照，后续可追加训练，并在回测页明确选择保存版本。操作见[固定 ETF 篮子模型训练指南](docs/07_ETF自主投资训练指南.md)。旧 PPO 与分钟回测报告保留在统一历史，分钟训练退出主导航。小规模实跑只验证软件闭环，不表示策略有效或达到实盘条件。

分钟深度学习工作台见[深度学习使用指南](docs/06_深度学习工作台使用指南.md)：真实 1m 数据导入、训练/验证/测试切分、CPU/CUDA、检查点恢复、冻结模型回测及对照。JSON 模板位于 [examples/learning](examples/learning)，隔离合成验收与实测规模见[验收记录](docs/research/2026-10-03-deeplearning/acceptance.md)。现有在线来源仍只提供日线；合成验收不证明真实行情盈利。

## 目标交易设计

- 候选 ETF 先通过自身的中期趋势条件，再在合格行业之间以相对动量排序。波动率主要用于仓位与风险预算；量能用于流动性和行情诊断，不被无条件视为利好。
- 以周评估和数周至数月持有为待验证基线，不强制每周换仓。已有持仓有保留缓冲；先处理退出，再用合格候选补充可用名额。每天检查持仓和账户风险，收盘后决定在后续可交易时点执行。
- 按行业及相关暴露控制集中度。候选不足或风险预算不允许时，可以少持仓或全现金。一般市场环境变化主要调整风险预算，极端风险可以触发退出；每类风险退出都要有相应的复核和恢复条件。
- VaR、ATR 和回撤阈值用于估计风险或触发处置，不能保证实际最大亏损。趋势窗口、排名范围、持仓数量、风险阈值和收益目标仍需研究并事先明确，没有已确认的最优取值。

景气信息和 GRU 是后续独立研究方向，见[GRU 研究备忘录](docs/04_深度学习方案.md)。它们目前不属于目标交易规则。

研究先检验收益持续时间是否支持周评估与中期持有，并明确组合共同损失约束；再检验行情广度、头部贡献和热度的增量价值。这些诊断暂不构成买卖信号；周期证据不足时可以否定方案，不自动转为短线。

## 当前仓库状态

工作台包含独立策略协议、买入持有与趋势轮动模板、按时间推进的账本、不可变快照、持久队列和中文网页。`configs/strategy.yaml` 不是工作台实验输入；实验使用 JSON 契约。模板可运行不表示目标交易设计的全部研究规则或盈利能力已经验证。参数校准、样本外验证和实盘可行性仍待研究。

行情获取与缓存由独立的 `quanto.data.MarketData` 模块管理：Parquet 保存标准行情，SQLite 保存覆盖、来源与已提交快照。日线按标的与语义序列保存，分钟线按标的与 UTC 月份分片；换数据源沿用同一结构。新回测共享经校验的数据对象，冻结策略与配置；运行时保留所选历史范围内、绩效起点前的全部行情。CSV 作为显式导入输入和报告导出格式。旧冻结数据和运行快照继续本地重放。新模块设计见[统一行情模块](docs/superpowers/specs/2026-10-01-market-data-module-design.md)，实测与容量外推见[验收记录](docs/research/2026-10-01-market-data-module-validation.md)。

## 快速启动

在使用该版本代码的 checkout 根目录执行（Python 3.13、Node.js 24、uv）：

```powershell
uv sync --locked --extra dev --extra web --extra data --extra learning
npm.cmd --prefix web ci
npm.cmd --prefix web run build
.\scripts\start-workbench.ps1
```

本机统一入口为 [回测任务](http://127.0.0.1:8770/runs)，固定策略和 ETF 自主投资共用 `data/research-20261001`。一键脚本读取未纳入 Git 的 `workbench.local.json`：`{"port":8770,"workspace":"data/research-20261001"}`；显式 `-Port`、`-Workspace` 参数优先。没有本机配置的新安装仍默认使用 `8765` 和 `data/workbench`。脚本只检查依赖，并保留已安装的可选训练依赖；CLI 检查构建源码/产物哈希并拒绝缺失、过期构建或占用端口。

本机直接启动命令为 `uv run --no-sync quanto serve --host 127.0.0.1 --port 8770 --workspace data/research-20261001`。只启动这一份服务；不要为不同策略类型另建工作区。产物位置由工作区内的 `workspace.json` 管理。

Git 分支切换不会迁移被忽略的本地数据库。调整端口时仍应指向同一个工作区，避免历史记录看起来消失。早期预览切换过程保留在[历史切换记录](docs/superpowers/plans/2026-09-30-preview-cutover.md)，其中的旧端口不是本机当前入口。

首次启动自动登记两套策略模板。首次回测前，在“行情数据”获取 ETF 真实行情或导入符合契约的 ZIP；确认登记版本的实际覆盖后，进入“固定策略 → 新建固定策略”选择数据、模板及覆盖中的标的和日期，填写资金与用途并保存。固定策略首页列出已保存配置及有效参数，点击“编辑配置”可继续修改。随后在“回测 → 新建回测”运行，查看订单、图表、克隆实验、比较和下载。示例金额和参数只用于说明操作。

“回测任务 → 全部回测”汇总固定策略、PPO 及分钟模型的回测任务，训练任务留在各自训练页。支持类型/状态/批次筛选、收益/年化/回撤/Sharpe 排序，以及勾选后的指标对照和条件差异。区间、ETF 范围、数据内容、成本、风控或估值口径不同的结果不能直接排名；缺失和损坏的结果会保留任务并显示错误。Agent 可读取同一份 `GET /api/backtests/catalog` 分页目录。

同源 `GET /api/session` 返回本机令牌；浏览器后续请求和 Agent CLI 均以 `Authorization: Bearer <token>` 调用 `/api`。CLI 可通过 `QUANTO_TOKEN` 环境变量读取令牌，例如 `uv run --extra web quanto datasets list`。任务提交和结果查询共用这套 API；`quanto runs --help` 可查看运行命令。

旧版 `fetch-data`、`generate-sample-data` 和 `run-backtest` 命令已由任务 API 取代。`POST /api/jobs/data` 的 demo 请求（例如 `{"kind":"demo","payload":{"years":1}}`）仅供开发与自动化测试；合成数据生成不属于正式用户流程。行情获取使用 `{"kind":"fetch","payload":{"symbols":["512480"],"start_date":"2024-01-08","end_date":"2024-01-12","refresh":false}}`。只有 `result.market_prefetch_state == "ready"`、回测包验证通过且返回 `dataset_version_id` 才能用于回测；通用任务 `succeeded` 也可能返回不完整覆盖。重复请求优先使用本地缓存，页面“更新已有行情”才显式刷新。普通请求不接受供应商、物理根、缓存版本或重试配置。

## 独立行情 API 与预缓存

模块公开 `coverage`、`prefetch`、`get_prefetch`、`wait_ready`、`cancel_prefetch`、`resolve_snapshot`、`read_bars`、`read_table` 和 `iter_batches`，各有同名 `_async` 方法。时间戳须有时区，区间为 `[start, end)`，标的包含交易所、类型与字符串代码。同步与异步查询都只读已提交快照；缺口不会在回测或训练读取中触发联网。等待超时或取消一个异步等待者不会取消共享获取；取消预缓存请求需显式调用 `cancel_prefetch`。

```python
from quanto.data import MarketData
from quanto.data.contracts import DataQuery
from quanto.backtest.market_dataset import build_data_requirement

requirement = build_data_requirement({"symbols": ["512480"],
    "start_date": "2024-01-08", "end_date": "2024-01-12"})
with MarketData() as market:
    task = market.prefetch(requirement)
    snapshot = market.wait_ready(task.id)  # 缺口或失败抛 MarketDataError，含 coverage
    request = requirement.requests[0]
    series = next(item for item in snapshot.quality["series"]
        if item["kind"] == "bars" and request.instruments[0].key in item["instruments"])
    query = DataQuery(**{**request.model_dump(), "price_basis": series["price_basis"]})
    prices = market.read_bars(snapshot, query)
    snapshot_id = snapshot.id  # 保存引用；关闭实例后仍可离线解析
```

```python
async def consume(requirement, query):
    async with MarketData() as market:
        task = await market.prefetch_async(requirement)
        snapshot = await market.wait_ready_async(task.id)
        async for batch in market.iter_batches_async(snapshot, query):
            yield batch  # Arrow RecordBatch；消费者暂停时不继续读取后续批次
```

深度学习可逐批读取标准 `1m` 数据，再在训练层产生特征、标签和张量；张量不写回行情事实。已有策略引擎仍使用日线回测包，`assemble_dataset` 位于回测层。盘中数据的存储与批次接口已提供，盘中策略的撮合和交易约束属于后续引擎工作。当前 AKShare/Tencent 适配器只提供日线 ETF、指数和 AKShare 日历；分钟数据需要显式离线导入或后续具备该能力的来源。当前在线价格保留供应商研究口径，单位、复权锚点和可知时间未知的限制不会被自动核实；严格 `raw_cash` 还要求有证据的单位、证券信息、交易状态和公司行动。研究交易假设仅进入消费者数据包。

## 行情管理、导入与备份

实际 A 股行情由数据模块维护一份共享库，与研究工作区、项目目录和供应商无关。模块读取用户配置 `~/.quanto/market.json` 的绝对 `root`；没有配置时使用 `~/.quanto/market`。本机配置为 `D:/Projects/quanto/data/market`。可用 `configure_market_root(Path(...))` 一次设置，全体工作台、预缓存、回测和训练随后使用同一位置；`workspace.json` 只管理研究产物路径。任务提交时冻结共享库路径。所有来源统一写入 `catalog.sqlite`、`objects/`、`evidence/`、`snapshots/`，目录不包含供应商名称。测试通过 `QUANTO_MARKET_CONFIG` 指向临时配置，避免写入实际行情库。

```powershell
uv run --no-sync python -m scripts.import_market_data --input old.csv --metadata import.json
uv run --no-sync python -m scripts.prefetch_market_data --requirement requirement.json
uv run --no-sync python -m scripts.prepare_strategy_research_data --snapshot <snapshot-id> --requirement requirement.json --output outputs/new-research
uv run --no-sync python -m scripts.benchmark_market_data --symbols 32 --daily-days 2520 --minute-days 20 --output benchmark.json
```

导入元数据包含完整 `DataRequest` 的 `request` 与 `SourceRecord` 的 `source`，显式列出 UTC 边界、标的、频率、价格口径、单位、来源和未知限制。旧日线 CSV 另提供 `daily_date_column:"date"`；字段映射用 `column_map`，旧代码映射用 `instrument_map`。分钟/参考表的无时区时间列必须提供 `timezone`。供应商研究口径需显式声明独立 `basis_id`。原文件字节作为 evidence 保存并记录 SHA-256；导入不会把单位未知的数据标成已验证，也不会单独发布 ready。日历、交易状态、公司行动等表分别导入后，按完整需求预缓存。

研究准备脚本要求已提交快照和显式需求，拒绝覆盖既有输出。`prepare_round2_market_data --audit-legacy` 只校验原 round-two 冻结样本和原来源审核，不复制到腾讯目录、不重新生成旧输出。

旧 `data/raw` 的 61 个 CSV（101,096 行）已核对原始 SHA-256、字符串代码、日期及全部行情值后迁入共享库，旧目录已删除。迁移报告与复核命令见[行情迁移记录](docs/research/2026-10-02/market-migration.md)。历史审计 JSON 和报告仍记录当时的路径，迁移报告把这些路径映射到证据与 Parquet 对象；它们不是当前读取路径。研究工作区管理实验、回测数据包和结果，实际行情始终由共享库提供。

行情缓存不进入 Git：本机共享根 `data/market` 被忽略。Git 保存代码、契约、迁移说明、来源元数据和小型测试夹具；保留已经冻结的研究回测副本以支持重放。若把共享根迁到仓库内的其他位置，须先将该路径加入忽略规则。备份时先停止行情获取与工作台服务，再完整复制共享根的 catalog、manifest、引用对象和 evidence，并一并备份工作台数据库、共享回测对象与结果目录。只复制 Parquet 会丢失来源证据及快照资格。模块保留旧快照对象且不自动回收，长期容量还需计入历史版本、原始响应和备份。

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

真实数据的有限检查：2026-09-29 拉取 ETF `512480` 的 `2024-01-08..12` 五个交易日成功，但默认 `sh000300` 基准出现 `ConnectionError`；最终为 `incomplete / COVERAGE_INCOMPLETE`，未登记数据版本。导入闭环不依赖此次联网成功，不能据此宣称真实数据整体就绪。策略以受信任本地 Python 执行，子进程不是恶意代码沙箱。复现边界、价格模式及 Windows/非 Windows 生命周期差异见使用指南。
