# ETF 与个股共用框架验收记录

本次验收覆盖有序证券篮子、数据身份与时点契约、固定策略和强化学习共享执行、冻结提交、结果来源及四个工作台页面。当前注册表包含八个实际实现的 ETF 定义；个股仅能保存选择篮子，具体数据、特征、交易和学习定义留给后续研究。合成股票定义只在测试依赖中使用。

验收在 `codex/multi-asset-framework` 的隔离代码工作树和临时研究工作区进行。没有切换实际服务、迁移实际研究数据库、连接实盘或执行研究规模训练。短 CPU PPO 会话用于验证保存、续训、指定版本回测及发布事务，不提供收益有效性证据。

## 验证状态

2026-10-11 完成综合验收。T00–T13 的实现、独立工程审核和下列验证均已完成，无剩余已知阻断问题；一项权限限制导致的跳过与环境提示单独列明。实际实例切换仍按本文发布步骤另行执行。

| 检查 | 命令 | 当前结果 |
|---|---|---|
| Web 单测 | `npm.cmd --prefix web run test -- --run --maxWorkers=2` | 25 个文件，182 项通过 |
| TypeScript | `npm.cmd --prefix web run typecheck`，生产构建再次执行类型检查 | 通过 |
| Web 生产包 | `npm.cmd --prefix web run build` | 通过；存在既有大 chunk 提示 |
| Python 全套 | `uv run --no-sync pytest -q --tb=short -ra --junitxml=.superpowers/sdd/2026-10-08-multi-asset-framework/full-python.xml -o junit_family=xunit1` | 1167 项通过、1 项权限限制跳过、2 条既有警告；825.33 秒 |
| 浏览器全流程 | `$env:PLAYWRIGHT_CHANNEL='msedge'; npm.cmd --prefix web run test:e2e` | 6 项通过，包含真实 worker 训练、续训、指定版本回测与导出 |
| 隔离迁移和个股能力边界 | `uv run --no-sync pytest tests/integration/test_framework_acceptance.py -q` | 2 项通过 |
| 时点视图修正回归 | `uv run --no-sync pytest tests/unit/test_point_in_time.py tests/unit/test_feature_frames.py tests/integration/test_backtest_no_lookahead.py tests/unit/test_etf_framework_baseline.py -q` | 84 项通过 |
| 研究控制器工作区隔离 | `uv run --no-sync pytest tests/test_fixed_basket_runner.py tests/test_reward_trial_runner.py -q --tb=short` | 38 项通过，使用显式临时工作区 |
| 行情与存储基准短样本 | `uv run --no-sync python scripts/benchmark_market_data.py --symbols 2 --daily-days 3 --minute-days 3`；`uv run --no-sync python scripts/benchmark_storage_pipeline.py --symbols 5 --days 250` | 均完成；仅验证合成入口，非吞吐结论 |

默认 Playwright 启动最初因本机缺少锁定版本的 Chromium 失败，未执行浏览器行为。下载长时间无进展后停止，使用项目已有 `PLAYWRIGHT_CHANNEL` 配置运行本机 Edge；没有改动 npm 依赖或降低断言。浏览器结果仅代表该 Windows 环境。

最终 Web 全套在列表修订号修正后重跑，182 项全部通过。生产包通过 `quanto.workbench.frontend.validate_frontend` 的源码及产物哈希核验。已复核 390 像素宽的股票篮子、模型详情截图，以及桌面账户、规范证券身份与持仓图表；浏览器断言无横向页面溢出。测试中的 jsdom canvas 提示和构建的大 chunk 提示仍存在，真实浏览器图表渲染与交互通过。

Python 的符号链接篡改用例受本机创建符号链接权限限制而跳过；缺失、额外文件及内容篡改检查照常执行。既有纳秒转 datetime 的两条警告保留。首轮曾发生一次 Windows 临时目录原子重命名拒绝访问；原测试单独重跑及后续整套运行中的该用例均通过，没有加入忽略异常或自动重试来掩盖错误。

## 设计验收与行为证据

下列文件在 `tests/unit`、`tests/integration` 或 `web` 中。A01–A18 均通过相应测试及独立审核；对应测试文件已纳入上表全套命令，也可独立重跑。完整本地 Python 结果保存在命令所列 JUnit 文件，浏览器结果在 `web/test-results/acceptance.json`。

| 项目 | 验证行为 | 证据 |
|---|---|---|
| A01 身份 | 同裸代码的不同证券分离；规范身份与局部键分别保存；导出保留有序映射 | `test_framework_contracts.py`、`test_asset_bindings.py`、`test_framework_comparison.py`、`test_framework_provenance.py` |
| A02 不可变修订 | 篮子保留成员顺序；旧修订、CAS 与队列快照稳定 | `test_framework_storage.py`、`test_framework_resolution.py`、`test_snapshot_submission.py` |
| A03 数据超集 | 额外证券不扩大篮子；逐成员检查覆盖与预热；篮子外披露冲突不阻塞所选证券 | `test_asset_bindings.py`、`test_framework_capabilities.py`、`test_point_in_time.py` |
| A04 时点 | 先可知时间后修订；15 点边界、日期披露和日线收盘各按声明解释 | `test_point_in_time.py`、`test_learning_definitions.py`、`test_backtest_no_lookahead.py` |
| A05 特征 | 缺失格、来源年龄、缓存身份、训练段拟合与原子发布 | `test_feature_frames.py`、`test_feature_training.py`、`test_transform_publication.py` |
| A06 能力 | 预检查不写入、不联网；提交复查；API、CLI 和 Web 使用同一请求 | `test_framework_capabilities.py`、`test_framework_api.py`、`test_investing_models.py::test_agent_model_commands_share_http_contract`、`framework-fixed-workflows.test.tsx`、`framework-model-workflows.test.tsx` |
| A07 同一账户 | 固定与学习相同意图产生相同账本；Hold 不再平衡，每日风险仍执行 | `test_trading_definitions.py`、`test_trading_runtime.py`、`test_learning_definitions.py` |
| A08 生命周期 | 不可卖持仓保留；未知终止事件拒绝完整成功结果；事件不重复记账 | `test_trading_runtime.py`、`test_framework_capabilities.py` |
| A09 交易事实 | 原始现金和复权口径区分；费用唯一来源；状态缺失不能视为可交易 | `test_trading_definitions.py`、`test_order_ledger.py`、`test_framework_requirement_checks.py` |
| A10 ETF 等价 | 接线前固定基准逐元素验证观察、动作与账户；best 和 last 分开 | `test_etf_framework_baseline.py`、`test_learning_definitions.py`、`test_investing_continuation.py` |
| A11 模型保护 | 禁止覆盖原训练身份；拟合/训练失败和取消不发布版本或污染模型头 | `test_transform_publication.py`、`test_model_framework_bindings.py`、`test_investing_models.py` |
| A12 旧对象重用 | 原文件不改写；数据与旧模型必须有证据才可关联；真实旧格式 PPO 关联后执行 | `test_asset_bindings.py`、`test_model_framework_bindings.py`、`test_framework_acceptance.py`、`test_framework_legacy_execution.py` |
| A13 历史与迁移 | 旧报告、下载与历史入口可读；DDL 失败回滚；重复升级无新增改动 | `test_framework_storage.py`、`test_investing_history.py`、`test_final_review_contracts.py`、`test_framework_acceptance.py` |
| A14 四域闭环 | ETF 固定/模型流程；个股篮子保存、修订、缺定义阻塞；窄屏及历史页面 | `e2e/framework.spec.ts`、`e2e/workbench.spec.ts`、`e2e/models.spec.ts`、`e2e/page-recovery.spec.ts` |
| A15 真实注册 | 合成股票定义不出现在公共注册表，不可借 ETF 定义执行个股 | `test_definition_registry.py`、`test_framework_acceptance.py`、`framework.spec.ts` |
| A16 冻结执行 | 导入定义前校验输入、源码、资源和依赖；缺失或漂移不回落当前代码 | `test_worker_snapshot.py`、`test_snapshot_submission.py`、`test_framework_api.py` |
| A17 操作身份 | 新覆盖数据只可按原局部键与规范顺序回测；续训保持原数据和边界 | `test_model_backtest_coverage.py`、`test_model_operations.py`、`test_investing_models.py`、`test_framework_legacy_execution.py` |
| A18 唯一输入 | 禁止公共 symbols；冻结有效参数与请求摘要；编辑使旧预检失效 | `test_framework_contracts.py`、`test_framework_resolution.py`、`framework-fixed-workflows.test.tsx`、`framework-model-workflows.test.tsx` |

## 隔离迁移演练

`test_framework_acceptance.py` 将旧 schema 1 夹具复制到独立临时目录，仅升级副本至 schema 2。原数据库仍为 schema 1，原目录全部文件哈希未变；副本中的历史数据和 checkpoint 哨兵字节未变。此项使用小型迁移夹具。`test_framework_legacy_execution.py` 另生成真实旧格式 ETF PPO zip，经原产物审核关联后，通过实际 API/worker 续训，并在较晚覆盖、含额外 ETF 的数据集上回测原保存版本；原 zip、manifest 和模型 spec 字节保持不变。它验证一个已知组合的执行闭环，不能代表任意历史模型均可迁移。

| 对象 | 升级前 SHA 256 | 升级结果 |
|---|---|---|
| 原工作区数据库 | `047b321317fa6620605ed4653b93349fd13f2bd5c8ffb38810e850786a3e5eea` | 原件保持相同哈希与 schema 1 |
| 历史数据哨兵 | `053ef844ac837b6e577185d8fc051d575fda4e4faae57c98f47342d2094e2c44` | 原件与升级副本相同 |
| 历史 checkpoint 哨兵 | `3526413c479dffade3ca60918993b8b498b190f1c03fbbadfbb9da520693a3cd` | 原件与升级副本相同 |

升级后 datasets、experiments、jobs、investing_models 各 1 行；新增 baskets 和 model_framework_bindings 均 0 行。外键检查无错误，第二次初始化的 SQL dump 完全相同。`test_framework_storage.py` 分别在删除旧 jobs 表、创建篮子修订表及更新版本时注入失败，断言 DDL、行、版本和历史文件全部回滚，再次升级可成功。证据属性写入本地 JUnit 报告。

## 独立工程审核

两位独立 Agent 分别审核量化核心与 API/CLI/Web 发布边界。最终审核发现的具体问题及关闭证据记录如下。

| 发现 | 处理与验证 |
|---|---|
| 比较详情信任未经核验的输入 manifest | `c76ed5a` 修复；成功结果以已校验产物为来源，损坏输入披露诊断；有效输入和产物冲突拒绝。独立增量审核与 8 项测试通过 |
| 篮子外证券修订冲突影响所选证券时点视图 | `c4dc969` 修复；失败/通过测试和 84 项回归通过，独立复核确认全局表不变 |
| 历史实验升级丢失当前配置和来源 | `f565246` 修复；从指定实验复制实际配置，保存 `source_experiment_ref`，重新选择篮子与定义。后端 5 项、相关 Web 回归及独立复核通过 |
| 截图复核发现篮子列表保存后仍显示旧修订号 | `347c3fb` 同步更新该篮子摘要；既有单测增加断言先失败后通过，最终 Web 和浏览器全套通过，独立增量审核无问题 |

全套回归另发现冻结能力报告的生成时间影响确定性输出，`9148bf4` 将冻结值显式保留为 `null`，实时预检查仍保留时间。`61e517e` 使合成行情基准的日频 worker 使用真实公开入口支持的研究口径，分钟缓存仍验证 raw/reference；来源标记为合成，未放宽生产能力检查。

研究控制器原来在已指定 workspace_path 时仍读取本机配置，`f0280a7` 修复该隔离问题，保留故障恢复及来源保护断言。这 38 项使用假 API，不代表历史专用研究协议可以直接向新公共入口提交。长历史合成查询按已解析价格口径构造，存储基准则通过真实篮子、数据绑定、交易定义和解析器冻结当前契约；没有恢复旧公共 symbols 输入。存储基准仍记录原六个阶段，新增框架解析在计时段外，不能将这份短样本解释为策略端到端性能。

## 同版发布和回退

本次交付准备发布包和证据，实际实例保持原状态。正式发布时执行：

1. 完成本表全部验收，构建 Web；由现有 frontend 源码摘要核验拒绝旧 dist。
2. 停止服务与行情任务，记录当前代码版本，保存工作台 SQLite、作业输入、结果、模型产物和引用的数据对象的一致备份；外置数据根也须一并备份。
3. 使用该备份的隔离副本启动同一 API/CLI/Web 包，检查 schema、历史列表与导出，并完成一个短 ETF 固定回测及模型版本回测。
4. 演练通过后再替换实际运行包并启动。检查四域页面、定义查询、能力检查、旧记录读取和提交拒绝原因。个股缺定义状态应保持明确。
5. 需要回退时停止新服务，恢复升级前的一致备份和旧包。保留新工作区副本供核查，不让旧代码直接打开新数据库，也不自动逆迁移新研究对象。

个股下一阶段按 [接入清单](stock-definition-onboarding.md) 交付。该框架尚未给出公司财务公式、买卖阈值、学习观察/奖励配方，也未验证真实规模训练吞吐或实盘运行。
