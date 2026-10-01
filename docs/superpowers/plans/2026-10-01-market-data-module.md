# 统一行情模块与预缓存 Implementation Plan

> 历史实施计划。工作区 `market_root` 与生产脚本 `--root` 的要求已由[共享行情库方案](../specs/2026-10-02-shared-market-library-design.md)取代；实际行情不按工作区分库。当前入口和路径以 README 为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将行情获取与缓存收拢到独立模块，以统一存储、同步及异步预缓存、固定版本的本地读取服务当前日线回测和未来训练输入。

**Architecture:** `quanto.data` 管理来源、标准化、覆盖、不可变 Parquet 和 SQLite 目录；消费者只提交需求并读取已就绪快照。回测专用的 `DatasetBundle`、研究假设和资格计算迁到回测层。工作台沿用已有持久队列，监督模组预缓存并登记通过二次验证的回测集。

**Tech Stack:** Python `>=3.13,<3.14`、Pydantic 2、pandas、PyArrow `>=21,<24`、SQLite、标准库线程/asyncio；现有 FastAPI、React、pytest、Vitest、Playwright。AKShare 延续可选 `data` extra；腾讯传输使用标准库，不新增存储服务或训练框架。

**Spec:** [统一行情数据模块与预缓存设计](../specs/2026-10-01-market-data-module-design.md)。执行前同时阅读设计、当前 `AGENTS.md` 与本计划。

## Global Constraints

- 时间范围统一为起点包含、终点不包含的 `[start, end)`；时间戳使用带时区的 UTC。
- 读取排序固定为 `(bar_end, instrument_id, bar_start)`，不能依赖目录遍历顺序。
- 契约与标准化导入支持 `1d` 和 `1m` 行情；首期联网仅支持已验证的 ETF 日线、指数日线及交易日历。
- `queued -> running -> ready | incomplete | failed | cancelled`；只有完整需求、质量、文件及引用均通过的 `ready` 带可执行 `SnapshotRef`。
- 有效本地缓存优先；只在真实缺口或 `refresh=True` 时检查来源能力。所有运行读取纯本地，越界返回 `DATA_NOT_PREPARED`。
- 不推断复权、单位、可得性、上市、停牌或公司行动事实；研究假设与来源未知分别披露。
- 同步与异步共享业务逻辑；一个等待方的超时或取消不取消其他消费者的共享获取。
- 新数据不进 Git，不建立供应商专用目录；不修改既有冻结样本、历史报告与旧运行快照，不自动垃圾回收。
- 保留已登记数据和冻结 V1/V2 运行的离线重跑；不保留只为旧测试存在的采集接口或路径参数。
- 每任务先审查测试有效性，完成验证与差异检查后立即独立本地 commit；不 push，不夹带 `docs/planning-artifacts/prd.md`。
- 本计划不授权切换现有预览工作区或修改运行中的预览配置。新工作区的数据根通过明确配置接入。

## Review Focus

1. 完整离线分钟导入、没有联网分钟来源：应就绪且零上游调用，不能先拒绝来源能力（任务 3、5）。
2. 同一标的两份未知复权数据或 `qfq` 请求实际返回 `day`：保留真实模式，不自动合并，不修改旧版本（任务 4、5）。
3. 租约过期后旧工作者返回，或取消先于发布提交：旧工作者/已取消请求不能发布就绪（任务 5、6）。
4. 行情已发布但工作台取消先于回测集登记：阻止登记，同时允许完整行情快照继续复用（任务 7）。
5. 日线策略需要预热前累计历史、下一交易日、跨边界事件与公布较晚的辅助表：必须显式准备，读取得遵守原有可见性（任务 1、7）。

---

## 文件职责与依赖顺序

| 文件 | 职责 |
| --- | --- |
| `src/quanto/backtest/dataset_schemas.py`、`dataset_loaders.py`、`dataset_storage.py` | 从现有 `data` 迁移的日线回测契约、加载、验证与组装产物写入 |
| `src/quanto/data/contracts.py` | 通用需求、身份、来源、质量、覆盖、状态、引用和错误类型 |
| `src/quanto/objects.py` | 从现有对象代码提取的规范 JSON、流式哈希、安全路径与文件集合核验原语 |
| `src/quanto/data/catalog.py`、`storage.py` | SQLite 目录/事务/租约；统一内容寻址对象、证据和快照发布 |
| `src/quanto/data/validation.py`、`coverage.py` | 标准表验证、事实要求、按交易时段的覆盖与真实缺口计算 |
| `src/quanto/data/imports.py`、`readers.py` | 显式标准化导入；经过核验的纯本地表查询和有界批次 |
| `src/quanto/data/providers/base.py`、`akshare.py`、`sdk_call.py`、`tencent.py` | 无输出目录、无缓存和重试职责的来源协议及日线适配；限时 SDK 子进程调用 |
| `src/quanto/data/acquisition.py` | 缺口计划、来源选择、重试、刷新一致性与跨进程获取协调 |
| `src/quanto/data/market.py`、`__init__.py` | 公共同步/异步 API、过程内任务与共享获取生命周期 |
| `src/quanto/backtest/market_dataset.py` | 将公开行情快照转成现有日线 `DatasetBundle`，冻结研究假设 |
| `src/quanto/workbench/data_jobs.py`、`service.py`、`store.py`、`worker.py`、`snapshots.py` | 工作进程接入、可信数据根配置、取消登记事务与来源关系 |
| `web/src/pages/DatasetsPage.tsx`、`web/src/types.ts` | 展示预缓存状态、缺口和限制，移除缓存身份输入 |
| `scripts/prefetch_market_data.py`、`import_market_data.py`、`benchmark_market_data.py` | 统一采集入口、显式历史导入、容量和流水线测量 |

依赖顺序为 1 → 2 → 3；4 依赖 1、3；5 依赖 2–4；6 依赖 5；7 依赖 6；8 依赖 7；9 完成脚本收口和全局验收。共享文件按该顺序编辑；可并行的离线样本审查和独立复核不并行修改这些文件。

## Task 1: 分离回测专用契约，确定行情公开类型

**Files:** 迁移 `src/quanto/data/{schemas,loaders,storage}.py` 到 `src/quanto/backtest/dataset_{schemas,loaders,storage}.py`；创建 `src/quanto/data/contracts.py`、`tests/unit/test_market_contracts.py`；更新 `src/quanto/config.py`、`backtest/engine.py`、`reports/backtest_report.py`、`workbench/{data_jobs,service,store,worker,snapshots}.py`、`data/cache.py` 及全部实际引用的测试/脚本导入。

当前导入更新清单还包括 `scripts/{research_strategy_parameters,research_round2,prepare_strategy_research_data,prepare_round2_market_data,benchmark_storage_pipeline}.py`、`tests/fixtures/engine_cases.py`、`tests/unit/{test_dataset_contract,test_storage_loading}.py`、`tests/integration/{test_data_jobs,test_parquet_publication,test_workbench_revisions}.py`；实施时用 `rg` 复核新增引用。

**Interfaces:**
- Consumes: 现有日线 `DatasetBundle`、`DatasetManifest`、`BacktestSpec` 与加载验证规则。
- Produces: 新回测路径中的 `load_dataset(path: Path) -> DatasetBundle`、`load_dataset_for_run(path: Path, spec: BacktestSpec) -> DatasetBundle`、`validate_dataset(bundle: DatasetBundle) -> DatasetBundle`、`write_parquet_dataset(bundle: DatasetBundle, destination: Path) -> None`；原算法及签名不变。
- Produces: `InstrumentId(exchange, instrument_type, code)`；序列身份保留前导零，ETF/指数分开。`PriceBasis(mode, basis_id=None)` 的模式为 `raw | verified_adjusted | vendor_research`；研究需求可接受待解析的 basis，快照/查询必须使用已解析身份。
- Produces: `DataRequest(kind, instruments, start, end, columns, frequency=None, price_basis=None, currency="CNY")`；`kind` 为 `bars | calendar | instruments | trading_status | corporate_actions | auxiliary:<name>`，仅 bars 要求频率与口径。
- Produces: `QualityPolicy(required_evidence, accepted_limitations)`、`DataRequirement(requests, quality)`、`DataQuery(kind, instruments, start, end, columns, frequency, price_basis)`、`TableQuery(instruments, start, end, columns)`；未知字段拒绝，区间/时区必须明确。
- Produces: `SourceRecord` 固定供应商/上游接口/适配器版本/参数/获取时间/实际模式/证据摘要/单位/可得性/限制；`NormalizedChunk(request, frame, source, evidence)`；`FragmentRef`、`SnapshotManifest`、`SnapshotRef(id, manifest_digest, coverage, quality)`、`CoverageReport`、`PrefetchTaskRef(id)`、`PrefetchStatus(state, progress, coverage, snapshot, reason, limitations)`。所有公开引用均无物理路径。
- Produces: `FragmentRef(object_hash, series_id, kind, instruments, start, end, columns, source_evidence_hash, quality)`；`SnapshotManifest(contract_version, requirement, fragments, sources, coverage, quality)`，首期 `contract_version=1`。完整请求及参考表范围属于清单，查询以清单核验，不能以客户端传来的 coverage 声明替代。
- Produces: `MarketDataError(code, message, coverage=None)`；固定错误代码包含 `DATA_NOT_PREPARED`、`INCOMPLETE`、`FAILED`、`CANCELLED`、`WAIT_TIMEOUT`、`CAPABILITY_UNAVAILABLE`、`INTEGRITY_ERROR`、`SEMANTIC_CONFLICT`、`TASK_NOT_FOUND`。

- [ ] **Step 1: 审查相关测试并增加契约测试。** 保留 `test_storage_loading.py` 的全历史、资格、下一交易日及边界事件断言，迁移导入；创建以下断言，不保留旧 `quanto.data.loaders` 重导出：

```python
def test_identity_keeps_leading_zero_and_instrument_type():
    etf = InstrumentId(exchange="SZSE", instrument_type="etf", code="000001")
    index = InstrumentId(exchange="SZSE", instrument_type="index", code="000001")
    assert etf.code == "000001"
    assert etf != index
```

  另测 `test_requirement_rejects_naive_or_empty_interval`：naive 时间、`start >= end` 均拒绝；`test_refs_and_requests_reject_paths`：请求/引用带 `raw_dir`、`cache_version`、`output_path` 拒绝。
- [ ] **Step 2: 验证新增测试为红。** `uv run --no-sync pytest tests/unit/test_market_contracts.py -q`，预期缺少新契约模块；先记录迁移前 `test_storage_loading.py`、`test_dataset_contract.py` 的基线结果。
- [ ] **Step 3: 迁移回测文件并实现契约。** 用 `rg` 更新所有真实导入；现有旧 cache 在任务 9 删除前临时引用新的回测路径。类型仅定义本计划需要的字段，证据/覆盖枚举在归属任务补齐；不建立旧路径兼容层。
- [ ] **Step 4: 验证迁移语义。** `uv run --no-sync pytest tests/unit/test_market_contracts.py tests/unit/test_storage_loading.py tests/unit/test_dataset_contract.py tests/integration/test_backtest_outputs.py tests/integration/test_backtest_no_lookahead.py -q`；预期全部通过。检查导入变更，没有日线算法差异。
- [ ] **Step 5: 提交。** 仅暂存上列迁移、契约、测试和实际导入更新；`git diff --cached --check` 后 `git commit -m "refactor: separate backtest datasets from market data contracts"`。

## Task 2: 统一不可变对象与 SQLite 目录

**Files:** 创建 `src/quanto/objects.py`、`src/quanto/data/catalog.py`、新 `src/quanto/data/storage.py`、`tests/unit/test_market_storage.py`；修改 `src/quanto/workbench/dataset_objects.py`、`revisions.py` 中共同原语的引用；保留旧对象格式与摘要算法。

**Interfaces:**
- Consumes: 任务 1 的 `NormalizedChunk`、`FragmentRef`、`SnapshotManifest`、`SnapshotRef`。
- Produces: `file_hash(path: Path) -> str`、`json_bytes(value: object) -> bytes`、`safe_name(name: str) -> str`、`verified_files(root: Path, expected: dict[str, str]) -> dict[str, Path]`；抽取现有行为，避免整文件读入内存。
- Produces: `MarketStore(root: Path)`；`put_chunk(chunk: NormalizedChunk) -> tuple[FragmentRef, ...]`、`write_manifest(manifest: SnapshotManifest) -> SnapshotRef`、`verify_snapshot(ref: SnapshotRef) -> SnapshotManifest`。写清单不等于目录 ready；发布资格在任务 5 事务处理。
- Produces: `MarketCatalog(root: Path)`；`fragments(requirement: DataRequirement) -> tuple[FragmentRef, ...]`、`register_fragments(fragments: tuple[FragmentRef, ...]) -> None`、`resolve_snapshot(snapshot_id: str) -> SnapshotRef`。目录至少有 series、fragments、source_evidence、snapshots、requests、leases；事务接口在任务 5 扩展。

- [ ] **Step 1: 增加存储断言。** `test_identical_manifest_reuses_id_and_fragment`：两次同内容发布引用相等且只有一份对象；`test_old_snapshot_bytes_survive_refresh`：新数据不改旧 hash/bytes；`test_tampered_missing_extra_or_symlink_is_rejected`：逐项得到 `INTEGRITY_ERROR`；`test_unpublished_manifest_is_not_resolvable`：仅落盘未提交的版本不可解析。夹具在本测试文件用两条确定性日线记录和来源元数据构造。
- [ ] **Step 2: 红验证。** `uv run --no-sync pytest tests/unit/test_market_storage.py -q`，预期缺少新 store/catalog 接口。
- [ ] **Step 3: 实现统一物理结构。** 严格使用设计中的 `catalog.sqlite`、`objects/<前两位>/<sha256>.parquet`、`evidence/<前两位>/<sha256>.bin`、`snapshots/<id>/manifest.json`、`staging/<内部标识>`；日线完整已知序列、分钟按月。确定性排序后落盘、流式摘要、原子发布，SQLite 注册不暴露半成品；清单身份排除任务时间/进度。引用文件集核验针对单个快照清单，不将整个共享 objects 根的其他有效对象视为额外文件。
- [ ] **Step 4: 绿验证及现有对象回归。** `uv run --no-sync pytest tests/unit/test_market_storage.py tests/integration/test_shared_dataset_snapshots.py tests/integration/test_parquet_publication.py -q`；预期通过且旧对象摘要不变。
- [ ] **Step 5: 提交。** 暂存本任务所列文件；检查差异后 `git commit -m "feat: add immutable market objects and transactional catalog"`。

## Task 3: 标准化导入、覆盖计算与纯本地读取

**Files:** 创建 `src/quanto/data/{validation,coverage,imports,readers}.py`、`tests/unit/test_market_validation.py`、`test_market_readers.py`、`tests/fixtures/market_data_cases.py`。

**Interfaces:**
- Consumes: 任务 1 类型、任务 2 store/catalog；固定日线/分钟及参考表离线样本。
- Produces: `validate_chunk(chunk: NormalizedChunk) -> NormalizedChunk`；`evaluate_coverage(requirement: DataRequirement, fragments: tuple[FragmentRef, ...], store: MarketStore) -> CoverageReport`，报告逐请求缺口、字段、质量和冲突，不联网。
- Produces: `import_table(store: MarketStore, catalog: MarketCatalog, request: DataRequest, frame: pd.DataFrame, source: SourceRecord, evidence: bytes) -> tuple[FragmentRef, ...]`；导入标准表并登记可复用片段，不自行宣称整个需求 ready。
- Produces: `MarketReader(store: MarketStore)`；`read_bars(snapshot: SnapshotRef, query: DataQuery) -> pd.DataFrame`、`read_table(snapshot: SnapshotRef, table: str, query: TableQuery) -> pd.DataFrame`、`iter_batches(snapshot: SnapshotRef, query: DataQuery, batch_size: int) -> Iterator[pa.RecordBatch]`。

- [ ] **Step 1: 写验证/读取测试。** 在公共夹具文件定义 2 个标的、跨月乱序 `1m` 数据、日线、已知交易时段、停牌/上市证据、边界行动和带 `available_at` 的辅助表；未知事实另有样本。

```python
def test_batches_are_bounded_and_globally_ordered(reader, snapshot, query):
    batches = list(reader.iter_batches(snapshot, query, batch_size=2))
    assert all(batch.num_rows <= 2 for batch in batches)
    frame = pa.Table.from_batches(batches).to_pandas()
    keys = list(frame[["bar_end", "instrument_id", "bar_start"]].itertuples(index=False, name=None))
    assert keys == sorted(keys)
```

  `test_unknown_middle_gap_remains_incomplete`：首尾完整但中间缺一分钟/一天仍 incomplete；`test_evidenced_suspension_and_listing_boundaries_pass`：同样空缺只有有效证据才通过；`test_reference_and_auxiliary_queries_stay_within_prepared_bounds`：边界外 `DATA_NOT_PREPARED`。验证 UTC、半开边界、前导零、字段类型、重复同值去重/异值冲突、未知单位和可得性保留。`test_imported_minute_coverage_needs_no_live_provider`：仅本地 fragments 可得到完整 coverage。
- [ ] **Step 2: 红验证。** `uv run --no-sync pytest tests/unit/test_market_validation.py tests/unit/test_market_readers.py -q`，预期缺少上述接口。
- [ ] **Step 3: 实现验证、导入和读取。** bars 标准列固定，来源不提供的 OHLC 可为空但不能被声明为已覆盖的必需字段；按已有字段验证 OHLC，不能从指数 close 伪造 open/high/low。日历、标的、状态、行动、辅助表各自有 schema 与区间规则；行动覆盖包含需求声明的边界事件，辅助表保留公布/修订信息。批次用 Parquet 投影/过滤和有界归并，内存随 batch/活跃流增长，不随总行数增长；不先收集全部表再切批。
- [ ] **Step 4: 绿验证。** 执行 Step 2 命令；再用禁止任何 provider 访问的夹具核验所有读 API、越界和完整性失败路径。预期通过。
- [ ] **Step 5: 提交。** 暂存本任务文件；差异检查后 `git commit -m "feat: add standardized market imports coverage and local readers"`。

## Task 4: AKShare 与腾讯日线适配

**Files:** 创建 `src/quanto/data/providers/{__init__,base,akshare,sdk_call,tencent}.py`、`tests/unit/test_market_providers.py`、小型 `tests/fixtures/market_sources/` 样本。

**Interfaces:**
- Consumes: `DataRequest`、`NormalizedChunk`、任务 3 标准验证。
- Produces: `ProviderCapabilities`（kind、频率、标的类型、字段、口径与证据能力）；`FetchContext(deadline, cancelled)`，`cancelled: Callable[[], bool]`。
- Produces: `MarketProvider` 协议：`capabilities() -> ProviderCapabilities`、`fetch(request: DataRequest, context: FetchContext) -> NormalizedChunk`；`AKShareProvider`、`TencentProvider` 实现相同协议，支持注入离线 SDK/传输，不接收 root/output 路径，不实现缓存/调度重试。
- Produces: 内部 `run_sdk_call(function: str, kwargs: dict, context: FetchContext) -> pd.DataFrame`，只允许 ETF、指数、日历三个 SDK 函数；`sdk_call.py` 的 `main() -> int` 作为短生命周期子进程入口，无缓存或持久任务职责。

- [ ] **Step 1: 写离线来源断言。** 保存已取得腾讯响应的少量代表行及来源说明，补 `day`、`qfqday`、空数据、短行、错误 code 和冲突窗口样本；AKShare 使用仿 SDK 返回固定中文列。

```python
def test_requested_qfq_does_not_relabel_actual_day(tencent_provider, request, context):
    chunk = tencent_provider.fetch(request, context)
    assert chunk.source.actual_mode == "day"
    assert "unverified_price_basis" in chunk.source.limitations
```

  `test_tencent_verified_units_and_unknown_evidence_are_distinct`：有证据时 ETF 手→份乘 100、万元→元乘 10000；证据不足时不自动套用。`test_akshare_index_does_not_fabricate_ohlc`：只有 close/amount 的响应保持缺字段。`test_minute_fetch_is_not_silently_daily`：返回 `CAPABILITY_UNAVAILABLE`；`test_transport_deadline_is_bounded`：注入超时传输得到失败，未发布数据。
- [ ] **Step 2: 红验证。** `uv run --no-sync pytest tests/unit/test_market_providers.py -q`，预期适配器不存在。
- [ ] **Step 3: 实现解析与能力。** 抽取现有腾讯 JSONP、参数和行解析，按实际 `day/qfqday` 及单位证据记录；AKShare 标记实际 Eastmoney/Sina 上游，日历获取范围来自 request。复权因子未核验时生成独立研究 basis；basis 依据语义证据而非任务 ID/每次下载时间。保留响应或 SDK 观测摘要作为 evidence，不把重新序列化表宣称为原始 HTTP 字节。
- [ ] **Step 4: 实现限时 SDK 隔离并绿验证。** 已查本地 AKShare 1.18.97：三个函数无 timeout 参数，ETF GET 硬编码 15 秒且可能多次请求，指数/日历 GET 没有 timeout。生产 SDK 调用在有界 worker 中启动短生命周期 Python 子进程；标准输入传 allowlist 函数/参数，标准输出返回 Arrow IPC，SDK 普通日志导向 stderr。父进程按剩余 deadline 等待，超时终止并回收子进程；取消和晚到结果仍经任务/租约检查，不全局修改 requests。新增 `test_sdk_timeout_reaps_child_without_publishing`：受控挂起子进程超过 deadline 后被回收且无片段；执行 Step 2 命令，预期全部通过。线程 Future 超时不能代替此收尾。
- [ ] **Step 5: 提交。** 暂存本任务适配器与小型样本/测试；`git commit -m "feat: adapt AKShare and Tencent to unified daily market contracts"`。

## Task 5: 本地优先的获取协调、刷新与发布事务

**Files:** 创建 `src/quanto/data/acquisition.py`、`tests/unit/test_market_acquisition.py`、`tests/integration/test_market_catalog_races.py`；扩展 `catalog.py`、`storage.py`、`contracts.py`。

**Interfaces:**
- Consumes: `evaluate_coverage`、`MarketProvider.fetch`、store/catalog。
- Produces: `MarketConfig(source_priority, request_timeout_seconds, max_retries, retry_delay_seconds, max_workers, batch_size)`；默认 30 秒、2 次重试、1 秒间隔、4 个 worker、65536 行批次，source_priority 为 AKShare 后腾讯，测试显式使用更小值。配置只在模组/管理员构造阶段提供。
- Produces: `LeaseToken(series_id, owner, generation, expires_at)`；catalog 的 `create_request(request_id: str, requirement: DataRequirement) -> None`、`cancel_request(request_id: str) -> None`、`acquire_lease(series_id: str, owner: str, ttl: float) -> LeaseToken | None`、`renew_lease(token: LeaseToken, ttl: float) -> LeaseToken`、`release_lease(token: LeaseToken) -> None`、`commit_ready(request_ids: tuple[str, ...], ref: SnapshotRef, leases: tuple[LeaseToken, ...]) -> tuple[str, ...]`。
- Produces: `AcquisitionCoordinator(store, catalog, providers, config)`；`prepare(requirement: DataRequirement, refresh: bool, request_ids: Callable[[], tuple[str, ...]], progress: Callable[[CoverageReport], None], cancelled: Callable[[], bool]) -> SnapshotRef`。其他终态抛 `MarketDataError`，共享请求资格由 catalog 最终确认。

- [ ] **Step 1: 写零请求、补缺与一致性测试。** `test_repeat_requirement_reuses_snapshot_without_source_calls`：第二次 ref 相等、调用计数不增；`test_complete_offline_minute_prefetch_bypasses_capabilities`：无来源也成功；`test_only_actual_gaps_are_fetched`：调用区间仅缺失时段；`test_partial_or_missing_required_evidence_is_incomplete`：不得 ready。`test_incompatible_unknown_basis_cannot_be_spliced`：明确冲突/未完整且旧 hash 不变；`test_adjustment_revision_refreshes_affected_known_history`：变更锚点或重叠历史后必须重取受影响历史，不能只追加尾部。
- [ ] **Step 2: 写事务竞争断言并红验证。** 使用两个独立 catalog 连接/进程和事件屏障，不用概率性 sleep：`test_expired_owner_cannot_publish_after_new_lease` 断言 generation 增加且旧 token 被拒绝；`test_cancel_committed_before_ready_wins` 断言请求不在成功 ID 集合；`test_interrupted_publish_leaves_no_ready_reference` 断言只存在可复用片段。`uv run --no-sync pytest tests/unit/test_market_acquisition.py tests/integration/test_market_catalog_races.py -q`，预期缺 coordinator/租约实现。
- [ ] **Step 3: 实现覆盖优先与获取计划。** 先完整检查本地数据，只有缺口/refresh 才选来源；相容字段和口径才自动切换。序列锁按稳定顺序取得，锁后复查覆盖；失败/重试/退避由本层负责。研究口径不能跨来源盲拼，重叠同值去重、异值阻止自动拼接；显式刷新从单一合格来源取得一致历史并记录差异，旧版本保留。
- [ ] **Step 4: 实现事务发布并绿验证。** SQLite `BEGIN IMMEDIATE` 中核验请求取消、当前租约 owner/generation/expiry 与已校验 manifest，才登记 ready。独立连接安全续租，失去租约停止发布；所有共享请求取消后停止后续获取，晚到结果不进入新的片段/就绪发布。缺字段/参考表/证据/来源能力映射 incomplete，错误获取/解析/存储映射 failed，丢失所有权不能 ready；原因代码与缺口保留。执行 Step 2 命令，预期全部通过。
- [ ] **Step 5: 提交。** 暂存本任务文件；`git commit -m "feat: coordinate market gaps refresh and fenced snapshot publication"`。

## Task 6: 同步/异步 API 与独立请求生命周期

**Files:** 创建 `src/quanto/data/market.py`、`tests/unit/test_market_async.py`、`test_market_prefetch.py`；修改 `src/quanto/data/__init__.py`。

**Interfaces:**
- Consumes: 前述 coordinator、reader、catalog；一个过程内 `MarketData` 拥有有界执行器与共享工作注册表。
- Produces: `MarketData(root: Path, config: MarketConfig | None = None, providers: tuple[MarketProvider, ...] | None = None)`；默认来源延迟创建，离线读取不要求安装 AKShare。
- Produces: `coverage(requirement) -> CoverageReport`、`prefetch(requirement, refresh=False) -> PrefetchTaskRef`、`get_prefetch(task_id: str) -> PrefetchStatus`、`wait_ready(task_id: str, timeout: float | None = None) -> SnapshotRef`、`cancel_prefetch(task_id: str) -> None`。
- Produces: `read_bars(snapshot, query) -> pd.DataFrame`、`read_table(snapshot, table, query) -> pd.DataFrame`、`iter_batches(snapshot, query) -> Iterator[pa.RecordBatch]`；各方法提供同名 `_async`，同结果/错误，`iter_batches_async` 返回 `AsyncIterator[pa.RecordBatch]`。
- Produces: `close() -> None`、`close_async() -> None` 与同步/异步上下文管理。关闭停止提交并取消尚未就绪请求，收尾线程；不恢复旧过程内 task，已发布 ref 可重新解析。

- [ ] **Step 1: 写独立请求/等待断言。** 注入使用事件控制的慢来源，不使用实网：

```python
def test_one_wait_timeout_does_not_cancel_request(market, requirement):
    task = market.prefetch(requirement)
    with pytest.raises(MarketDataError) as failure:
        market.wait_ready(task.id, timeout=0)
    assert failure.value.code == "WAIT_TIMEOUT"
    assert market.get_prefetch(task.id).state in {"queued", "running"}
```

  `test_identical_requests_share_fetch_but_cancel_independently`：两个不同 task ID、一次获取、取消 A 后 B ready，A cancelled；`test_all_request_cancellation_blocks_late_ready`：阻塞源晚返回也不能 ready；`test_ready_then_cancel_does_not_unpublish_snapshot`：已就绪引用仍可读。
- [ ] **Step 2: 写异步断言并红验证。** `test_async_slow_source_keeps_event_loop_responsive`：受控源等待期间另一个 coroutine 能设置事件；`test_cancelled_async_wait_keeps_prefetch_alive`：取消 waiter 后请求仍活动；`test_async_batches_have_bounded_backpressure`：消费者暂停时预取不超过一个批次；`test_sync_async_results_match`：coverage/read/错误相同。`uv run --no-sync pytest tests/unit/test_market_prefetch.py tests/unit/test_market_async.py -q`，预期 facade 缺失。
- [ ] **Step 3: 实现 facade 与共享状态。** 相同完整 requirement+refresh 的调用共享工作，各自 catalog 请求和状态；冻结 requirement 输入，动态维护消费者集合，发布同时加入的新请求要么加入事务，要么复用已发布结果完成自己的资格事务。async 阻塞操作在有界 worker 中执行，等待不占用采集 worker；取消 waiter 不传播给共享 Future。所有公开 read 先经 catalog.resolve_snapshot 确认已提交引用，再核验 manifest/对象与声明范围；不能使用仅落盘未 ready 的引用。状态/进度存于实例，不增建持久调度器；不对已发布 snapshot 执行自动回收。
- [ ] **Step 4: 绿验证。** 执行 Step 2 命令及任务 5 的竞争测试；确认快提交不等全量下载、异常包含缺口、关闭无新增任务/就绪发布、重新打开实例可离线解析旧快照。预期通过。
- [ ] **Step 5: 提交。** 暂存本任务文件；`git commit -m "feat: expose sync and async market prefetch and offline queries"`。

## Task 7: 回测集组装与工作台就绪门槛

**Files:** 创建 `src/quanto/backtest/market_dataset.py`、`tests/unit/test_market_dataset_assembly.py`、`tests/integration/test_market_workbench.py`；修改 `workbench/{data_jobs,service,store,worker,snapshots}.py`；更新 `tests/integration/{test_data_jobs,test_worker_snapshot,test_snapshot_submission}.py` 中旧 provider monkeypatch。

**Interfaces:**
- Consumes: `MarketData` 的公开 API；任务 1 的日线 dataset loaders/validators；现有工作台取消/完成登记事务。
- Produces: `build_data_requirement(payload: dict) -> DataRequirement`、`assemble_dataset(market: MarketData, snapshot: SnapshotRef, requirement: DataRequirement, research_assumptions: dict) -> DatasetBundle`，位于回测层。
- Produces: 工作台 fetch 结果保留 `state: complete | incomplete`、coverage/limitations，增加 `market_prefetch_state` 与 `market_snapshot_ref`；仅 ready 且回测 bundle 完整才有 `dataset_version_id`。一般任务队列仍使用原有状态，不引入行情状态作为 jobs.state。
- Produces: 可信 workspace 配置 `market_root`（新工作区默认 `<workspace>/market`），冻结进工作进程输入；API 普通 fetch 参数只接受标的、日期、消费者需求与 `refresh`，拒绝 provider/root/cache_version/重试参数。
- Produces: 工作台独立 `dataset_market_sources(dataset_version_id PRIMARY KEY, snapshot_id, manifest_digest)` 来源关系；与 dataset 登记同一事务写入。新运行快照记录来源引用；旧 V1/V2 的自包含组装数据照旧本地读取，无需访问行情根。

- [ ] **Step 1: 写组装回归。** 用相同固定标准样本构造旧日线 bundle 和新公开 API 组装结果，比较表、资格、订单与报告；`test_market_assembly_preserves_full_history_and_visibility` 断言预热前累计历史未截短、下一交易日存在、边界行动不遗漏、辅助 `available_at` 未提前可见。研究假设只能进入消费者 manifest，不回写成行情事实。
- [ ] **Step 2: 写工作台门槛与竞争测试并红验证。** `test_succeeded_incomplete_fetch_cannot_start_backtest`：没有登记版本且回测提交拒绝；`test_ready_market_snapshot_does_not_bypass_bundle_validation`：缺必需公司行动/状态时不登记；`test_workbench_cancel_after_market_publication_blocks_registration`：取消先提交时没有 dataset/source 关系，market ref 仍有效；`test_market_provenance_commits_with_dataset`：登记与来源同时出现。`uv run --no-sync pytest tests/unit/test_market_dataset_assembly.py tests/integration/test_market_workbench.py -q`，预期仍走旧 fetcher/缺组装接口。
- [ ] **Step 3: 实现消费者组装。** 日线包通过 read_bars/read_table 获得六张基础表和辅助表；完整历史起点明确来自所选历史范围，不由 `warmup_bars` 替代。包含 benchmark、下一交易日历和跨边界事件；研究模式可携带已披露假设，严格 raw_cash 缺事实继续拒绝。不得用报价存在制造真实 trading_status。
- [ ] **Step 4: 实现工作进程桥接。** data_jobs 只提交/查询/等待预缓存、转发进度与 cancel，随后组装并验证。service 删除随机 cache_version 和来源重试策略；父进程核验 ready 行情引用与组装数据摘要，store/worker 在登记事务再次核验任务未取消和完整结果，不能信任子进程结果里一个 complete 布尔值。行情发布与工作台登记分别有事务边界，不能宣称两库原子提交；进程重启沿用原中断处理。
- [ ] **Step 5: 绿验证并提交。** 执行 Step 2 命令及 `uv run --no-sync pytest tests/integration/test_data_jobs.py tests/integration/test_worker_snapshot.py tests/integration/test_snapshot_submission.py tests/integration/test_shared_dataset_snapshots.py tests/integration/test_api_cli_equivalence.py -q`。旧 V1/V2 冻结样本重跑、nested auxiliary、raw_cash 验证与全历史语义须通过；`git commit -m "feat: gate workbench datasets on prepared market snapshots"`，只暂存本任务列出的改动。

## Task 8: 工作台展示预缓存状态与刷新意图

**Files:** 修改 `web/src/pages/DatasetsPage.tsx`、`web/src/types.ts`、`web/tests/datasets.test.tsx`、`web/e2e/workbench.spec.ts`。

**Interfaces:**
- Consumes: 任务 7 的 fetch payload/result，不通过 UI 选择供应商或缓存路径。
- Produces: 普通重复获取默认 `refresh=false`；以“更新已有行情”复选框表达 `refresh=true`。任务详情展示准备中/已就绪/数据不完整/失败/取消、实际缺口和限制；只有登记版本才显示可用于回测。

- [ ] **Step 1: 更新当前需求对应的 UI 测试。** 删除缓存版本输入断言；`test_fetch_submits_requirement_without_cache_identity` 断言 payload 没有 cache_version/root/provider；`test_refresh_is_explicit` 断言勾选后 `refresh=true`；`test_succeeded_but_incomplete_is_not_ready` 断言可见缺口、无可用版本操作。
- [ ] **Step 2: 红验证。** `npm.cmd --prefix web run test -- --run tests/datasets.test.tsx`，预期旧输入/payload/状态展示断言失败。
- [ ] **Step 3: 实现页面与类型。** 保留现有一般任务状态，单独展示结果中的准备状态；删除 CoverageEntry.cache_version，不改无关布局或回测参数。e2e 用离线工作区验证 complete 与 incomplete，不依赖实网。
- [ ] **Step 4: 绿验证。** 执行 Step 2 命令及 `npm.cmd --prefix web run typecheck`、`npm.cmd --prefix web run build`、`npm.cmd --prefix web run test:e2e`。命令分别单独执行，预期全部通过。
- [ ] **Step 5: 提交。** 暂存本任务文件；`git commit -m "feat: show market preparation readiness and explicit refresh"`。

## Task 9: 历史导入、脚本收口与完整验收

**Files:** 创建 `scripts/{prefetch_market_data,import_market_data,benchmark_market_data}.py`、`tests/unit/test_market_scripts.py`、`tests/unit/test_market_boundaries.py`、`docs/research/2026-10-01-market-data-module-validation.md`；修改 `scripts/{prepare_strategy_research_data,prepare_round2_market_data,benchmark_storage_pipeline}.py`、`.gitignore`、`README.md`；删除 `src/quanto/data/{cache,fetcher}.py` 与旧 `scripts/fetch_research_tencent.py`，重写/删除 `tests/unit/test_fetcher_cache.py` 中过时接口断言。

**Interfaces:**
- Consumes: 公共 MarketData、标准化 import_table 与消费者 assemble_dataset；旧文件仅为显式导入输入。
- Produces: `prefetch_market_data.py --root <可信配置根> --requirement <需求JSON> [--refresh]`；返回状态、缺口和引用，不创建供应商目录。
- Produces: `import_market_data.py --root <根> --input <CSV或Parquet> --metadata <显式元数据JSON>`；metadata 包含 InstrumentId/字段映射/频率/UTC或原时区/边界/价格口径/单位/来源/未知限制/参考表需求。路径只属于管理导入工具，不进入公共查询或工作台 fetch payload；保留原文件 hash。
- Produces: benchmark 输出 JSON：输入行数、对象字节、重复/补缺请求计数与区间、完整/筛选读取耗时、批次 RSS 峰值、校验开销、工作台端到端准备时间；日线与分钟分别记录，注明硬件与样本。

- [ ] **Step 1: 审查旧测试并写替代行为断言。** `test_legacy_import_keeps_hash_units_and_unknowns`：旧CSV/Tencent目录显式导入后保留证据限制，原 bytes 不变；`test_research_acquisition_uses_module_and_never_vendor_paths`：新研究入口均通过模组，未写 `data/raw/tencent-*`；`test_data_module_has_no_consumer_imports`：AST 检查 data 不导入 config/backtest/universe/workbench/训练框架；`test_market_root_is_git_ignored`：`git check-ignore` 能识别新 catalog、对象和 evidence，小型测试样本仍可跟踪。
- [ ] **Step 2: 红验证。** `uv run --no-sync pytest tests/unit/test_market_scripts.py tests/unit/test_market_boundaries.py -q`，预期脚本不存在/旧 fetcher 违反依赖边界。
- [ ] **Step 3: 收口脚本和旧 API。** 删除新采集的 vendor 目录写入和文件复制逻辑；研究组装读取模组 snapshot，使用任务 7 消费者规则。旧研究输出不重新生成；旧测试有效覆盖已在任务 3–7 移植，再删除过时文件。管理员 CLI 显式选根，工作台通过配置选根；`.gitignore` 新增完整行情根规则但不移除已跟踪冻结样本。
- [ ] **Step 4: 文档与代表性测量。** README 提供独立 sync/async、离线导入/分钟批次、workbench 配置和备份方法；指出备份包含 catalog/manifest/引用对象/evidence。用固定离线来源测量日线及分钟；记录数据生成方式、行数/字节，不将读取微基准宣称为整体回测速比。容量外推用标的数×交易日×每日 bars×实测每行字节，分别列 32 标的和约 5000 标的的 1d/1m 情况，明确不是已获取的数据量。
- [ ] **Step 5: 完整验收。** `uv run --no-sync pytest -q`、`npm.cmd --prefix web run test -- --run`、`npm.cmd --prefix web run typecheck`、`npm.cmd --prefix web run build`、`npm.cmd --prefix web run test:e2e` 分别通过；禁网/fake transport 验证离线重跑。执行脚本 benchmark 并保存小型报告；`git diff --check`，确认实际新增行情对象不在 staged files。需要真实源抽样时只通过新模块，记录来源/时间/范围/缺口/质量，不作为自动测试前提。
- [ ] **Step 6: 提交。** 精确暂存本任务文件和删除；`git commit -m "feat: migrate market acquisition scripts and verify unified cache"`。如发现需回改前序代码，先完成对应回归并以独立修复提交处理，不混入报告提交。

## 计划自检与交付证据

设计验收的重复复用/补缺/来源切换/复权冲突/刷新归任务 4–6；异步/多等待方/租约/取消归 5–6；本地/分钟/参考表/篡改/单位/边界归 2–3；日线全历史和两层就绪门槛归 1、7；脚本、Git、容量和性能归 9。五项 Review Focus 各有明确归属测试。

执行时每任务提交前查看 `git diff` 和验证输出，仅记录真实结果。最终提交列表、测试计数、基准报告与仍未知的来源事实构成交付证据；测试全绿不能代替来源单位或复权事实的核验。实现尚未开始，本计划中的命令和预期结果均为实施步骤，不是已执行结果。
