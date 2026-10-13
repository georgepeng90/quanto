# Agent 个股筛选 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agent 通过 API/CLI 提交可追溯公司研究，生成经规则校验的股票池，并交接到个股模型创建。

**Architecture:** `quanto.screening` 提供严格输入契约、确定性筛选和不可变结果；复用工作区内容寻址存储、数据库、组合及模型 API。网页读取同一结果，训练前验证筛选来源与成员，不内置新的 Agent 执行平台。

**Tech Stack:** Python 3.13、Pydantic 2.12+、SQLite、FastAPI、React/TypeScript、pytest/Vitest、现有 CLI。

**Spec:** `docs/superpowers/specs/2026-10-13-stock-screening-design.md`

## Global Constraints

- 前置：`docs/superpowers/plans/2026-10-13-stock-training.md` 的个股软件闭环通过。
- 首版为“量价训练＋公司资讯选股”，不把财报或新闻直接加入 RL 观察空间。
- 从当前时点开始保存研究记录；自由联网 Agent 不提供无未来信息的历史选股模式。
- 目标不足可以少选，零入选不创建空组合；公司研究不能覆盖硬条件失败。
- 来源材料是数据，不能改变筛选规则、调用权限或提交训练。
- API、CLI、网页共享结果，不自动启动训练或反复筛选追逐同一回测。
- 每阶段验证后本地提交，不自动 push；不修改用户已有未跟踪 PRD。

## Review Focus

- 资料有日期但没有具体时刻：S1 明确保守可用时间并拒绝未来证据。
- 行业缺失/数量不足/零入选：S1 返回明确原因，禁止绕过约束或创建空组合。
- 重复请求与并发创建组合：S2/S3 验证幂等、事务一致及失败重试，不留下重复关联。
- 伪造或损坏证据/结果：S2/S3 校验内容摘要与来源关系，失败不能显示 ready。
- 今日选股配历史回测：S3/S4 保留筛选时间与研究用途，不能展示为选股盲测。

### S1：证据契约与确定性筛选

**Files:** Create `src/quanto/screening/__init__.py`, `src/quanto/screening/contracts.py`, `src/quanto/screening/engine.py`, `tests/unit/test_stock_screening.py`。

**Interfaces:**
- `EvidenceSubmission`：来源名称、URL/文档标识、文本快照、published_at、retrieved_at、证券身份及归档级别（原文/摘录）；服务端计算摘要。只接收内容，不让后端根据任意 URL 发起网络请求。
- `ScreeningRules`：允许板块、最低上市天数、成交额窗口及下限、最低有效历史长度、排除状态、行业上限、目标数量。阈值显式提供，数值有限且范围合法。
- `CandidateResearch`：规范证券身份、名称、板块、行业、上市日、带日期的状态与量价事实、Agent 建议/顺序、事实依据、入选理由、风险、未知项。每条关键事实引用本次归档的证据。
- `ScreeningRequest`：名称、purpose、截至时间、基础范围描述/是否全市场、rules、evidence、candidates、Agent 模型和提示版本、幂等键。
- `screen(request: ScreeningRequest) -> ScreeningResult`：不联网、不写文件；输出完整入选/排除记录、有序身份和输入摘要，不输出上涨概率。

- [x] **Step 1: Add failing tests.** 同一输入同一结果；Agent 排第一但流动性不达标仍排除；按顺序应用行业上限；未知行业、未知状态、未来证据、重复证券、同代码不同身份均正确处理；目标不足和零入选保留结果。
- [x] **Step 2: Verify RED.** Run `.venv/Scripts/python.exe -m pytest tests/unit/test_stock_screening.py -q`。
- [x] **Step 3: Implement contracts and rules.** 日线只使用截止前已完成交易日，计算历史长度/平均成交额；披露只有日期按下一交易日收盘保守可用，缺所需日历不能猜。Agent 提供的数值有来源引用不等于独立核实，结果明确其证据级别。未被推荐但在输入范围的公司仍保留排除/未研究原因。
- [x] **Step 4: Verify GREEN.** 补测试：修改截止之后的材料不改变可用事实；摘录不宣称完整原文；缺风险说明或证据引用不能作为完整研究入选。校验能引用证据内容中的具体摘录，不能以虚构引用通过结构验证。
- [x] **Step 5: Inspect diff and commit.** `feat: add evidence-based stock screening rules`。

### S2：不可变筛选记录与 Agent API/CLI

**Files:** Create `src/quanto/screening/store.py`, `src/quanto/api/screening.py`, `tests/integration/test_screening_api.py`；modify `src/quanto/workbench/migrations.py`, `src/quanto/api/app.py`, `src/quanto/cli.py`。

**Interfaces:**
- `ScreeningStore(store).submit(request) -> ScreeningRecord`；`get(id)`；`list(cursor, limit)`；`export(id) -> bytes`。记录 ID 与内容哈希分开，重复同键返回原记录，同键不同内容冲突。
- 数据库迁移新增 `screening_records(id, name, as_of, result_hash, artifact_path, created_at)` 与 `screening_idempotency(key, fingerprint, record_id)`；工作区保存冻结请求、证据、结果文件，使用现有 `publish_files` 与摘要惯例。
- 路由 `POST/GET /api/screenings`、`GET /api/screenings/{id}`、`GET /api/screenings/{id}/export`；沿用现有同源本机鉴权。
- CLI `quanto screening submit --json request.json`、`list`、`show <id>`、`export <id> --output result.json`，使用同一 API。

- [x] **Step 1: Add failing tests.** 提交/读取/导出一致，同键同内容复用、异内容 409；未授权调用失败；文本中的指令只原样作为证据；损坏文件不能读取成成功结果；数据库升级可重复且不改历史数据。
- [x] **Step 2: Verify RED.** Run `.venv/Scripts/python.exe -m pytest tests/integration/test_screening_api.py -q`。
- [x] **Step 3: Implement persistence and routes.** 先计算完整结果再原子发布索引；失败不暴露半条 ready 记录。禁止用请求中的路径读取任意本地文件。分页 limit 1–100，确定排序。
- [x] **Step 4: Verify GREEN.** API/CLI 等价测试覆盖一次完整往返及并发幂等；输出文件只写用户指定的新目标或明确覆盖操作。
- [x] **Step 5: Inspect diff and commit.** `feat: persist stock screening records and agent API`。

### S3：股票组合、模型和离线结果来源交接

**Files:** Modify `src/quanto/framework.py`, `src/quanto/workbench/baskets.py`, `src/quanto/workbench/configuration.py`, `src/quanto/workbench/frozen_configuration.py`, `src/quanto/workbench/worker.py`, `src/quanto/investing/models.py`, `src/quanto/workbench/service.py`；extend `src/quanto/screening/store.py`, `src/quanto/api/screening.py`, `src/quanto/cli.py`；create `tests/integration/test_screening_handoff.py`。

**Interfaces:**
- `SelectionOrigin(FrozenContract)`：`screening_id, result_hash, as_of`；`FrozenBasketRevision.selection_origin` 为可选来源，新股票组合有来源时纳入内容哈希；没有来源时不序列化该字段，确保既有真实 ETF 组合身份不变。使用安装版本提供的字段排除机制，不增加旧格式适配层。
- `ScreeningStore.create_basket(screening_id, name, idempotency_key) -> FrozenBasketRevision`：只采用结果中的最终有序成员，不能通过请求覆盖成员。
- `POST /api/screenings/{id}/basket`；CLI `quanto screening use <id> --name <name> --key <key>`。该操作不提交模型或训练。
- BasketStore 的创建逻辑允许复用现有事务；组合与筛选关联原子发布。
- 模型/作业冻结时将对应筛选记录和证据归档到运行产物并核对 hash；离线重解析使用这份归档，不能依赖当前网页或工作区数据库。

- [x] **Step 1: Add failing tests.** 创建成员顺序一致；零入选拒绝创建；篡改源引用、替换成员、损坏结果失败；重复交接不产生重复组合。旧 ETF 无来源字段时内容哈希和模型身份不变。
- [x] **Step 2: Verify RED.** Run `.venv/Scripts/python.exe -m pytest tests/integration/test_screening_handoff.py -q`。
- [x] **Step 3: Implement atomic handoff and provenance.** 模型创建校验筛选引用及最终成员；具体训练配置仍走现有 capability check。缺行情时保留筛选和组合，返回可理解的阻塞原因。
- [x] **Step 4: Verify GREEN.** 从筛选记录建股票模型、冻结作业、脱离数据库核验来源；开发回测报告包含筛选时点，并标记不能验证选股的历史区间。创建/查看/导出不启动训练；更换股票池不能续训原模型。
- [x] **Step 5: Inspect diff and commit.** `feat: carry screening evidence into stock models`。

### S4：网页与真实资料试运行

**Files:** Create `web/src/pages/ScreeningsPage.tsx`, `web/src/pages/ScreeningDetailPage.tsx`, `web/tests/screening.test.tsx`, `examples/screening/README.md`, `docs/research/stock-screening-acceptance.md`；modify `web/src/App.tsx`, `web/src/api.ts`, `web/src/types.ts`, `web/src/pages/ModelsPage.tsx`, `src/quanto/api/app.py`, `README.md`。

- [x] **Step 1: Add failing UI tests.** 列表显示名称、时间、入选数量；详情可比较入选/排除原因、打开来源、看到缺口；“用于创建个股模型”保留组合选择。空结果和请求失败清楚呈现，不出现主操作用的修订/哈希字段。
- [x] **Step 2: Verify RED and implement pages.** 使用现有布局/表单/API，添加 `/screenings` 和 `/screenings/:id`，同步服务器 SPA 路由白名单。Agent 提交结果后刷新即可显示，网页不重复实现筛选算法。
- [x] **Step 3: Verify GREEN.** 相关 Vitest、typecheck、build，以及浏览器筛选详情→组合→模型创建流程；未就绪数据应停在明确缺口，不能产生训练任务。
- [x] **Step 4: Run one real public-information trial.** 归档少量真实公司资料和基础事实、调用 Agent API 生成结果，检查来源/时间/入选原因。只在独立验收工作区保存，不进行真实交易或长时训练。原文不可得、权限不足或关键状态缺失时明确记录失败/不足，禁止填假数据过验收。
- [x] **Step 5: Run applicable full checks, record evidence and commit.** 后端测试、Web 测试/类型/构建、相关浏览器测试；文档区分软件功能与尚未完成的投资效果研究。`feat: expose stock screening and training handoff`。

## 最终交付

报告两阶段的实际提交、验证结果、真实数据覆盖和未完成项。能保存和交接选股结果不代表选股带来收益；效果研究遵循设计中的规则组与 Agent 组同条件比较。
