# DSR 奖励与近期验证 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for integration, with independent environment and UI work delegated in the same workspace. Follow superpowers:test-driven-development and verification-before-completion. The user authorizes immediate development without another approval gate.

**Goal:** 实现 DSR 与同输入对照奖励，保留旧模型，并用一个预先确定的近期区间选择新试验权重。

**Architecture:** 在现有环境逐日记账位置更新收益统计，把学习奖励和真实收益分开。训练、推断共享奖励身份与状态规则；现有版本链、种子和选优算法保留。测试日期成对可选，近期试验通过一个普通脚本准备两个配置和导出验证对比，不新增实验平台或回测引擎。

**Tech Stack:** Python 3.13、Gymnasium、Stable Baselines3 2.9.0、Pydantic、pytest；React、TypeScript、Vitest。

**Spec:** [当前 SDD](../specs/2026-10-06-rl-dsr-reward-design.md)

## Global Constraints

- 工作基线 `6624860`；在当前 `training` 工作区实现，复用已安装依赖。未跟踪的 `docs/planning-artifacts/prd.md` 不属于本任务。
- 三个版本为 `net-log-return-v1`、`net-log-return-with-state-v1`、`differential-sharpe-v1`；缺省为旧版本。已有清单不重写、不改变原哈希。
- 两个新方案共有额外 A/B 输入；`eta=0.01`、`A_0=0`、`B_0=0.0001`、`variance_floor=1e-8`，float64；不裁剪 DSR。两个新方案只允许 gamma=1、risk_free_rate=0。
- 新方案的前缀现金日更新 A/B，但奖励不归给首个动作；真实收益必须满足前缀加动作区间等于终值对数收益。
- 保留固定 ETF、执行账本、PPO、seed 配置和续训采样、last/policy 分离。不同数据、日期、奖励、输入的链不能相互续训。
- 最近两年为试验准备默认值，可预先选择一年；以数据完整截止日为准，训练在此前，验证作为选优用途。没有多时期、多 seed、长训练或自动调参任务。
- 每个独立阶段完成适用验证后立即本地 commit；不 push。

## Review Focus

1. 缺省 reward 字段的历史产物仍可读取、回测、续训，且原文件不变（Task 2）。
2. 自动 episode reset、现金前缀与最后一个短决策区间不丢日、不重复算奖励（Task 1）。
3. 无留出测试期时训练和 UI 均不凭空创建测试日期或把验证数据当独立测试（Tasks 2、3）。
4. 近期验证导出必须使用选中的 policy.zip，并核对原数据内容哈希；拒绝跨配置比较（Tasks 2、4）。
5. 共同数据截止、闰日回溯、不完整覆盖或预热不足均有明确结果；不能静默缩短一年/两年（Task 4）。

## Task 1: 奖励契约、每日统计与环境输入

**Files:** `src/quanto/investing/rewards.py`（新）、`contracts.py`、`environment.py`；`tests/unit/test_investing_rewards.py`（新）、`test_investing_reward_contracts.py`（新）、`test_investing_environment.py`。

**Interfaces:**
- `rewards.py` 提供三个版本常量、`STATE_REWARD_VERSIONS`、`reward_parameters(reward_version) -> dict`、`DifferentialSharpeState.update(daily_return) -> (dsr, floor_triggered)`。
- `DailyETFEnv(..., reward_version=LEGACY_REWARD_VERSION)`；`feature_version(lookback, reward_version=legacy)` 和 `environment_version(reward_version=legacy)` 保留旧身份，新输入使用旧特征名加 `-dsr-state-v1`，新环境 `daily-session-reward-state-v2`。
- reset info 含 `prefix_net_log_return`；step info 含 `step_net_log_return`；两者提供区间 `reward_daily_count`、`reward_variance_floor_count`、`reward_dsr_max_abs`。`env.reward_diagnostics` 在环境生命周期累计以上统计和 `reward_sum`，后者仅累加实际发给 PPO 的奖励；A/B 按 episode 重置。

- [x] 审查已有环境测试：旧奖励/维度/前缀/账户测试仍有效，新增测试只验证新方案。
- [x] RED：独立手算首日 +1% 得 D=1、A=.0001、B=.0001；多日序列、零收益/冲击、非有限/非正权益；相同动作两方案同状态和实际收益；前缀、短周、reset、collect=False 和因果检查。契约拒绝未知版本、新版本不合规 gamma/risk-free。
- [x] GREEN：在每次 `advance_to_close` 后流式计算；新动作奖励按日相加；旧维度和首步语义保持不变。新增两个有界状态输入。
- [x] 验证：`.venv/Scripts/python.exe -m pytest tests/unit/test_investing_rewards.py tests/unit/test_investing_reward_contracts.py tests/unit/test_investing_environment.py -q`，全部通过后提交本阶段文件。

## Task 2: 训练、可选测试日期、推断和验证报告

**Files:** `contracts.py`、`training.py`、`evaluation.py`；`tests/unit/test_investing_reward_training.py`（新）、`test_investing_training.py`、`test_investing_continuation.py`、必要的 integration 测试。

**Interfaces:**
- `PolicyTrainingSpec.reward_version` 缺省旧方案；`test_start/test_end: date | None = None`，只能同有同无。
- `evaluate_policy(..., reward_version=legacy)` 保留返回 `(metrics, result, report)`；metrics 添加 `evaluation_reward_sum` 和数值诊断，账本比较使用独立净收益。
- 新产物记录 `reward_version`、`reward_parameters`、派生特征/环境身份、当前会话 `training_reward_sum` 与诊断。缺少奖励字段的旧清单在读取后解释为旧方案，不改原产物。
- `evaluate_validation_policy(policy_dir, dataset_dir, output_dir, emit, cancelled)` 读取 policy.zip，在原验证区间导出与普通回测相同的完整表和 summary，标识 `policy_validation` / 用于选优；校验数据哈希。普通 backtest 截止日保护不变。

- [x] RED：无测试期创建/训练/续训；部分测试日期或重叠拒绝；两种新奖励真实小规模 PPO 更新、维度/参数恢复、跨奖励拒绝；历史缺省字段产物可用且哈希不变；prefix+step 净收益账本、报告所选版本及数据匹配。
- [x] GREEN：训练数据检查仅遍历存在分区；有测试期且请求评价时才执行末次测试；否则 test=null 且不宣称已测试。所有环境构造与评价调用传递奖励版本，续训校验身份与固定常数，记录会话诊断。
- [x] 复用报告导出代码，独立验证入口不能放开普通后续回测限制；不增加 Web 作业或训练历史重构。
- [x] 验证：全部 `tests/unit/test_investing*.py`、`tests/integration/test_investing_models.py`、`test_investing_workbench.py`，通过后提交。

## Task 3: 创建表单与详情显示

**Files:** `web/src/investingTypes.ts`、`components/ModelCreation.tsx`、`components/ModelBacktestForm.tsx`、`pages/ModelDetailPage.tsx`；`web/tests/models.test.tsx`、`model-history.test.tsx`。

**Interfaces:** `reward_version` 在旧模型类型中可省略；测试日期可省略/null；无测试期 `backtest.end=validation_end`。其余模型和作业 API 不变。

- [x] RED：表单可选择三种奖励，新对照标研究用途；缺省旧奖励，详情准确显示；无测试期可提交、只填一项拒绝；切到无测试期模型后普通回测日期清空；现有 seed 值照常发送。
- [x] GREEN：新增奖励选择，两个新方案固定 gamma=1、risk_free_rate=0；无测试期显示“未设置留出测试期”；保留其他交互。
- [x] 验证：`npm.cmd test -- --run`、`npm.cmd run build`（web 目录），通过后提交。

## Task 4: 单区间试验准备与对比入口

**Files:** `scripts/compare_policy_rewards.py`（新）、`tests/test_compare_policy_rewards.py`（新）、`docs/research/dsr-reward-trial.md`（使用说明）。

**Interfaces:**
- `prepare_trial(spec_path, dataset_path, output_dir, years=2)` 读取现有训练配置和数据，输出一份 trial.json、两份新模型创建可用的 spec；只改变奖励、共同训练/验证日期及必要约束。保留用户 seed、预算、ETF 等配置。
- 区间 `(T-N年,T]`；T 取数据覆盖内完成的共同交易日，核对篮子行情/状态覆盖；周年日不存在时取月末。保留训练起点，训练终点取验证起点前一天；日期/数据/预热不够则报错。
- `compare_trial(trial_path, control_policy_dir, dsr_policy_dir, output_dir)` 核对奖励、原数据身份、两配置和完成步数，调用 Task 2 验证报告入口，输出两份账户报告及 comparison.json，逐项列出 Sharpe、收益、回撤、成本等；无效成绩不宣称胜出。
- CLI 只有 `prepare`、`compare`；没有自动训练、seed 矩阵或额外时期。

- [x] RED：一年/两年与闰日日期、共同截止、缺覆盖拒绝、保持用户配置；比较时错数据/错奖励/错日期/预算不等拒绝；真实小型产物只取 policy.zip、验证用途清晰。
- [x] GREEN：复用现有数据加载/校验、产物哈希和评价报告；说明如何创建两模型、同预算续训及比较，不运行真实市场长训练。
- [x] 验证：新脚本测试、`--help` 和全 Python suite；提交入口与说明。

## 最终验证与交付

- [x] 全 Python suite、全 Web suite、Web build 和 `git diff --check`。
- [x] 独立审查整个改动，修复有行为影响的问题并补充有效回归测试。
- [x] 记录实际验证结果，确认所有任务改动已分别提交，报告实现能力与尚未运行真实收益实验的边界。

## 执行记录

- 基线：相关 Python 测试 83 通过；已运行的 Web 模型/投资/API 测试 15 通过。
- 既有账户/收益断言保留在旧奖励路径；新增奖励采用独立收益账本。旧测试期必填假设按批准的新需求调整。
- 采用当前工作区分文件并行开发，主代理统一暂存和提交；避免另建工作区导致本地依赖与测试环境重复配置。
- Task 1：每日奖励/环境与 65 项针对性测试通过，提交 `22fb93c`。奖励契约与可选日期随 Task 2 一起提交。
- Task 2：契约检查及 58 项训练/续训/评估/API 针对性测试通过，提交 `05c92b1`。历史产物只在读取后解释缺省字段，未为失效测试添加接口或适配层。
- Task 3：Web 143 项测试和构建通过，提交 `07d06ce`；额外确认默认 seed=42、手填 17 会同时进入模型/账户配置，15 项创建页测试通过，提交 `a7300e3`。
- Task 4：16 项针对性测试通过，覆盖真实 PPO 初训/续训、实际步数、所选权重报告、闰日、非交易日截止、数据缺口、全篮子预热、相等/无效选优成绩；三个 CLI 帮助入口通过。
- 真实数据仅 prepare 验证通过：原 8 ETF 篮子/seed 保持，数据截止 2026-09-30，生成训练 2022-01-01 至 2024-09-30、验证 2024-10-01 至 2026-09-30，无留出测试期；输出在系统临时目录，未启动真实市场训练。
- 独立审查已完成：修正准备脚本日期类型、必要参数约束、可变续训预算核对和训练预热；没有未解决的产品代码问题。最终产物没有所有早期会话边界，试验说明与 comparison.json 明确要求结合现有模型历史核验，未新增历史协议平台。
- 全量 Python 首轮仅一处新测试按 Windows 默认编码读取 UTF-8 报告失败；已显式指定编码并通过针对性重跑。最终完整回归：797 通过、1 跳过，耗时 312.97 秒；仅两个既有市场数据纳秒转换警告。
- 最终 Web 全量 149 通过，TypeScript/Vite 构建通过；有既有 jsdom canvas 提示与大分包警告。验证期间其他任务新增的前端页面/测试没有纳入本任务提交。
- Task 4 脚本、测试与使用说明已提交 `11ee354`；文档 UTF-8、链接、代码围栏及 `git diff --check` 通过。所有本任务阶段均本地提交，不 push。
