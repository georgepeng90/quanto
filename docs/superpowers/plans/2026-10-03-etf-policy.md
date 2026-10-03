# ETF 中长线自主交易 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供日线观察、周/月自主决策、净收益驱动的本地 ETF PPO 训练与冻结回测。

**Architecture:** 共用日线 SessionRunner；Gymnasium 环境驱动成熟 PPO。新增 investing 服务/页面，复用不可变数据版本、冻结源码任务和产物登记。

**Tech Stack:** Python 3.13、PyTorch、Stable-Baselines3 2.9.x、Gymnasium 1.x、FastAPI、React。

**Spec:** `docs/superpowers/specs/2026-10-03-etf-policy-design.md`

## Global Constraints

- Windows 当前 PC、分支 deeplearning；用户已授权自主执行，不重复请求审批。
- 日线观察，自然周/月决策，下一交易日开盘成交，多头并允许现金。
- 训练与冻结回测使用同一日线账户，保留数据来源与研究价格限制。
- 学习奖励是扣费后净值对数变化；验证选模，测试不参与本次选模。
- 不改动未跟踪 `docs/planning-artifacts/prd.md`，只本地提交。

## Review Focus

- 片段边界被错认成周/月末，或终点动作被虚构成交：环境测试。
- 未来数据改变过去观察、资产未上市或预热不足却获得交易资格：环境测试。
- 复权与公司行动重复计入、风险动作在非决策日丢失：共享账本回归。
- 回测加载错误资产顺序、篡改产物或测试期参与选模：训练与 API 测试。
- 取消发生在发布前后、旧任务 SQLite 迁移丢记录：任务生命周期测试。

## Task 1: 日线交互环境

**Files:** `src/quanto/backtest/engine.py`, `src/quanto/investing/environment.py`, `tests/unit/test_investing_environment.py`。

**Interfaces:** SessionRunner(bundle, spec, cancelled, collect=True) 提供 advance_to_close、submit_close_intent、result；DailyETFEnv(bundle, spec, frequency, lookback, episode_sessions=None, training=False) 提供标准 Gymnasium reset/step，result() 导出本次回放。

- [ ] 先建立固定策略与交互动作一致性、未来扰动、周/月边界、Hold、奖励、非决策日风险测试。
- [ ] 机械提取日线循环，保留原 run_backtest 外部契约；实现因果观察和训练片段抽样。
- [ ] 运行环境测试及现有日线账本/无未来信息测试，提交阶段成果。

## Task 2: PPO 训练与冻结评估

**Files:** `src/quanto/investing/contracts.py`, `training.py`, `evaluation.py`, `tests/unit/test_investing_training.py`, `pyproject.toml`, `uv.lock`。

**Interfaces:** train_policy(spec, dataset_dir, output_dir, emit, cancelled, dataset_content_hash)；backtest_policy(policy_dir, dataset_dir, spec, output_dir, emit, cancelled)。模型 manifest kind=policy；回测 summary 包含 metrics/assumptions/counts/period/metadata，CSV 兼容学习结果索引。

- [ ] 审查新需求的测试合同，测试日期顺序、权重学习、验证选择、save/load 一致、测试隔离、取消。
- [ ] 锁定成熟依赖，实现 PPO MLP、验证 checkpoint、最终测试、确定性回测和三个基准。
- [ ] 运行 CPU 真实小模型训练与冻结回测；执行适用单元测试后提交。

## Task 3: 作业、API 与 CLI

**Files:** `src/quanto/investing/service.py`, `api.py`, `jobs.py`; `learning/registry.py`, `workbench/worker.py`, `scheduler.py`, `models.py`, `api/app.py`, `cli.py`; `tests/integration/test_investing_workbench.py`。

**Interfaces:** `/api/investing/overview` 返回 capabilities/datasets/policies/backtests/jobs；POST train 接收 dataset_id/spec，POST backtests 接收 dataset_id/policy_id/spec；GET policies|backtests/{id}、backtests/{id}/tables/{name}|charts|export。

- [ ] 先测试认证、日期/产物/请求拒绝、旧表迁移、取消不登记。
- [ ] 复用 source freeze、dataset_objects、registry、结果索引；新增两种 job kind 与 investing CLI。
- [ ] 通过真实监督子进程跑通提交至结果及导出，提交。

## Task 4: ETF 自主投资页面

**Files:** `web/src/pages/InvestingPage.tsx`, `web/src/investingTypes.ts`, `web/src/api.ts`, `web/src/App.tsx`, `web/src/styles.css`, `web/tests/investing.test.tsx`。

- [ ] 页面显式说明“日线观察，周/月决策，净收益学习”，支持现有日线数据版本与日期选择。
- [ ] 实现训练配置、任务查询/取消、模型指标、冻结回测/基准、每日结果/导出；区分既有分钟预测入口。
- [ ] 验证表单合同与失败反馈，运行前端测试/typecheck/build，并进行真实浏览器验收后提交。

## Task 5: 可复现验收与指南

**Files:** `scripts/verify_investing_system.py`, `examples/investing/`, `docs/07_ETF自主投资训练指南.md`, `docs/research/2026-10-03-etf-policy/acceptance.md`, `README.md`。

- [ ] 隔离生成多时期多 ETF 日线，真实 PPO 训练与周/月冻结回测、对照、奖励和文件完整性检查。
- [ ] 审查已有真实日线版本，能满足覆盖时完成小规模真实实跑，记录数据限制与资源消耗。
- [ ] 独立代码审查、适用全套回归、浏览器验证；保留成果与明确限制，提交并更新本计划。
