# 网页回测工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付本机网页回测工作台，使用户和 Agent 能修改 Python 策略及参数、可靠地重复回测、查看过程、复现和比较结果。

**Architecture:** 保留 Python/pandas 计算路径，抽出统一数据、策略、订单和账本契约。FastAPI、CLI 与 React 网页共用实验服务；SQLite 管理索引和持久任务，独立子进程从不可变快照执行，每次运行有完整产物。

**Tech Stack:** Python 3.13 作为本机验收环境、pandas/numpy、Pydantic、JSON Schema、FastAPI/Uvicorn、SQLite；React/TypeScript/Vite、CodeMirror、ECharts；pytest、HTTPX、Vitest、Playwright。实施时解析并提交 `uv.lock` 和 `web/package-lock.json`，不在计划中虚构已验证的包版本。

**Spec:** [网页回测工作台设计](../../planning-artifacts/architecture.md)，2026-09-29 已认可的本机部署方向。

**Status:** T1–T10 已实现、分别提交并通过独立任务复核；T11 实现与交付验收已完成（Python 205、前端 50、类型检查/构建、真实浏览器 E2E 1 均通过）。原任务定义与实际完成记录在本文件统一维护，裁决及代价保留于末尾。T11 独立任务复核及全分支独立复核由主代理接续。

## Global Constraints

- “首版聚焦 A 股场内股票型行业 ETF 的日线、多头、现金账户回测。”
- “回测由独立子进程执行，首版默认单任务运行，其余排队。”
- “提交任务时、进入 `queued` 前完成快照与清单的原子发布，再创建可领取的任务。”
- “参数契约声明的功能必须实际生效，不能接受但忽略频率、退出或熔断设置。”
- “首版常规订单只参加指定的下一交易日开盘撮合，未成交余量到期并记录原因，不自动在数日后补买。”
- “策略代码按受信任本地 Python 执行；子进程提供任务隔离和取消能力，并不是恶意代码沙箱。”
- “每阶段通过对应验收后立即独立 commit，不自动 push。”
- 修改旧测试前检查需求有效性；不为旧接口保留兼容层。既有未跟踪 `docs/planning-artifacts/prd.md` 不修改、不暂存。
- 执行时先检查已附工作树并复用合适工作树；需要隔离时使用 managed worktree，功能分支前缀 `codex/`。规划阶段不创建闲置工作树。
- 本机已经发现 Python 3.13/3.14、Node 24.13.0、npm 11.17.0 和 uv。实施用独立 `.venv`，不修改全局 Python。每条命令在所用 checkout 根目录执行，PowerShell 使用 `npm.cmd`。
- 运行数据放 `data/workbench/`，产物放 `outputs/workbench/`，不将数据、令牌或产物提交 Git。页面、日志说明和错误摘要面向用户使用中文，稳定错误码保留英文。

## Review Focus

1. **零交易/不足预热/只有一个绩效日**：有效全现金报告或明确拒绝，首日损益不丢失，无定义指标为 null。归属 T2、T4。
2. **收盘与开盘剧烈跳空、卖单不成交且现金不足**：不读取未来收盘，不预支卖出款，资金分配不依赖输入排序。归属 T3。
3. **数据缺口、前导零代码、除息到付款之间的持仓**：不静默丢行/改代码/少记应收款，不重复计分红。归属 T1、T3。
4. **排队后改源码、辅助模块仍来自工作区、冻结写盘失败**：快照隔离有效，失败不留下可领取任务，复现核对规范化账本。归属 T5、T6。
5. **重复提交、并发保存、取消与完成竞争、刷新后再连**：单次提交可幂等重试，草稿不丢改动，终态不倒退，网页恢复状态。归属 T5–T7、T9。

## 文件职责与任务依赖

| 单元 | 主要文件 | 职责 |
|---|---|---|
| 数据与配置 | `src/quanto/data/{schemas,loaders,cache,fetcher}.py`、`config.py`、`universe.py` | 单一校验入口、价格模式、日历、事件、历史资格 |
| 策略 | `src/quanto/strategies/{base,buy_hold,trend_rotation}.py` | 参数与意图契约、样例、历史视图 |
| 回测 | `src/quanto/backtest/{models,orders,ledger,engine,metrics}.py` | 事件时间、撮合、资金持仓、评价 |
| 研究风险 | `src/quanto/risk/policy.py` | 日风险状态、限额、处置及显式恢复 |
| 版本与运行 | `src/quanto/workbench/{models,store,revisions,snapshots,runs,scheduler,worker,service}.py` | 版本对象、冻结、事务队列、独立执行、共用服务 |
| 查询与导出 | `src/quanto/workbench/results.py`、`src/quanto/reports/backtest_report.py` | 分页、差异、报告与数据导出 |
| 入口 | `src/quanto/api/{app,routes,security}.py`、`cli.py` | 同源 API、会话、机器调用与本机启动 |
| 网页 | `web/src/{api,types,App}.tsx/ts`、`web/src/pages/`、`web/src/components/` | 数据、编辑、运行、分析与比较 |

T1 → T2 → T3 → T4 建立可验证内核；T5 → T6 → T7 建立实验服务；T8 → T9 → T10 完成网页；T11 完成启动与交付。T5 的存储实现可在核心接口固定后独立推进，但共享接口变更必须先同步，不能多 Agent 同时修改同一文件。每个任务是一次可验收提交，不为单纯脚手架创建独立里程碑。

## 共用类型与约定

这些类型由下列所属任务实际实现，后续任务不得另起同义类型或平行计算口径。

- **T1 `DatasetBundle`**：`manifest` 加 `prices, benchmark, calendar, instruments, status, events, eligibility` DataFrame 和按名称索引的 `auxiliary` 表。`price_mode` 为 `raw_cash` 或 `adjusted_research`；`calendar` 包含真实日历范围，不能从截断价格表推断周末。原始字段最少包含现有 OHLCV/amount；代码按字符串读取。
- **T1 `BacktestSpec`**：严格校验的 `start, end, symbols, initial_cash, seed, execution: ExecutionSpec, risk: RiskSpec, metrics: MetricSpec`。配置类型（含 RiskSpec）均在 `config.py` 定义，T3 只实现其执行行为。策略参数由单独 JSON Schema 校验，默认值解析后保存；未知字段拒绝。
- **T2 `Strategy`**：属性 `warmup_bars: int`；`should_evaluate(ctx: StrategyContext) -> bool`；`on_close(ctx: StrategyContext) -> Hold | TargetPortfolio`。修订入口统一为 `strategy.py:create_strategy`，工厂签名 `create_strategy(params: dict) -> Strategy`；相邻 `strategy.json` 声明入口、参数 JSON Schema、必要数据列、辅助源码和研究数据名称。
- **T2 `StrategyContext`**：`asof, history, eligible_symbols, portfolio: PortfolioView, pending_orders, risk_state, is_week_end`；history 是截至 asof 的副本，portfolio/订单为只读值对象。`PortfolioView` 在 `strategies/base.py` 定义，含 `cash, receivables, shares, sellable_shares, market_values, equity, stale_value`；`pending_orders` 为不可变的订单摘要映射元组，不依赖尚未实现的 T3 类型。`TargetPortfolio(weights: dict[str, float], reasons: dict[str, str], diagnostics: dict)` 表示完整目标，`Hold(reason: str)` 表示没有新常规目标。
- **T3 `Order` / `Fill` / `RiskDecision`**：前两者定义于 `backtest/models.py`；Order 含 id、symbol、side、quantity、signal_date、execution_date、status、reason、source；Fill 含 order_id、trade_date、symbol、side、quantity、price、commission、slippage_cost。RiskDecision 定义于 `risk/policy.py`，含 `intent, state, events`。
- **T3 `BacktestResult`**：固定列结构的 `equity, orders, fills, positions, signals, risk_events, account_events` 表和 `completed_through`；无交易时仍有表头。权益列区分 cash、receivables、market_value、equity、stale_value。
- **T3 `RunEvent`**：`seq, phase, asof, completed_days, total_days, level, code, message, payload`，每次完成一个交易日发事件；`emit: Callable[[RunEvent], None]`、`cancelled: Callable[[], bool]` 由运行器注入。
- **T4 `ReportBundle`**：`metrics, metric_definitions, curves, monthly_returns, annual_returns, warnings` 加产物清单；指标 JSON 只允许有限数值或 null。
- **T5 记录类型**：`DatasetVersion, StrategyRevision, ExperimentDraft, FrozenRun, RunRecord, JobRecord, BatchItem` 定义于 `workbench/models.py`。全部 ID 为 UUID 字符串；草稿另含递增 `revision`，payload 包含 strategy_revision_id、dataset_version_id、有效策略参数、BacktestSpec 和样本用途。`RunRecord` 含 job_id、manifest_hash、source、parent_run_id、group_id；`JobRecord.kind` 为 backtest/demo/fetch/import/validate_strategy，统一排队状态，只有 backtest 关联回测产物。BatchItem 包含 experiment_id 与 expected_revision。
- **T7 `Page` / `LogPage`**：`items, next_cursor`；日志另含 `state, completed_through`。`ComparisonResult` 含 `runs, differences, directly_comparable`。输出默认以 ISO 日期/UTC 时间序列化，市场日期解释为 Asia/Shanghai。

数值断言中的资金、价格和阈值均为测试夹具，不能写成账户政策。金额计算使用统一精度和舍入规则，测试以 `pytest.approx(..., abs=1e-8)` 核对；规范化产物哈希的列顺序、行排序、浮点序列化另行固定。

---

### T1：统一数据、配置及历史资格契约

**Files:** 修改 `pyproject.toml`、`src/quanto/data/schemas.py`、`loaders.py`、`cache.py`、`src/quanto/config.py`、`src/quanto/universe.py`、`src/quanto/sample_data.py`；新建 `.python-version`、`uv.lock`、`tests/fixtures/backtest_cases.py`、`tests/unit/test_dataset_contract.py`；更新 `tests/unit/test_universe.py`。

**Interfaces:**
- 产生 `load_dataset(path: Path) -> DatasetBundle`、`validate_dataset(bundle: DatasetBundle) -> DatasetBundle`（`data/loaders.py`）及 `build_eligibility(prices, instruments, rule: dict) -> DataFrame`（`universe.py`）。
- `DatasetManifest` 至少保存来源、演示标记、价格模式、币种、日期/日历覆盖、数量单位、事件覆盖、基准类型和字段语义；instrument 指定 lot_size、settlement_sessions、上市日期与行业分组。缺交易状态不可伪装完整真实数据。
- `ExecutionSpec` 明确 commission_rate、minimum_commission、slippage_rate、max_stale_sessions、next_open 模型；`MetricSpec` 明确 annual_sessions、risk_free_rate、cash_rate、定义版本。费用率/滑点不得为负，初始资金必须正数。
- `RiskSpec` 明确 max_total_weight、max_symbol_weight、group_caps、stress_scenarios（含标的冲击向量和执行成本假设）、max_stress_loss、drawdown_pause、pause_action（禁止新增/减仓/清仓）、pause_target_weight（减仓时必填）、recovery_mode（never/recorded_approval）。未启用的可选度量显式为 null，限额无暗含最优默认值；批准事件纳入带 available_at 的 auxiliary 数据。DatasetManifest 含 universe_rule，加载时根据其定义创建 eligibility；BacktestSpec.symbols 只选已注册数据内的标的。

- [x] **1. 审查旧测试并补充失败用例。** 新测试名为 `test_eligibility_does_not_delete_prices`、`test_dataset_keeps_leading_zero_symbols`、`test_invalid_dataset_is_rejected`、`test_unknown_configuration_is_rejected`。资格测试断言保留全部 5 行行情、前三日资格为 `[False, False, True]`；重复键、非正价格、high<low、缺必需字段、未知配置均拒绝。CSV 中 `000001` 必须保持六位字符串。给出 raw_cash/adjusted_research 两个小数据夹具、完整一周日历和停止于周三的价格切片。
  ```python
  assert bundle.prices.shape[0] == 5
  assert eligible.loc[:2, "eligible"].tolist() == [False, False, True]
  assert loaded.prices.iloc[0]["symbol"] == "000001"
  ```
- [x] **2. 建立本机隔离环境并验证失败。** `.python-version` 指定 3.13；在 pyproject 加实际所需 Pydantic、jsonschema、Parquet 读取依赖并锁定。运行 `uv sync --extra dev`，再运行 `uv run pytest tests/unit/test_dataset_contract.py tests/unit/test_universe.py -q`，确认因新行为未实现而失败，不把缺依赖当业务红测。
- [x] **3. 实现统一入口和配置。** CSV/Parquet 均调用同一校验，拒绝静默删除重复/非法行。明确 suspension 可无行情、unknown gap 拒绝的区别。新增资格输出 mask，不裁掉持仓估值数据；数据表副本避免调用方修改源快照。样例数据输出来源/模式和日历元数据。
- [x] **4. 验证。** 重跑本任务测试全部 PASS，检查旧调用点并在本任务同步到统一加载器；其他业务接口留给其所属任务，不加入旧签名兼容分支。
- [x] **5. 提交。** 只暂存本任务文件，检查 `git diff --cached --check`；commit `refactor: define validated backtest data and configuration contracts`。

### T2：可扩展策略接口和两个可运行样例

**Files:** 新建 `src/quanto/strategies/base.py`、`buy_hold.py`、`trend_rotation.py`、`strategies/buy_hold/{strategy.py,strategy.json}`、`strategies/trend_rotation/{strategy.py,strategy.json}`、`tests/unit/test_strategy_contract.py`、`tests/unit/test_strategy_templates.py`；修改现有 `strategies/__init__.py`，替换旧 `multi_factor_rotation.py` 的调用后移除失去用途的重复实现；按新模板更新 `configs/strategy.yaml`。

**Interfaces:** 消费 T1 数据与配置；实现共用 Strategy 类型。`validate_parameters(schema: dict, params: dict) -> dict` 解析默认值且拒绝未知字段；`validate_intent(intent, known_symbols: set[str]) -> None` 检查权重有限、非负、合计≤1。模板工厂和用户修订入口遵循相同协议。

- [x] **1. 写失败用例。** 测试 `test_strategy_contract_rejects_invalid_input`、`test_buy_hold_emits_once`、`test_trend_retains_and_refills`、`test_context_excludes_future`。验证未知参数、NaN 权重、重复/不存在标的被拒绝；Hold 与空 TargetPortfolio 区别；上下文历史未来不可见。买入持有样例初次给等权目标、之后 Hold；趋势样例按预设条件过滤，再动量排名、保留缓冲、先退出再补位；同分以 symbol 确定性排序。全不合格返回空目标。
  ```python
  assert isinstance(buy_hold.on_close(first_ctx), TargetPortfolio)
  assert isinstance(buy_hold.on_close(later_ctx), Hold)
  assert trend.on_close(no_qualified_ctx).weights == {}
  ```
- [x] **2. 红测。** `uv run pytest tests/unit/test_strategy_contract.py tests/unit/test_strategy_templates.py -q`，确认新接口/行为失败。
- [x] **3. 实现。** 以简单累计收益/动量为初始趋势模板，参数包括 trend_window、momentum_window、entry_rank、retain_rank、evaluation（日/周）；窗口来自参数且 warmup_bars 能完整覆盖。保留范围不能小于入场范围；frequency 不写死在引擎。不足预热时新增信号不产生，若整个绩效期无可评估时点则预检报错。因子研究辅助函数可复用，但不把旧综合权重作为主线默认。
- [x] **4. 验证。** 本任务测试及 `tests/unit/test_factors.py` 中仍有意义的未来扰动/横截面隔离测试 PASS；逐个参数做行为差异或校验测试，禁止“表单有参数、引擎不使用”。
- [x] **5. 提交。** 精确暂存、cached diff 检查；commit `feat: add replaceable strategy contract and research templates`。

### T3：按真实时间推进的订单、账本和日风险

**Files:** 新建 `src/quanto/backtest/models.py`、`orders.py`、`ledger.py`、`src/quanto/risk/policy.py`、`tests/unit/test_order_ledger.py`、`tests/unit/test_risk_policy.py`；重构 `src/quanto/backtest/engine.py`；替换 `tests/integration/test_backtest_no_lookahead.py`，审查更新 `tests/unit/test_position_sizing.py`、`test_market_regime.py` 及对应旧代码，只移除因新路径替换而失效的部分。

**Interfaces:**
- `run_backtest(bundle: DatasetBundle, strategy: Strategy, spec: BacktestSpec, emit: Callable[[RunEvent], None], cancelled: Callable[[], bool]) -> BacktestResult`，纯回测不创建运行目录。
- `PortfolioLedger(initial_cash: float)`：`snapshot(asof) -> PortfolioView`、`apply_event(event) -> None`、`apply_fill(fill) -> None`、`mark(asof, prices, status, max_stale_sessions) -> dict`。
- `create_orders(intent, portfolio, reference_prices, signal_date, execution_date, instruments) -> list[Order]`；`execute_open(orders, open_quotes, status, ledger, execution_spec) -> list[Fill]`，均在 `orders.py`。
- `RiskPolicy(spec: RiskSpec)` 的 `evaluate(ctx: StrategyContext, proposed: Hold | TargetPortfolio) -> RiskDecision` 每日运行；RiskDecision 含允许目标、风险状态和事件。RiskSpec 支持总仓位/单品种/分组合并上限、显式冲击情景与损失限额、回撤触发动作。恢复仅支持不恢复或输入数据中当时已可知的人工批准事件，且仍检查风险条件；不凭经过若干天自动恢复。不支持的旧 VaR/行情分档参数拒绝，不将其错误模型沿用为安全保证。

- [x] **1. 写行为红测。** 测试 `test_next_open_ignores_future_close`、`test_sell_before_buy_is_order_invariant`、`test_suspended_holding_keeps_last_mark`、`test_dividend_receivable_payment_and_split`，覆盖下列数值夹具（lot=1、零滑点、无特别说明则零佣金）。
  ```python
  # D1 close=10，初始1000，目标100%；D2 open=10，改变D2 close和D3行情
  assert original.orders.iloc[0]["quantity"] == changed.orders.iloc[0]["quantity"] == 100
  assert original.fills.iloc[0]["price"] == changed.fills.iloc[0]["price"] == 10
  # 先卖A100份@10，再买B；反排输入后账本不变
  assert final.shares == {"A": 0, "B": 100}
  assert final.cash == 0
  # 100份A最后可靠价10，现金500；确认停牌及失去新增资格
  assert suspended.equity == 1500 and suspended.stale_value == 1000
  # 股息应收不提前变现金：A100份，除息10→9，每份股息1
  assert ex_day.receivables == 100 and ex_day.cash == 0 and ex_day.equity == 1000
  assert pay_day.receivables == 0 and pay_day.cash == 100
  # 随后2:1拆分，股价9→4.5；权益仍1000
  assert split_day.shares["A"] == 200 and split_day.equity == 1000
  ```
  另测空目标清仓/Hold保持；卖单失败不预支款；费用导致买量缩减；收盘风险信号不得同收盘成交；截断于周三不产生虚构周末信号；买入可卖份额及禁买/禁卖状态生效；常规余单到期、空目标撤销挂买、持续风险卖单不重复卖出。
- [x] **2. 红测。** `uv run pytest tests/unit/test_order_ledger.py tests/unit/test_risk_policy.py tests/integration/test_backtest_no_lookahead.py -q`，失败必须指向新账本/时间行为。
- [x] **3. 实现订单、账本与时间循环。** 按设计日序运行，订单数量只使用信号日权益/参考价；开盘卖后买，不足现金按比例和最小单位分配，余量使用确定性规则。分红按登记日持仓固定权益、除息日计应收、支付日入现金；拆分调整数量与成本基础及有效订单。每天按期初可用现金计提显式 cash_rate 的交易日利息，记账户事件，默认演示假设为零。模式为 adjusted_research 时不处理现金分红，禁止与 raw_cash 混淆。未知缺价停止，确认停牌按陈旧上限计价；结束日保留未平仓并标未执行单。
- [x] **4. 实现日风险并验证。** 风险处置先于常规目标；相同实际持仓穿透分组上限，不重复计算账户金额，情景冲击用明确假设。测试回撤暂停、收到过早/不可知批准不恢复、合法批准且风险允许才恢复。重跑本任务测试和 T1/T2 全部 PASS，核对现金+应收+市值=权益。
- [x] **5. 提交。** 精确暂存、cached diff 检查；commit `fix: make backtest execution and portfolio accounting causal`。

### T4：一致绩效、过程产物和独立报告

**Files:** 修改 `src/quanto/backtest/metrics.py`、`src/quanto/reports/backtest_report.py`、`tests/unit/test_metrics.py`、`tests/integration/test_backtest_outputs.py`；新建 `tests/unit/test_report_bundle.py`。

**Interfaces:** `metrics.py` 提供 `compute_metrics(result: BacktestResult, spec: BacktestSpec) -> dict`；`backtest_report.py` 提供 `build_report(result: BacktestResult, bundle: DatasetBundle, spec: BacktestSpec) -> ReportBundle` 和 `write_backtest_outputs(target_dir: Path, result: BacktestResult, report: ReportBundle) -> dict[str, str]`，返回相对文件名及哈希，已存在成功目录拒绝覆盖。它不把 run 标记成功，状态由 T6 发布。

- [x] **1. 写红测。** 测试 `test_first_day_return_uses_initial_cash`、`test_cash_only_metrics_are_defined_or_null`、`test_report_rejects_overwrite`。固定初始现金1000，买90份@10，佣金1%=9，收盘11：现金91，权益1081，总收益0.081。单日绩效也计算首日收益；全现金 total_return=0，零方差 Sharpe/零回撤 Calmar 为 null，空数据明确拒绝。保留净值 `[100,120,90,110]` 的回撤0.25用例。基准曲线首值归一化、种类/区间明确，月度和年度复利与总收益相容。
  ```python
  assert metrics["total_return"] == pytest.approx(0.081)
  assert cash_only_metrics["sharpe"] is None
  assert json.loads(json.dumps(metrics, allow_nan=False))["total_return"] == pytest.approx(0.081)
  ```
- [x] **2. 红测。** `uv run pytest tests/unit/test_metrics.py tests/unit/test_report_bundle.py tests/integration/test_backtest_outputs.py -q`。
- [x] **3. 实现。** 收益首项以 initial_cash 为基数，年化收益固定为 `(final/initial) ** (annual_sessions / observed_sessions) - 1`，标明按观测交易日年化；不足两个绩效日时年化/波动/Sharpe 为 null，总收益仍报告。风险利率/现金利率采用相同交易日口径。保存 orders/fills/positions/signals/risk/account events、完整日净值与指标定义。成交笔数与往返数分开，FIFO匹配平仓成本和持有期，未平仓单列。HTML 静态报告内嵌曲线数据与 SVG，不依赖外网 CDN，文本转义；CSV/JSON 保留元数据和模式标记。
- [x] **4. 验证。** 本任务及前序测试 PASS；无交易表有固定列；相同输入产物规范化哈希一致；输出覆盖拒绝；两个模板都能生成独立可读报告。
- [x] **5. 提交。** 精确暂存、cached diff 检查；commit `feat: add auditable backtest results and comparable metrics`。

### T5：数据/策略修订、草稿和入队前快照

**Files:** 新建 `src/quanto/workbench/__init__.py`、`models.py`、`store.py`、`revisions.py`、`snapshots.py`、`runs.py`、`tests/integration/test_workbench_revisions.py`、`test_snapshot_submission.py`；修改 `.gitignore`。

**Interfaces:**
- `WorkbenchStore(root: Path)` 管理 SQLite/schema_version；`register_dataset(bundle_dir: Path) -> DatasetVersion`、`register_strategy(files: dict[str, bytes], manifest: dict) -> StrategyRevision`、`save_experiment(experiment_id: str | None, expected_revision: int | None, payload: dict) -> ExperimentDraft`。
- `SnapshotBuilder.freeze(experiment: ExperimentDraft, source: Literal['web','agent'], parent_run_id: str | None) -> FrozenRun`，FrozenRun 含 run_id、manifest_path、manifest_hash。
- `RunService.submit(experiment_id: str, expected_revision: int, idempotency_key: str, source: str) -> RunRecord`；同一 key 同一请求返回同一记录，不同请求返回冲突。`submit_batch(items: list[BatchItem], idempotency_key: str, source: str) -> list[RunRecord]` 预检各草稿修订后原子入队整组，首版最多100项，超限拒绝而非截断。

- [x] **1. 写红测。** 测试 `test_draft_revision_conflict_keeps_first_save`、`test_submission_is_idempotent`、`test_failed_freeze_never_queues`、`test_snapshot_ignores_later_workspace_edits`。两次相同 expected_revision 保存只有一次成功；同幂等键只生成一个 run_id。冻结写盘失败无 queued 任务。排队后改策略、辅助文件、引擎和源数据，manifest/账本仍读原版本。拒绝源码 `../`、绝对路径、符号链接逃逸和缺失声明文件；密码/令牌不进入快照。
  ```python
  assert service.submit(eid, rev, "same", "web").run_id == service.submit(eid, rev, "same", "web").run_id
  assert queued_count_after_freeze_failure == 0
  assert stored_manifest_hash == manifest_hash_after_workspace_edit
  ```
- [x] **2. 红测。** `uv run pytest tests/integration/test_workbench_revisions.py tests/integration/test_snapshot_submission.py -q`。
- [x] **3. 实现。** SQLite 存稳定对象、草稿revision、jobs/runs、幂等键；单写事务和唯一约束实现冲突检测。数据/源码用临时目录写入并校验后原子发布，提交事务最后才暴露 queued；数据库失败的孤立不可变目录不被调度，按清单识别，不随意清理其他文件。源码复制前后检查一致性，编辑中的不稳定输入拒绝提交而不冻结混合版本。全量冻结项目 Python 执行源码及声明策略文件，记录 Git 状态、依赖/解释器版本、种子和有效配置。无未来自动安装环境；缺依赖/哈希变化阻止精确重跑，非精确模式显式标注。
- [x] **4. 验证。** 本任务测试 PASS；两个不同策略与数据修订能共存，取消/失败快照不被覆盖；用户现有数据/文件不被移动或改写。`data/workbench/`、`outputs/workbench/`、前端 node_modules/build、令牌均被适当忽略。
- [x] **5. 提交。** 精确暂存、cached diff 检查；commit `feat: persist experiment revisions and immutable run snapshots`。

### T6：持久队列、独立进程、进度和取消

**Files:** 新建 `src/quanto/workbench/scheduler.py`、`worker.py`、`tests/integration/test_run_lifecycle.py`、`test_worker_snapshot.py`；扩展 `runs.py`、`store.py`。

**Interfaces:** `Scheduler(store: WorkbenchStore, max_workers: int = 1).start()/stop()`；`RunService.cancel(run_id: str) -> RunRecord`；`worker_main(job_path: Path, control_dir: Path) -> int`。job_path 对应被冻结的 kind/input 描述；backtest 引用 FrozenRun 清单，T7 扩展固定的其他 kind 处理器。worker 按冻结的源码路径启动，`emit` 追加 NDJSON 和逐日部分产物，`cancelled` 读取取消标记。

- [x] **1. 写红测。** 测试 `test_job_claim_is_exclusive`、`test_cancel_and_completion_cannot_overwrite_terminal_state`、`test_restart_marks_abandoned_job_interrupted`、`test_worker_imports_frozen_modules`。同一 job 只能被一个调度器领取；queued 可直接取消。running→cancel_requested 后只在确认停进程才 cancelled；终态不被迟到完成消息覆盖。模拟进程卡住、异常退出、服务重启以及快照模块与工作区同名，断言快照被使用、遗留运行 interrupted、排队保留、无孤儿进程或伪成功。
  ```python
  assert len(successful_claims_for_same_job) == 1
  assert cancelled_queued_job_was_started is False
  assert restarted.running_job.state == "interrupted"
  ```
- [x] **2. 红测。** `uv run pytest tests/integration/test_run_lifecycle.py tests/integration/test_worker_snapshot.py -q`。
- [x] **3. 实现。** 单服务锁+事务领取，记录进程 PID、启动标识和心跳，不能仅凭复用 PID 杀进程；Windows 子进程隐藏窗口。通过专用 bootstrap 从快照载入 quanto 和策略模块，排除工作区/可编辑安装路径覆盖；已安装第三方依赖按锁文件校验。在导入用户策略之前固定 Python/numpy 随机种子。测试用短超时注入，不用长 sleep 验证竞态。
- [x] **4. 完成生命周期。** 取消协作超时后终止所属工作进程，服务退出回收；静默长计算用独立心跳区分卡住和正常初始化。必需文件哈希/表结构校验通过且产物原子发布后才 succeeded，失败保留部分产物。重跑稳定快照比较规范化账本/产物哈希；运行元数据不纳入确定性比较。重跑本任务测试及 T5 全部 PASS。
- [x] **5. 提交。** 精确暂存、cached diff 检查；commit `feat: run reproducible backtests with progress and cancellation`。

### T7：数据任务、统一应用 API、Agent CLI 和查询

**Files:** 新建 `src/quanto/workbench/service.py`、`results.py`、`data_jobs.py`、`src/quanto/api/{__init__,app,routes,security}.py`、`tests/integration/test_workbench_api.py`、`test_api_cli_equivalence.py`、`test_data_jobs.py`、`test_comparison.py`；修改 `cli.py`、`data/fetcher.py`、`tests/unit/test_fetcher_cache.py`、`pyproject.toml`、`uv.lock`。

**Interfaces:**
- `create_app(root: Path, frontend_dir: Path | None = None) -> FastAPI` 与 `WorkbenchService(root: Path)`；HTTP 路由只做校验/序列化，服务方法调用 T1–T6。API 前缀 `/api`；错误统一 `{code, message, details}`，HTTP 409 为修订/幂等冲突，422 为参数问题。
- 资源 `/datasets`、`/strategies`、`/experiments` 提供列表、按 ID 获取、新建；草稿更新 `PUT /experiments/{id}` 含 expected_revision。`POST /runs`、`POST /runs/batch`、`GET /runs/{id}`、`POST /runs/{id}/cancel`、`POST /runs/{id}/rerun`；日志 `/runs/{id}/logs?cursor=...`，表格 `/runs/{id}/tables/{name}?cursor=...&limit=...`，摘要 `/runs/{id}/results`，导出 `/runs/{id}/exports/{format}`，比较 `POST /comparisons`。
- `POST /jobs/data` 提交 demo/fetch/import，`GET /jobs/{id}` 和日志/取消接口共用生命周期。`POST /uploads` 接收 multipart 数据文件并得到受控 upload_id，再交任务；不让网页传主机路径。语法检查只用 AST，`POST /jobs/strategy-validation` 在独立 job 动态验证策略，不在 API 进程 import 用户代码。`data_jobs.py` 提供 `execute_data_job(kind: str, payload: dict, emit: Callable, cancelled: Callable) -> dict`，由 T6 固定处理器调用。
- `compare_runs(run_ids: list[str]) -> ComparisonResult` 在 results.py；`WorkbenchClient(base_url: str, token: str)` 位于 cli.py，所有管理命令调用同一 HTTP API，JSON 输出含 run_id、state、detail_url。已有离线命令在迁移时明确替换/更新文档，不留第二套账本。

- [x] **1. 写红测。** 测试 `test_cli_reads_same_run_as_api`、`test_api_rejects_conflicts_and_invalid_parameters`、`test_comparison_reports_all_material_differences`、`test_incomplete_fetch_is_not_registered_as_complete`。API/CLI 查询同一运行返回相同 run_id、指标和可打开链接；分别提交相同快照时 ID 不同但规范化账本一致。未知 ID 返回404；非法参数422；草稿冲突409；同key不同请求409；批量100项可接受、101项拒绝。基准/数据/区间/成本/价格模式/指标版本任一不同都出现在 differences；失败/部分运行不参与完成排名。HTTPX TestClient 测同源来源和写令牌校验，读取私有结果也不得允许任意外站跨域读取。
- [x] **2. 红测。** 添加 FastAPI/Uvicorn、HTTPX 和上传解析所需依赖并更新锁，运行 `uv run pytest tests/integration/test_workbench_api.py tests/integration/test_api_cli_equivalence.py tests/integration/test_data_jobs.py tests/integration/test_comparison.py -q`。
- [x] **3. 实现 API/CLI 与数据任务。** 采集失败返回实际覆盖、失败标的和不完整状态；缓存只按显式版本选择，不静默回退当完整。fake provider 测缓存覆盖含中间缺口，网络调用不进入常规单元测试。真实 qfq 数据注册为 adjusted_research，raw_cash 需附事件/状态；导入两模式可在页面预检。字段语义检查防指数成交额冒充市场总额。数据任务取消/重启使用 T6 job 协议，但没有回测 metrics。
- [x] **4. 实现查询并验证。** 表名/导出格式白名单，分页和日志游标稳定，文本日志不执行 HTML；报告查询记录测试区间是否被查看。下载中文文件名可用。JSON CLI 仅 stdout 输出 JSON、日志走 stderr；命令 `quanto serve|datasets|strategies|experiments|runs|compare` 有帮助及明确退出码。运行本任务及更新的 fetcher 测试全部 PASS。
- [x] **5. 提交。** 精确暂存、cached diff 检查；commit `feat: expose a shared workbench API and agent CLI`。

### T8：网页数据管理与策略实验编辑

**Files:** 新建 `web/package.json`、`package-lock.json`、`index.html`、`vite.config.ts`、`tsconfig.json`、`src/main.tsx`、`App.tsx`、`api.ts`、`types.ts`、`styles.css`、`pages/DatasetsPage.tsx`、`pages/ExperimentsPage.tsx`、`components/StrategyEditor.tsx`、`components/ParameterForm.tsx`、`tests/editor.test.tsx`、`tests/datasets.test.tsx`；API 形状来自 T7，不另算指标。

**Interfaces:** 导航路径 `/datasets`、`/experiments`、`/runs`、`/runs/:id`、`/compare`。`api.ts` 封装类型化 fetch、会话与统一错误；StrategyEditor 编辑 manifest 与多源码文件，ParameterForm 消费 JSON Schema，不认识的复杂结构回退到明确的 JSON 编辑而非丢字段。

- [x] **1. 写前端失败用例并建立必要项目配置。** Vitest/Testing Library 测试 `preserves unsaved edits on revision conflict`、`renders parameters from schema`、`shows dataset mode and coverage errors`，验证演示/真实和两价格模式标记，数据覆盖不足的错误，修改文件和参数后保存新修订；409保存冲突保留本地编辑并展示服务端修订，不静默覆盖。AST错误展示文件/行号。新参数从 schema 自动出现，未保存修改离开页面需明确处理。
- [x] **2. 红测。** 安装本任务声明依赖并锁版本，`npm.cmd --prefix web run test -- --run` 确认页面行为未实现导致失败，建立 `typecheck`、`build` 脚本。
- [x] **3. 实现。** 中文工作台左导航、清晰的当前数据/策略修订和实验配置；CodeMirror 编辑器和文件列表；schema 表单与 JSON 同一状态。数据页可生成演示、上传导入和发起获取任务，展示 job 进度及实际结果；策略参数、执行费用、风险假设、种子、预热、样本用途都可编辑，提交前冻结修订。
- [x] **4. 验证。** `npm.cmd --prefix web run test -- --run`、`npm.cmd --prefix web run typecheck`、`npm.cmd --prefix web run build` 全部通过；页面空态、错误态和成功态都指向真实 API 行为，不用静态假列表验收。
- [x] **5. 提交。** 精确暂存、cached diff 检查；commit `feat: add browser dataset and strategy editing workflows`。

### T9：运行列表、实时过程和取消

**Files:** 新建 `web/src/pages/RunsPage.tsx`、`RunDetailPage.tsx`、`components/RunProgress.tsx`、`components/EventTimeline.tsx`、`web/tests/runs.test.tsx`；扩展 `App.tsx`、`api.ts`、`types.ts`。

**Interfaces:** 消费 T7 jobs/runs/logs/tables API；轮询默认1秒，结束后停止，离开页面释放请求；每个运行保存独立游标，不将日志重复追加。RunDetailPage 的同一 URL 在刷新后从 API 恢复。

- [x] **1. 写红测。** 测试 `retries submission with the same key`、`keeps cancellation pending until acknowledged`、`restores run state after reload`。双击运行复用同一幂等键；网络超时重试不新增任务，用户明确再次运行生成新key。展示 queued/running/cancel_requested/四种终态；刷新恢复进度和日志。取消请求后按钮显示等待而非提前声称已停；未知总量仅显示阶段。
  ```typescript
  expect(submitRequests[0].key).toBe(submitRequests[1].key);
  expect(screen.getByText('正在取消')).toBeVisible();
  expect(screen.queryByText('已取消')).toBeNull();
  ```
- [x] **2. 红测。** `npm.cmd --prefix web run test -- --run tests/runs.test.tsx`。
- [x] **3. 实现。** 单次及有限参数列表提交、运行筛选/分组；详情含模拟日期、阶段、已完成日数、日志、错误和输入快照。按日/标的联查信号、风控、订单和成交；无成交有原因。部分净值标“运行中/部分结果”，失败/取消允许看部分产物但不出最终绩效排名。
- [x] **4. 验证。** 本任务测试、typecheck/build PASS；模拟断线恢复后不重复事件、不倒退已完成日期；取消失败或任务已终态显示服务端真实状态。
- [x] **5. 提交。** 精确暂存、cached diff 检查；commit `feat: show persistent run progress and trading decisions`。

### T10：图表、实验比较和完整报告导出

**Files:** 新建 `web/src/pages/ComparePage.tsx`、`components/ResultCharts.tsx`、`components/ResultTables.tsx`、`components/RunDifferences.tsx`、`web/tests/results.test.tsx`、`web/tests/compare.test.tsx`；扩展 `RunDetailPage.tsx`、`api.ts`；加入 ECharts 并更新 lock。

**Interfaces:** 所有绩效使用 T4/T7 返回的指标/定义、曲线、月年收益。前端仅做显示与可视范围降采样；API 完整数据用于指标与导出。ComparePage 接收 run IDs，呈现 ComparisonResult，不自行裁切日期重算。

- [x] **1. 写红测。** 测试 `renders cash-only results and undefined ratios`、`does not rank incompatible runs`、`links fills to their decisions`。零交易有全现金曲线/空明细，null指标显示不可用；两个匹配运行叠加净值与回撤，不同口径醒目展示且不自动排“最好”。按交易点击可追到信号与成本，数据模式始终可见；CSV/JSON/HTML 导出含模式和数据版本。
- [x] **2. 红测。** `npm.cmd --prefix web run test -- --run tests/results.test.tsx tests/compare.test.tsx`。
- [x] **3. 实现。** 净值/基准、回撤、月度热力图、年度收益、仓位/现金、换手/费用和明细分页；统一日期提示、图例及键盘可达控件，异常状态以文字解释。比较同时列代码文件差异、参数与环境变化；代码/日志显示必须转义。下载调用已验证的后端产物。
- [x] **4. 验证。** 本任务测试及全套前端 test/typecheck/build PASS；图表缩放与分页不改变后端指标；独立 HTML 离线打开正常，不依赖运行中的服务。
- [x] **5. 提交。** 精确暂存、cached diff 检查；commit `feat: add visual backtest analysis and experiment comparison`。

### T11：本机启动、浏览器闭环与交付

**Files:** 新建 `scripts/start-workbench.ps1`、`web/playwright.config.ts`、`web/e2e/workbench.spec.ts`、`tests/integration/test_serve_static.py`、`docs/05_回测工作台使用指南.md`；修改 `api/app.py`、`cli.py`、`pyproject.toml`、`README.md`、`docs/03_量化系统架构.md`、`.gitignore`。

**Interfaces:** `quanto serve --host 127.0.0.1 --port 8765 --workspace <path>` 启动同源前端/API与一个调度器，首版拒绝非回环地址；workspace 默认当前仓下 `data/workbench`，产物位置通过该工作区配置指向 `outputs/workbench`。CLI 将当前 checkout 的 `web/dist` 显式传入 create_app 的 frontend_dir；脚本检查依赖及前端构建是否匹配，缺构建给出明确构建命令，不偷偷开第二个服务或占用端口重试。首次启动提供演示模板，点击生成演示数据后完成引导。

- [x] **1. 写集成失败用例。** 测试 `test_spa_fallback_does_not_shadow_api`、`test_serve_rejects_missing_build` 和 E2E `edits runs compares and exports a backtest`。SPA深链接直接打开可用，`/api/不存在` 返回 JSON404而非HTML，缺前端构建/端口占用报错。Playwright 建独立临时 workspace 和服务，按真实流程生成数据→编辑代码与参数→运行→看订单→复制修改→比较→下载；不使用假后端或手填运行状态。
- [x] **2. 运行红测。** `uv run pytest tests/integration/test_serve_static.py -q`；按 Playwright 官方 webServer 方式启动测试服务，`npm.cmd --prefix web run test:e2e` 确认闭环缺口，而非以浏览器未安装作为红测。仅测试环境安装所需浏览器。
- [x] **3. 完成启动和使用指南。** 明确 `uv sync --extra dev --extra web --extra data`、`npm.cmd --prefix web ci`、前端 build 及一键启动命令；以当前 checkout 加锁定依赖作为首版交付方式，校验静态构建版本。记录 API/CLI JSON示例、导入契约、价格模式差异、策略修订规则和失败/取消恢复步骤。真实数据下载失败不阻止演示数据闭环，但要报告真实接入未验证的范围。
- [x] **4. 最终验收。** `uv run pytest -q`、前端全套 test/typecheck/build、`npm.cmd --prefix web run test:e2e` 均 PASS；浏览器人工核对桌面布局、中文、图表、错误状态及刷新行为。运行快照重跑一致性、取消卡住策略、服务重启和持仓缺价/除息数值用例全部有证据。记录实际测试数和已知数据限制，不以回测收益判断软件正确性。
- [x] **5. 提交。** 精确暂存、cached diff 检查；commit `feat: deliver the local browser backtesting workbench`；检查 status 保留用户文件，提供启动入口、演示运行链接与结果，不自动 push。

## 旧测试取舍与设计覆盖

| 现有测试/设计要求 | 处理与归属 |
|---|---|
| `test_universe.py`、两套数据加载器 | T1 保留历史资格意图，改验证资格mask及估值数据不被删除 |
| `test_factors.py` | T2 保留有效的时间/横截面测试；不强制新模板依赖旧因子权重 |
| `test_position_sizing.py`、`test_market_regime.py` | T3 按新日风险契约检查行为；去掉未实现参数和无数据口径支持的旧假设 |
| `test_backtest_no_lookahead.py` | T3 替换仅日期比较的测试，增加未来扰动、开盘数量与周中截断 |
| `test_metrics.py`、`test_backtest_outputs.py` | T4 验证初始现金基数、无交易、两模式、结构化产物及报告 |
| `test_fetcher_cache.py` | T7 保留命中缓存；不完整回退必须显示真实覆盖和失败状态，不静默注册完整数据 |
| 设计 §1–5：网页/Agent与策略自由度 | T1–T2、T7–T9；两个样例和用户策略不改引擎 |
| 设计 §6–7：时间、账本、数据模式 | T1、T3–T4；分红应收与支付不混淆 |
| 设计 §8–9：快照、任务、取消、中断 | T5–T6、T9；所有代码快照来自入队前 |
| 设计 §10–11：接口、指标、比较、实验纪律 | T4、T7、T10；全部尝试和查看测试区间的记录可查 |
| 设计 §12–13：验收和交付 | 每任务对应测试及 T11 浏览器真实闭环；阶段验证后分别 commit |

## 计划自检与执行交接

2026-09-29 计划自检确认任务覆盖设计各节，共用类型和字段名一致；随后按 T1–T11 顺序实施。当前实现、锁定依赖和实际验证由下方完成记录说明，不再以最初的规划阶段状态描述现行产品。

已采用子 Agent 分任务实现和独立任务复核，核心数据→策略→账本→指标接口先顺序稳定，再推进服务与网页。模型和推理强度按各任务风险单独选择；各任务复核与修复范围记录在完成表中，最终全分支审查另行进行。

真实数据源的具体不完整结果、依赖警告处理和跨平台限制均保留在交付记录及使用指南中；没有以这些限制省略演示/导入的真实浏览器闭环。

技术核对来源：[Vite 环境要求](https://vite.dev/guide/)、[FastAPI 测试接口](https://fastapi.tiangolo.com/tutorial/testing/)、[Playwright 启动测试服务](https://playwright.dev/docs/test-webserver)、[ECharts 许可证](https://github.com/apache/echarts/blob/master/LICENSE)。实施时以锁定的实际版本验证这些接口。

## Completed task evidence

T11 最终交付证据（2026-09-29，实施目录 `C:\Users\georg\.codex\worktrees\web-backtest-workbench\quanto`）：

- `uv run pytest -q`：205 passed，100.05 秒，无 TestClient 警告。涵盖快照重跑与 API/CLI 等价、取消卡住策略、重启、Windows 进程所有权、缺价/除息会计行为。
- `npm.cmd --prefix web run test -- --run`：6 文件、50 passed；包含此前 49 项行为及新增中文枚举检查。原订单更新测试按现行中文显示核对 title 原码与标签，仍验证按 ID 替换。
- `npm.cmd --prefix web run typecheck`、`npm.cmd --prefix web run build`：PASS。`$env:PLAYWRIGHT_CHANNEL='chrome'` 后 `npm.cmd --prefix web run test:e2e`：1 passed，15.7 秒；Playwright webServer 自建临时工作区，在真实后端完成生成演示→修改源码/参数→运行→订单→克隆→比较→下载，并验证深链接刷新和中文错误。
- 启动集成初始 RED 4 项，工作区产物配置 RED 1 项；修复后启动/配置 6 项 PASS。真实浏览器初始 RED 为静态 401；SVG 图例/日期重叠几何断言 RED→GREEN，订单中文固定枚举 RED→GREEN。控制器已查看最终桌面图表与错误截图。
- 一键 PowerShell 脚本实际启动 `127.0.0.1:8767` 并返回深链接 200，烟测进程已回收。默认正式启动为 `.\scripts\start-workbench.ps1`，地址 `http://127.0.0.1:8765`；`data/workbench/workspace.json` 记录默认 `outputs/workbench`，已有任务产物路径不变。
- 100 个有效项的真实 API 冻结/入队、同键重试、101 项拒绝均有永久回归，最终行数 `(runs,jobs,idempotency)=(100,100,1)`；没有执行 100 个策略。先前独立并发观察仅看到 0 或 100 行，仍以事务原子性为实现依据。
- `httpx2>=2,<3` 仅加入 dev，锁定 2.13.1；Starlette 1.7 使用其支持的 TestClient，CLI 保留 httpx 0.28.1。未屏蔽警告、未宽泛升级后端依赖。
- 延迟 ECharts 块仍为 623.28 kB / gzip 211.37 kB，入口 JS 481.31 kB；Vite 提示未屏蔽、阈值未抬高。实际冷页面首次滚动到 SVG 图表约 222 ms（本机 Chrome，非跨设备性能保证）。
- 真实来源有限检查：ETF 512480 的 2024-01-08..12 五日成功，默认 sh000300 基准 ConnectionError；最终 incomplete/COVERAGE_INCOMPLETE，未登记版本，未通过替换基准制造成功。演示和导入闭环独立验收，不宣称真实来源全面就绪。
- 精确复现哈希覆盖冻结输入/源码字节；全部已安装发行包名/版本保守比对，即使无关包变更也可能阻止重跑。第三方安装文件不逐个哈希，动态/外部依赖不能自动完整发现。策略为受信任 Python，不是恶意代码沙箱。
- Windows Job Object 已验证突退回收；分配失败在执行前报错。非 Windows 两个根进程都消失后的任意辅助进程遗留窗口仍存在，未宣称跨平台同等保证。详细时序假设与例子见使用指南。
- 原工作区 `D:/Projects/quanto/docs/planning-artifacts/prd.md` 仍未跟踪且未修改；SHA-256 `85f6eade5d6f75a844e847bf6636d1311d172e92450b207d3b102258b54d48e1`。全部修改仅在现有 managed worktree，本阶段只本地提交，不 push/merge。

T11 提交主题：`feat: deliver the local browser backtesting workbench`。以下为此前控制器记录的独立任务完成范围；T11 已经控制器独立复核；全分支审查提出的最终整合修正及其独立复审记录见下文。

- Task 1: complete (commits e6de77f..6ac61f0, review clean)
- Task 2: complete (commits 6ac61f0..bd05489, review clean)
- Task 3: complete (commits bd05489..829343d, review clean)
- Task 4: complete (commits829343d..f427657, review clean). Full suite111 PASS.
- Task 5: complete (commitsf427657..ba8b811, review clean); full suite136 PASS.
- Task 6: complete (commitsba8b811..1a52e39, review clean).
- Task 7: complete (commits1a52e39..f431bef, review clean).
- Task 8: complete (commitsf431bef..8680d17, review clean).
- Task 9: complete (commits8680d17..27fe66e, review clean); fullrunlist presentation acceptance remains explicitT10 gate.
- Task 10: complete (commits27fe66e..bc144b4, review clean); architecture3.3 runlist fields/filter gate closed.

## Controller rulings and tradeoffs (preserved verbatim)

Ruling: Adapt task extraction to the plan’s T1–T11 headings using a local Python extractor, because the supplied task-brief script recognizes only Task N headings; cost if wrong is a missing brief section, checked against the original task text.

Ruling: Task-local contract migrations may temporarily leave legacy engine or CLI tests failing until their explicitly assigned replacement tasks T3/T4/T7. Do not preserve obsolete compatibility solely to keep them green; report every remaining failure and require a green full suite before delivery. Cost if wrong is delayed discovery of integration failures.

Ruling: Daily raw_cash dividend events require record_date < ex_date <= pay_date; freeze entitlement after record-date fills at close, then post receivable before ex-date open. Starting cash accounts have zero entitlement before their start. This removes ambiguous same-day ordering; cost if wrong is rejecting an import needing a richer intraday event model.

Ruling: Cash interest accrues on available cash after start-of-day event postings and before open fills, with one close posting; same-day sale proceeds earn from the next session. This fixes the otherwise unspecified interest cut-off; cost if wrong is a different cash-interest assumption requiring a new experiment.

Ruling: Splits apply before effective-date open to holdings, unsettled lots and live orders, preserving basis; fractional results unsupported by the daily cash model are rejected explicitly. Cost if wrong is rejecting data needing cash-in-lieu support rather than silently mis-accounting it.

Ruling: Retire the obsolete run-backtest CLI path now while preserving sample/fetch commands, then T7 supplies the approved API-backed CLI. Restoring its deleted row-filter contract would violate the design. Cost if wrong is temporary unavailability of the legacy run command during this multi-stage migration; the final CLI is delivered in T7/T11.

Ruling: A valid recorded approval for the current pause episode resets only the risk drawdown reference at that decision close after active exposure/stress limits pass; historical performance drawdown is unchanged. Consume each approval once, require as-of-known timing and a fresh approval after another pause. This allows a liquidated cash account to resume without erasing losses; cost if wrong is a different restart-policy assumption requiring a new experiment.

Ruling: Generic preflight validates historical warmup opportunity only. Arbitrary should_evaluate(ctx) may depend on live portfolio/risk state, so its schedule cannot be truthfully inferred from an optional evaluation attribute or a synthetic context. The engine calls the protocol at actual closes and rejects a run with no evaluable post-warmup session; zero trades after valid evaluations remains a valid cash report. Cost if wrong is late rather than pre-queue rejection for custom schedules; it preserves strategy freedom and avoids false preflight claims.

Ruling: Final exits may sell the complete whole-share odd-lot balance created by splits; partial sells obey trading lot multiples. Fractional-share events without cash-in-lieu remain rejected. This prevents silent stranded holdings; cost if wrong is a more permissive final-exit assumption than a particular instrument allows, disclosed as a model rule.

Ruling: Complete round trips count per-symbol flat-to-long-to-flat episodes; FIFO matching fragments are separate realized-trade rows and do not inflate that count. Split events preserve the episode. This gives the complete-round-trip requirement a stable meaning; cost if wrong is a different trade-count convention, disclosed in metric definitions.

Ruling: Under architecture sections6-7, quote-only qfq data may use explicitly disclosed synthetic trading/lot/settlement assumptions in adjusted_research when its calendar and quote/benchmark grid are complete. These are model assumptions, never verified real tradability; missing quote sessions are still unresolved unless confirmed suspended, and raw_cash retains actual event/status requirements. Cost if wrong is a simplified execution assumption affecting results, always disclosed before running and in reports.

Ruling: Preserve arbitrary integer BacktestSpec.seed; Python random uses the full integer while NumPy and PYTHONHASHSEED receive seed modulo 2**32, with derived seeds recorded in runtime metadata. Their native ranges differ from the existing accepted contract; cost if wrong is distinct integers colliding for NumPy/hash randomness, explicitly visible and reproducible.


## 最终整合修正（独立复审待完成）

I1–I6 修正完整配置比较、原始/批准信号分离、权威演示披露、延迟保存防覆盖、枚举类型及历史冻结运行分支；M1/M2 修正指标单位/精度与中文比较标签。针对每个触发条件新增回归，旧通过结果不作为新增场景覆盖。最终验证（2026-09-30）如下；仅作者自检，独立复审尚未完成。

- `uv run pytest -q`：220 passed，111.38 秒，exit 0；新增 15 项配置、信号、披露、历史分支、枚举验证及旧/新报告严格读取回归。历史 schema 1 继续读取/导出；新报告 schema 2，未知版本拒绝。旧信号测试改为分别验证原始和批准权重。
- `npm.cmd --prefix web run test -- --run`：61 passed / 6 文件，exit 0；新增延迟保存、枚举类型、原始/批准追踪、演示标记、历史草稿深链接和 CodeMirror 文件切换回归。文件切换 RED 实际显示 Python 缓冲区留在 JSON 标签；按文件绑定编辑器后 GREEN。
- `npm.cmd --prefix web run typecheck` 与 `npm.cmd --prefix web run build`：exit 0。ECharts 623.28 kB / gzip 211.37 kB，实验编辑页 500.27 kB 左右，保留体积警告，不拆包或抬阈值。
- `$env:PLAYWRIGHT_CHANNEL='chrome'; npm.cmd --prefix web run test:e2e`：1 passed，16.9 秒，exit 0；真实创建/执行两次运行，修改原草稿后再从历史运行分支仍得到旧权重，验证来源、演示披露、信号与批准目标、比较和 HTML。首次图表 208 ms；Node 颜色环境警告仍为非阻塞。作者查看最终桌面截图。
- 原预览两份 schema 1 报告实际通过新服务结果/表/CSV/JSON/HTML 读取；154 个既有冻结输入/最终产物文件在预览重启前记录哈希，禁止改写。原工作区 PRD SHA-256 仍为 `85f6eade5d6f75a844e847bf6636d1311d172e92450b207d3b102258b54d48e1`。
Ruling: The first release does not add a generic StrategyContext query API for arbitrary auxiliary research tables. Declared research-file freezing remains a reproducibility boundary, not a promise of general point-in-time data access; the approved context and explicit later breadth-data extension support this scope. Cost if wrong is that strategies needing those extra research tables require a later context/data-access extension before their research can run reliably.


### 最终局部保护修正

独立复审确认 I1–I6/M1/M2 修正，另复现当前草稿克隆重新挂载源码编辑器导致未保存源码丢失。现在源码未保存时禁用该克隆操作，显示先保存新修订或切换草稿并确认放弃修改的指引；仅实验配置未保存时仍可克隆。历史运行克隆继续使用冻结源码。

Ruling: Block current-draft cloning while strategy source has unsaved edits, with explicit save/discard guidance, rather than remounting and silently losing the source buffer. Historical-run cloning still uses frozen source. Cost if wrong is one extra save/discard step before cloning a locally edited strategy, preferable to losing work.

持久预览替换被 automatic approval 以 `blocked by policy` 拒绝，未执行停止或启动，也未创建两次新预览运行。原 PID 4020 仍使用旧后端；旧日志/控制仍在 SDD scratch，不能清理。已通过的隔离 Chrome 验收不替代持久预览重启。此保护修正不重试这些操作。

局部修正验证：真实 ExperimentsPage + CodeMirror 回归先 RED（克隆后源码注释丢失），再 GREEN；`npm.cmd --prefix web run test -- --run tests/editor.test.tsx` 为 22 passed / exit 0。`npm.cmd --prefix web run typecheck`、`npm.cmd --prefix web run build` 均 exit 0；新实验编辑块 500.39 kB / gzip 168.63 kB，ECharts 623.28 kB / gzip 211.37 kB，保留警告。本次仅前端与文档，按控制器要求不重复 Python 220 或预览操作。
