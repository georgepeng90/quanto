# 本地深度学习量化系统设计

## 目标与授权

在 `deeplearning` 分支为现有 quanto 增加完整的分钟行情训练和模型回测流程：准备数据、训练、冻结模型、在后续时间段回测、查看与比较结果。用户确认首期支持 **1 分钟及以上 K 线**，并明确授权自行选择设计、规划和开发，无需再次确认。

采用当前 Windows PC、Python 3.13、RTX 5070 Ti（约 16 GiB 显存）、约 32 GiB 内存。检查时 D 盘约剩 27.5 GiB；数据准备需要容量检查，训练不能展开所有重叠窗口。保留用户已有未跟踪的 `docs/planning-artifacts/prd.md`。在已创建的当前分支工作，本地分阶段提交，不推送。

成功是可验证的研究闭环，不以训练后必然盈利为验收条件。真实分钟数据需要有来源的导入；现有在线供应商只提供日线。测试数据必须标注合成，不能作为投资有效性证据。

## 方案选择

选择 **共享行情库 + 独立 learning 子系统 + 现有工作台任务队列**。复用数据快照、UTC 分钟数据、Parquet、流式读取、任务监督、取消和日志；新增时间戳原生的盘中账本。现有日线研究及冻结结果继续使用现有日线引擎。

另外两种选择：直接扩展日线引擎会同时影响日期校验、T+1、利息、指标与旧结果查询；完全独立训练脚本则缺少工作台、任务及版本闭环。所选方案把新的时间语义集中在盘中模块，并通过同一工作台操作。

模型首选小型 GRU，另有线性窗口基线。使用监督学习预测未来收益，再由确定性的持仓规则及独立风险限制产生订单。首期不含强化学习、逐笔撮合、盘口排队、券商实盘连接或分布式训练。

## 数据与训练集

- 唯一行情事实来源为已提交的 MarketData 快照；训练与回测读取不触发下载。用户可复用现有显式 CSV/Parquet 导入与预缓存命令。
- 准备请求包含名称、snapshot_id、完整 DataQuery，以及 `frequency_minutes`，支持 1、5、15、30、60。原始查询为 `1m`；聚合按日历中的每段交易时段起点对齐，不能跨午休/隔夜或把缺失分钟填成行情。只有完整聚合桶可用。
- 明确保存时区、bar_start/bar_end、trading_date、标的、来源与限制；时间区间统一 UTC、左闭右开。盘中业务显示上海时间。
- 用 Arrow 批次构建分片的二维特征及行情，按标的维护有限历史；重叠窗口按需读取，禁止保存全部三维窗口。训练数据与原始行情分开，行情不写入标签或张量。
- 核心特征使用当时已完成 K 线的价格变化、K 线幅度及量额变化。特征顺序和版本固定，输入异常明确剔除并计数，不用未来回填。
- 数据准备保存校验和清单、行情覆盖、有效样本数、缺口/剔除统计、源快照及频率。读取前验证制品，处理时保持有界内存。
- 大文件导入优先按标的/月拆分；不把现有全量 pandas 导入宣称成超大单文件流式导入。操作指南给出分片导入办法。

## 训练与模型制品

- 训练请求指定训练/验证/测试三个严格有序且不重叠的时间区间，以及 lookback、horizon、模型、批大小、epochs、学习率、种子与设备。默认小型 GRU：隐藏维度 32、1 层、批大小 256；参数范围通过契约校验。
- 标签是完成信号 K 线后的下一根开盘，到指定未来 K 线收盘的对数收益。标签完整结束在所属分区内；边界样本清除，前段已知行情允许作为后段预热。不能随机拆分相邻窗口。
- 标准化只从训练分区拟合，训练时可以打乱训练样本；验证/测试保持时间顺序。早停与最佳权重只使用验证集，测试集只在权重冻结后评估。
- CPU 和 CUDA 均可执行；`auto` 明确记录所选设备，用户显式选择 CUDA 但不可用时失败。保存实际 PyTorch/CUDA/设备、随机种子及确定性设置，不声称跨硬件逐位相同。
- 制品至少包含权重、训练配置、归一化参数、特征版本/顺序、数据引用与哈希、分区、训练曲线、预测评估、训练/验证信息截止时间和全部文件哈希。权重采用 state_dict，显式 `weights_only=True` 加载。
- 保存每轮可恢复检查点（模型、优化器、epoch、随机状态和数据/配置身份）；恢复创建新任务并验证数据/配置身份，不能把旧任务状态重写为运行中。
- 支持克隆训练配置、修改参数后重新训练以及模型比较。首期参数探索通过可追踪的独立训练任务，不隐式进行无限搜索。

## 盘中回测

- 模型回测引用已成功登记的模型和数据版本；同一特征版本及频率才可连接。模型可在新数据集上推理，完整记录训练与评估两个数据引用。
- 默认要求回测起点不早于训练/验证截止；训练区间重叠回放只能显式选择研究模式并在结果醒目标注。已有多轮研究看过的历史不能称为独立的新留出证据。
- 预测只看到已完成的 K 线。订单由该根收盘信号产生，在下一根可交易 K 线开盘进行模拟；保存完整时间戳及唯一订单号。
- 使用每个标的显式的 lot_size 与 settlement_days。T+1 依交易日推进，不能依分钟推进；日历提供午休和隔夜边界。未提供事实时要求记录研究假设。
- 做多/现金组合，预测正收益排名，最多持有 top_k，只在 rebalance_every 个完成 K 线后评估；总仓位、单标的仓位和账户回撤暂停为明确上限。线性模型和简单等权/动量/现金基线采用相同数据、时钟、成本与约束。
- 成交包含手续费、最低佣金、方向性滑点、整手、现金、可卖数量，以及基于上一根已完成 K 线成交量的参与率上限。剩余未成交数量到期，并记录原因；禁止用未来整根成交量决定开盘订单规模。
- 回测是 K 线级研究模拟；不声称复现盘口、实际开盘排队、价格冲击、延迟或未取得的真实停牌/涨跌停状态。来源未知限制与使用的可交易假设必须随结果显示。
- 首期以研究价格账本执行；调整价格不重复加入公司行动现金流。原始价格若存在未处理公司行动，不得冒充真实现金账本，记录研究边界并拒绝声称 raw_cash。
- 现金利息按交易日计一次。风险检查不把分钟当交易日。保留分钟净值、信号、成交、订单与持仓，按日末净值计算年化收益和 Sharpe，同时报告盘中最大回撤。
- 回测分批写结果，不在内存积累全部历史/所有表。前端曲线降采样、表格分页；导出包含所有行和来源。

## 工作台与制品生命周期

新增 `/learning` 中文页面，覆盖训练集准备、训练配置、任务进度/取消、模型列表、模型回测、结果与基线比较。常用参数表单化，完整配置可查看；空状态解释分钟数据导入步骤。

API 以 `/api/learning` 开头，在通用资源路由之前注册：

- `GET /learning/overview`：设备、分钟快照摘要、训练集、模型、回测、相关任务；列表有上限并提供独立分页接口。
- `POST /learning/datasets`：准备训练数据，返回 JobRecord。
- `POST /learning/train`：提交训练，返回 JobRecord。
- `POST /learning/backtests`：提交模型/基线回测，返回 JobRecord。
- `GET /learning/{resource}/{id}`：数据、模型、回测详情；`resource` 为 datasets/models/backtests。
- `GET /learning/backtests/{id}/tables/{name}` 与 `/charts`：分页明细和有界曲线。
- `GET /learning/backtests/{id}/export`：可下载制品 ZIP。
- 任务日志和取消复用 `/jobs/{id}/logs`、`/jobs/{id}/cancel`。
- CLI `quanto learning` 使用相同 API，提供 overview、prepare、train、backtest、get、logs、cancel。

新增 job kinds `prepare_learning`、`train`、`model_backtest`；迁移旧 SQLite CHECK 约束并保留全部任务。新增 learning_artifacts 注册表，以 job id 为制品 id，kind 为 dataset/model/backtest。

提交时冻结源码、配置、依赖版本及输入制品引用。worker 验证引用，向 staging 写入，完成后原子发布；调度器验证产物并在同一事务内登记制品和成功状态。取消抢先提交时不能登记模型。重启任务标记 interrupted；恢复训练引用上一个检查点，保留父任务信息。

为本机限制默认单任务运行；训练不能抢占多个 GPU 作业。只有请求深度学习时才加载 torch，日线功能不需要 torch。依赖采用可选 `learning` extra；CUDA wheel 以本机实测为准并锁定。

## 内部接口边界

`quanto.learning` 内部分工：

1. `dataset.py`：`prepare_dataset(spec: dict, market_root: Path, output_dir: Path, emit, cancelled) -> dict`；`iter_dataset_bars(dataset_dir: Path, start=None, end=None)` 返回按 `(bar_end, instrument_id)` 排序的行；`read_dataset_manifest(path)` 验证并读取。
2. `training.py`：`train_model(spec: dict, dataset_dir: Path, output_dir: Path, emit, cancelled, resume_dir: Path | None = None) -> dict`。
3. `inference.py`：`iter_inference_rows(model_dir: Path, dataset_dir: Path, start, end, cancelled)` 返回行情行与 `score`（预热不足时 None）；加载时验证模型和数据身份，使用训练归一化。
4. `intraday.py`：`run_intraday_backtest(rows, spec: dict, output_dir: Path, emit, cancelled, metadata: dict) -> dict`；输入为上述按时间排序的行（预测或基线），输出分片/CSV明细与 JSON 汇总。
5. `jobs.py`：连接上述函数，冻结输入引用、使用工作台 RunEvent 日志、包装哈希收据；`service.py`/`api.py` 管理提交和查询。

跨模块公开行至少包含 `instrument_id, trading_date, bar_start, bar_end, open, high, low, close, volume, amount`；时间值为 UTC 可解析值，训练特征不要求交易引擎了解。

## 验收

1. 测试先验证当前需求：行情时区/聚合/缺口、训练归一化和标签边界、模型重载预测一致、制品篡改、下一根成交、T+1、午休/隔夜、费用和日级指标。
2. 用有明确标注的分钟夹具在 CPU 跑通准备→训练→冻结→回测→查询；在本机 CUDA 跑通真实张量训练和推理，记录实际环境与限制。
3. 任务取消/恢复/失败、旧数据库迁移、最终发布竞争、API/CLI与页面闭环均有测试。
4. 大样本基准记录样本数、内存、时间、磁盘和吞吐；不能以微型测试宣称千万样本验收。数据量不足时报告实际验证规模。
5. Python 相关及完整回归、前端测试、类型检查、构建和浏览器闭环通过；阶段性检查后立即本地提交。

## 参考

- [PyTorch 本机安装](https://pytorch.org/get-started/locally/)
- [PyTorch DataLoader](https://docs.pytorch.org/docs/stable/data.html)
- [PyTorch 权重与检查点](https://docs.pytorch.org/tutorials/beginner/saving_loading_models)

本设计依据当前源码和五轮研究记录；外部资料用于库接口，不构成任何收益证据。
