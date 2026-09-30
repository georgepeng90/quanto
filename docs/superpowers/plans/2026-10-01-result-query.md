# 回测结果查询与展示 Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for independent backend and UI tasks, then verify the integrated system. The user has authorized continuous execution without manual review gates.

**Goal:** 回测详情以摘要及按日/区间分页查询展示，避免全量下载和误报无数据。

**Architecture:** 冻结产物经过内容验证后生成 SQLite 查询副本；服务端用索引查明细并缓存有界图表摘要；网页仅请求当前视图和当前页。

**Tech Stack:** Python SQLite / FastAPI / React / Vitest。

**Spec:** `docs/superpowers/specs/2026-10-01-result-query-design.md`

## Global Constraints

- 不改冻结产物；SQLite 为可重建派生数据；每次源完整性检测仍基于内容。
- 保留数字和日期口径、A 股红涨绿跌和研究来源提示。
- 列表使用 100 行分页，图表默认最多 1200 点；禁止客户端遍历结果表。
- 不触碰原有未跟踪 PRD，不 push；各阶段验证后单独 commit。

## Review Focus

- 同大小且恢复时间戳的损坏不能被缓存隐藏。
- 切换筛选时，旧响应不能覆盖新结果。
- 单表错误不能使其他区域留空。
- 执行日与信号日不同仍能追踪来源；上下文不能受当前页影响。
- 采样不能重新计算绩效，也不能把采样表称为完整数据。

### Task 1: SQLite 查询及图表摘要

**Files:** `src/quanto/workbench/result_index.py`、`service.py`、`results.py`、`src/quanto/api/routes.py`；对应集成测试。

**Interfaces:** 按设计返回 TablePage (`items,next_cursor,total`) 和图表摘要 (`items,total_rows,sampled`)；表日期筛选使用 `date_from,date_to`，追踪使用 `context_date`。

- [ ] 写并运行红测试：大表筛选分页、SQL 索引、并发发布、跨日关联、摘要上限、缓存再用、篡改拒绝。
- [ ] 实现流式导入、原子发布、索引查询、图表摘要；删除被取代的整表内存缓存。
- [ ] 运行相关 Python 测试与差异检查，然后提交后端阶段。

### Task 2: 明细与追踪交互

**Files:** `web/src/components/ResultTables.tsx`、新增 `RunTrace.tsx`、必要加载组件和对应测试。

**Interfaces:** 组件消费 `runId,defaultDate,startDate,endDate,refreshToken,live`；通过 `api.runTable(id,name,query,signal)` 查询。追踪仅单日；明细允许区间。

- [ ] 写并运行红测试：初次只加载账户页；切页/筛选单次查询；旧请求被取消；单日跨日关联；失败可重试且不假报空。
- [ ] 实现按需请求和独立分页、加载/错误/空状态。
- [ ] 运行前端测试与类型检查，提交交互阶段。

### Task 3: 详情总览与图表

**Files:** `RunDetailPage.tsx`、`ResultCharts.tsx`、`ComparePage.tsx`、`api.ts`、`types.ts`、前端测试。

- [ ] 删除 12 表遍历；运行状态和日志独立更新，加载图表与明细组件。
- [ ] 当前图表使用 `api.runChart`，切页签/区间取消旧请求，明确摘要口径；对比也采用摘要。
- [ ] 验证瞬时失败重试、真实空记录、长回测无全量请求，前端全测与 build 后提交。

### Task 4: 集成验证与使用说明

**Files:** 使用指南、必要端到端测试、本阶段实测记录。

- [ ] 运行适用全套测试；真实长回测记录冷/热查询与返回行数。
- [ ] 在浏览器验证详情、切换图表、区间筛选、分页、追踪并保存证据。
- [ ] 更新使用说明和限制，独立审查后修复问题并提交。
