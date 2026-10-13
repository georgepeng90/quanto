# 个股量价训练 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通生产注册的沪深主板个股量价 PPO 数据导入、训练、续训和回测，正确记录交易税费。

**Architecture:** 复用现有数据快照、身份绑定、日线账户、PPO 和模型服务。新增股票数据规则和独立交易/学习定义；股票税费来自冻结辅助表，ETF 当前定义保持真实身份不变。完成后执行独立的选股模块计划。

**Tech Stack:** Python 3.13、Pydantic 2.12+、pandas、SQLite、现有 PyTorch/Stable-Baselines3、FastAPI、React/TypeScript、pytest/Vitest。

**Spec:** `docs/superpowers/specs/2026-10-13-stock-training-design.md`

## Global Constraints

- 首版采用“量价训练＋公司资讯选股”；财报及新闻不进入 RL 观察空间。
- 先支持来源明确的沪深主板普通 A 股；采用 `raw_cash` 数据。
- A 股日线、人民币现金账户、多头与现金、固定股票池、周/月决策、下一交易日开盘执行。
- 不连接券商、不自动下单、不另建账户模拟器。
- 不重写历史收益、不伪造历史实现哈希、不保留仅为旧测试服务的运行分支。
- 每个可独立检查的实现阶段验证后本地提交，不自动 push。
- 用户未跟踪的 `docs/planning-artifacts/prd.md` 不属于本次改动。

## Review Focus

- 状态只有收盘证据：开盘成交不得借用当日尚未知的信息；T1/T2 测试阻塞未知开盘权限。
- 费率切换、重复区间或缺口：每笔成交必须唯一匹配；T1 测试截止边界和冲突。
- 买入余额刚好不足、部分卖出：全部费用进入现金和 FIFO；T2 手算对账。
- 已有 ETF 冻结模型：T3/T4 核对原定义真实哈希与续训、回测，禁止修改 checkpoint 来迁就新声明。
- 无公司行动数据与明确无事件不同：T1 检查完整覆盖证据，T4 区分真实数据和合成闭环。

## 实施顺序与接口

T1 → T2 → T3 → T4。执行期间先审核相关测试的现行意义；“仅 ETF 可运行”的旧验收在 T3 更新，真实 ETF 行为测试继续保留。

### T1：有证据的股票数据与规则校验

**Files:**
- Create: `src/quanto/backtest/stock_data.py`, `tests/fixtures/stock_training_cases.py`, `tests/unit/test_stock_data.py`
- Modify: `src/quanto/backtest/dataset_identity.py`, `src/quanto/workbench/configuration.py`, `src/quanto/data/point_in_time.py`, `src/quanto/workbench/worker.py`

**Interfaces:**
- `validate_stock_data(bundle, binding, symbols: tuple[str, ...], interval: ResearchInterval) -> StockExecutionContext`：返回只读证券映射、开盘权限和费用索引；缺事实抛带现有能力错误码的 `ValueError`。
- `StockExecutionContext.fee_rule(symbol: str, trade_date: date, side: str) -> Mapping`：唯一日期规则；调用者传入数据局部键，索引按 `AssetBinding` 解析规范身份。
- `STOCK_TABLE_VERSIONS`：统一声明并在在线 resolver、worker、离线重解析中使用，不仅由测试注入。

**Data decisions:**
- 核心 instruments 保留现有列并增加 `board`；首版 `main_board`、SSE/SZSE、stock，手数 100、交收 1。超出支持范围拒绝。
- `auxiliary['fee_rules']` 列：`exchange, instrument_type, board, side, effective_from, effective_to, stamp_duty_rate, transfer_fee_rate, available_at, source_ref`。区间采用 `[effective_from, effective_to)`，不允许空结束日；两个方向分别声明，买入印花税明确为零。币种 CNY，比例费率。
- 表语义 `stock-fee-rules@1`，按发布时间和生效区间核验。费率原始证据由导入包归档，`source_ref` 不能仅有任意字符串。
- `auxiliary['stock_sources']` 保存证据：`source_ref, source_url, payload_base64, payload_sha256, published_at, retrieved_at, available_at, coverage_start, coverage_end`，语义 `stock-sources@1`。解码后核对原始字节摘要，规则/状态引用必须指向已有证据。事件覆盖引用放在 manifest 的 `field_semantics['stock_event_coverage_source']`，必须覆盖研究区间。这样证据随现有 auxiliary 导入归档，不依赖原始本机路径。
- `auxiliary['stock_open_status']` 列：`symbol, date, status, can_buy, can_sell, available_at, source_ref`；语义 `stock-open-status@1`。每个执行交易日、每个成员恰好一条，证据不晚于当日开盘。核心日线 status 继续用于收盘可见状态；股票撮合使用这张表。
- 公司行动采用现有表；`event_coverage=complete` 同时需要证据引用，空表也须声明覆盖区间。股息税未建模的限制进入 manifest/report。
- 专用校验接入 stock 配置的能力检查和实际执行前，ETF 不被要求提供股票辅助表。类型检查通过不等同于声明事实已经向第三方独立核实。

- [x] **Step 1: Review existing tests and add failing behavior tests.** `test_fee_rules_require_unique_covered_intervals` 检查首尾边界、重叠、缺口；`test_open_status_rejects_late_or_missing_evidence` 检查收盘时间的权限不能供当日开盘使用；`test_stock_identity_board_units_and_event_coverage_are_required` 检查板块、单位、身份、无事件证据和未知退出事件。
- [x] **Step 2: Verify RED.** Run `.venv/Scripts/python.exe -m pytest tests/unit/test_stock_data.py -q`，记录缺实现导致的失败。
- [x] **Step 3: Implement the interfaces and fixture.** 使用现有证据归档/绑定及表语义，禁止从证券代码猜板块或将缺状态填真。每次运行只构建一次只读索引，逐日执行不扫描整张规则表。
- [x] **Step 4: Verify GREEN and related data semantics.** Run `.venv/Scripts/python.exe -m pytest tests/unit/test_stock_data.py tests/unit/test_point_in_time.py tests/unit/test_framework_requirement_checks.py -q`；修复新引入的问题。
- [x] **Step 5: Inspect diff and commit.** `feat: validate evidenced stock execution data`，只暂存本任务文件。

### T2：股票费用贯穿成交、账本和报告

**Files:**
- Create: `src/quanto/backtest/stock_trading.py`, `tests/unit/test_stock_trading.py`
- Modify: `src/quanto/backtest/orders.py`, `src/quanto/backtest/models.py`, `src/quanto/backtest/ledger.py`, `src/quanto/backtest/engine.py`, `src/quanto/backtest/metrics.py`, `src/quanto/reports/backtest_report.py`, `src/quanto/investing/evaluation.py`, `src/quanto/workbench/configuration.py`
- Test: `tests/unit/test_order_ledger.py`, `tests/unit/test_trading_runtime.py`, `tests/unit/test_report_bundle.py`

**Interfaces:**
- `StockTradingDefinition(TradingDefinition)`，实例 `STOCK_TRADING_DEFINITION`：`stock-daily@1`，仅 stock/raw_cash；参数 schema 空，税费率只有 T1 的冻结表一个来源。
- `StockTradingDefinition.fees(fill_input: Mapping) -> Mapping[str, float]`：严格接受 `trade_date, instrument_id, board, side, quantity, price, commission_rate, minimum_commission, fee_rule`；返回 `commission, stamp_duty, transfer_fee`。
- `StockTradingDefinition.with_cost_multiplier(multiplier: float) -> StockTradingDefinition`：创建仅用于本次成本实验的执行实例，原注册定义不变；该倍数由已冻结 `PolicyBacktestSpec.cost_multiplier` 提供，仅缩放数据表税费。佣金/滑点仍由既有 evaluation 路径缩放，禁止二次缩放。
- `execute_open(..., trading_definition=..., stock_context: StockExecutionContext | None = None)`：股票必须有 context，ETF 调用既有佣金契约；这是实际资产规则分派，不是旧格式回退。
- `Fill` 增加 `stamp_duty`、`transfer_fee` 金额及 `total_fees` 只读属性。ETF 填写明确零税费；股票金额必须来自已校验规则。

- [x] **Step 1: Add failing ledger and reporting tests.** 合成费率 `stamp_duty_rate=.0005`（仅卖）、`transfer_fee_rate=.00001`；初始现金 2000，买 100 股@10 再卖 100 股@12，最低佣金各 5：现金及已实现盈亏应为 2189.38/189.38，税费 .60、过户费 .02、佣金 10。零成交各项为零；费用不可重复扣减。

  手算断言：`assert ledger.available_cash == 2189.38`，`assert report.metrics['stamp_duty_paid'] == pytest.approx(.60)`，`assert report.metrics['execution_cost'] == pytest.approx(10.62)`（此夹具滑点为零）。
- [x] **Step 2: Verify RED.** Run `.venv/Scripts/python.exe -m pytest tests/unit/test_stock_trading.py -q`。
- [x] **Step 3: Implement shared execution integration.** 预算、卖出费用检查、Fill、FIFO 与 round-trip 指标使用一致金额；报告新增各项汇总及 `execution_cost = commission_paid + stamp_duty_paid + transfer_fee_paid + slippage_cost`。在 SessionRunner 从 T1 context 取得当日开盘状态和规范证券身份。
- [x] **Step 4: Verify GREEN.** 同一固定成交路径成本倍数为 2 时费用应为 21.24、净收益 178.76；增加余额临界、部分成交、跨费率日和分批 FIFO 测试。运行 stock trading、order ledger、trading runtime、report bundle 和 ETF baseline 相关测试。核对 `TradingDefinition` 原源码未变。
- [x] **Step 5: Inspect diff and commit.** `feat: account for stock taxes and transfer fees`。

### T3：注册股票特征与 PPO，验证模型完整生命周期

**Files:**
- Create: `src/quanto/investing/stock_definitions.py`, `tests/unit/test_stock_learning_definitions.py`, `tests/integration/test_stock_training.py`
- Modify: `src/quanto/workbench/definitions.py`, `src/quanto/investing/feature_runtime.py`, `src/quanto/investing/evaluation.py`, `src/quanto/workbench/configuration.py`, `src/quanto/workbench/worker.py`
- Update tests: `tests/unit/test_definition_registry.py`, `tests/integration/test_framework_acceptance.py`

**Interfaces:**
- `STOCK_MARKET_FEATURES`：`stock-market-observation@1`，使用已有 MARKET_FIELDS 算术和逐字段来源/缺失掩码；独立股票需求声明，不能调用拒绝 stock 的 ETF requirements。
- `STOCK_PPO_DEFINITION`：`stock-ppo-cash-log@1`；动作 `hold-target-cash-quartiles-v2`，奖励 `net-log-return-v1`，只允许 `stock-daily@1`。lookback/frequency 仍使用现有 `PolicyTrainingSpec`。
- 股票 environment factory 返回现有 `DailyETFEnv` 配置的共享环境，使用独立股票特征和交易定义；不修改 `DailyETFEnv` 或 `LearningDefinition` 源码。复用现有编码/动作/净对数奖励函数，并把真实执行依赖纳入股票实现哈希。
- 成本实验在 runtime 的股票 trading 实例上应用 T2 倍数，训练恒为 1；原始数据和注册对象不被修改。

- [x] **Step 1: Review old registration assertions; add failing production-path tests.** 注册表可发现三类 stock 定义；股票+ETF 定义交叉使用被拒；股票辅助表缺失被拒；原 ETF 定义哈希与基线一致；未来价格、事件和状态不改变过去观察。

  能力断言：`assert report.status == 'ready'`；缺规则时 `assert report.status == 'blocked'` 且 `assert 'DATA_REQUIREMENT_UNSATISFIED' in report.error_codes`。通过 `np.testing.assert_array_equal(before, after)` 核验过去观察未被未来事实改变。
- [x] **Step 2: Verify RED.** Run `.venv/Scripts/python.exe -m pytest tests/unit/test_stock_learning_definitions.py tests/integration/test_stock_training.py -q`。
- [x] **Step 3: Implement definitions and shared service integration.** 使用现有 ModelStore/队列/产物发布；能力检查离线且不启动训练。更新过时的“全注册表只能 ETF”断言，不保留假股票实现。
- [x] **Step 4: Verify GREEN.** 合成股票数据运行最小 CPU PPO（`n_steps=2, batch_size=2, n_epochs=1, hidden_size=8, total_timesteps=4`），完成保存、再训练、policy/cash/buy_hold 回测；固定目标和 RL step_weights 的账本一致。换成员、重排或替换训练数据必须拒绝续训。运行既有模型提交、冻结重解析及 ETF baseline 回归。
- [x] **Step 5: Inspect diff and commit.** `feat: enable registered stock price-volume PPO training`。

### T4：界面、使用示例、当前模型验证和阶段验收

**Files:**
- Modify: `web/src/pages/ModelsPage.tsx`, `web/src/pages/ModelDetailPage.tsx`, `web/src/components/BasketEditor.tsx`, `web/src/App.tsx`, `README.md`, `docs/research/stock-definition-onboarding.md`
- Create: `examples/stock/README.md`, `examples/stock/training.json`, `docs/research/stock-training-acceptance.md`, `web/tests/stock-model.test.tsx`
- Existing reference tests: `tests/integration/test_model_framework_bindings.py`, `tests/integration/test_framework_model_submission.py`, `tests/integration/test_model_backtest_coverage.py`

- [x] **Step 1: Add focused failing UI tests.** 股票组合只显示匹配股票方案，数据缺口可读；创建模型不自动训练；用户无需理解版本哈希。只修改现有页面实际有缺口的部分。
- [x] **Step 2: Verify RED and implement entry wiring.** Web 测试失败后修正选择/展示；使用“个股量价 PPO”等业务名称。示例明确完整数据导入要求、税前分红限制及训练 JSON 字段。
- [x] **Step 3: Verify GREEN and the existing model boundary.** 先确认所有原 ETF 注册定义摘要实际不变；在隔离测试工作区验证既有 ETF 保存版本回测/续训。若哈希发生变化，回到本阶段代码定位并修正；不通过改数据库或产物摘要掩盖变化。
- [x] **Step 4: Run release checks.** `.venv/Scripts/python.exe -m pytest -q`；`npm.cmd --prefix web run test -- --run`；`npm.cmd --prefix web run typecheck`；`npm.cmd --prefix web run build`；相关浏览器流程测试。只做一次适用范围内的完整验证，除非变更或失败需要重跑。
- [x] **Step 5: Record evidence and commit.** 记录真实数据是否已具备；若未取得完整事实，只声明软件闭环通过并列出实际缺口。`feat: expose verified stock training workflow`。不自动重启现有研究服务、不在用户工作区启动长时间训练。

## 后续

本阶段验收后按 `docs/superpowers/plans/2026-10-13-stock-screening.md` 接入选股。选股可以保存候选和证据；只有具体数据/定义齐备时才显示训练就绪。
