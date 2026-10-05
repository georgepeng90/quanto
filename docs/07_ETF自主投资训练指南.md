# 固定 ETF 篮子模型训练指南

当前研究主线是固定 ETF 篮子的中长线自主投资：用日线观察，以自然周或月决策，允许连续持有和现金等待。每个模型绑定一个有序篮子，同一模型通过反复回放历史片段累积学习；不同篮子创建不同模型。工作台提供本地模型、追加训练、验证选模和指定版本回测，不接入券商实盘。

主导航只有“行情数据、固定策略、强化学习模型、回测”。模型入口为 `/models`；回测统一在 `/runs` 发起和查看。旧 PPO 与分钟回测保留在历史目录，旧结果链接仍可阅读报告和导出；分钟训练退出主流程。

## 模型、训练与保存版本

创建模型只保存名称、数据版本、固定篮子和研究配置，状态为“待训练”，不会启动训练。篮子须显式填写且顺序固定；选择数据版本不会自动填入全部 ETF。Agent 可以通过同一 API 创建模型，ETF 选择标准由具体研究任务决定。

首次开始训练初始化 PPO。一次训练会话内随机抽取历史片段，重置模拟账户、保留模型参数并反复学习。完成后自动保存不可变版本；再次“继续训练”使用最后成功版本的 `last.zip` 恢复参数、优化器和累计步数，追加指定的步数预算。每次预算是新增量，不是累计目标。一次会话可以包含很多训练片段，没有自动无限排队任务。

每个成功版本同时包含 `last.zip`（继续学习的最终状态）和 `policy.zip`（截至该会话的验证最优快照）。如果本次验证没有改善，最优快照保持不变，但最终状态与累计训练步数继续推进。保存版本可用于回测，活动训练不会覆盖旧版本。恢复会重置账户与采样环境，不承诺与完全不中断运行逐位一致。

同一模型同时只能有一个排队或运行中的训练会话。取消或异常中断不会发布本次版本；本次未发布的进度会丢弃，已有成功版本保持可用，下次从最近成功版本继续。需要保留阶段成果时使用合适的追加预算，让会话正常完成。

模型的数据、篮子顺序、频率、历史窗口、网络结构、三段日期、成本、风控和选模规则创建后固定。续训仅可改变新增步数与设备。换篮子或研究条件时创建新模型。旧的独立 PPO 制品没有 `last.zip`，仍可查阅和回测，但不会被自动转换为可续训模型。

## 成熟框架与安装

训练直接使用 Stable-Baselines3 2.9.0 的 `PPO`、PyTorch 和标准 Gymnasium 环境；项目只适配 ETF 观察、日线账户及实验管理。策略梯度、优化器、采样缓存和训练循环由 SB3 提供，成交账本、风险约束、数据版本和任务生命周期沿用现有 quanto 服务。

从当前 checkout 根目录执行：

```powershell
uv sync --locked --extra dev --extra web --extra data --extra learning
npm.cmd --prefix web ci
npm.cmd --prefix web run build
.\scripts\start-workbench.ps1
```

本机启动脚本读取 `workbench.local.json`，统一使用 `http://127.0.0.1:8770` 和 `data/research-20261001`；所有实验、训练和回测共用此工作区。新安装未配置时仍使用通用默认值，API 示例端口应与实际服务一致。

强化学习依赖归在 `learning` extra。默认小型 MLP 使用 `device: "cpu"`；可显式选择 `cuda`，须先确认运行环境支持。CPU 能完成首轮研究，显卡利用率不是训练效果指标。运行时冻结依赖版本，提交后修改依赖应重新提交任务。

## 观察与交易语义

固定且有序的 `backtest.symbols` 是策略身份的一部分。模型训练与冻结推断使用同一顺序，不能通过交换代码或替换为未见 ETF 来宣称迁移能力。

每个 ETF 有 12 个特征：5/20/60/历史窗口的对数价格变化、近 20 日波动、窗口回撤、成交额相对均值、当日高低价幅度、当日报价标记、允许新增标记、实际持仓权重、可卖份额比例。组合另含现金比例、应收比例、风险暂停状态和组合回撤，合计 `12 × ETF 数量 + 4` 维。只使用当前收盘及之前的数据，固定裁剪，不拟合全样本归一化；观察不含 episode 剩余天数、区间结束日期或倒计时。

`lookback` 默认 120 根历史日线，接口范围 3–512。允许新增还要求当前报价与足够历史；不足窗口的标的不可新增。数据版本应保留训练起点之前的历史。每段首次可执行决策至少须有一个篮子标的达到窗口长度；这不代表其他标的已满足交易资格。

固定周期特征独立读取所需历史：5/20/60 日收益分别需要 6/21/61 个收盘价，20 日波动使用 21 个收盘价，成交额均值使用 20 根日线，不随较短的 `lookback` 缩短。历史窗口收益和回撤仍只使用最近 `lookback` 根日线。若标的实际历史不足固定周期，则使用截至当前已有的历史，不补齐、不读取未来数据；入场资格仍按 `lookback` 判断，这类短历史特征不代表完整周期。

`lookback < 61` 的模型采用修正后的 `daily-etf-v3` 特征版本；此前同类 `daily-etf-v2` 模型不能直接续训或按新口径回测，需要新建模型训练，已有结果保留。`lookback >= 61` 的观察值未改变，继续使用 `daily-etf-v2`，原有模型可继续训练和回测。

自然周/月边界按完整交易日历识别，节假日不会变成固定每 5/20 日调仓。只有区间内还有下一执行交易日，才开放动作：周期末收盘发出信号，下一交易日开盘撮合。区间截断不会伪装成周期末，结束持仓按收盘估值，不强制卖出。

新建模型默认使用 `action_version: "hold-target-cash-quartiles-v2"`：动作首维 `0` 为 Hold、`1` 为目标组合、`2` 为现金目标。Hold 保持实际份额，不重复再平衡；现金目标独立选择清仓，忽略后续权重维，不再要求所有 ETF 同时输出零权重。它通过原有下一开盘订单与账本执行，仍受停牌、可卖份额、结算和费用约束，不保证立即全部卖出。

目标组合的其余维仍为每个 ETF 的 0%、25%、50%、75%、100% 目标权重；合计超过 100% 时按比例缩放，再经总仓位、单标的、行业与压力风险约束。目标权重全为零也表示现金目标。只做多、不加杠杆，未投资部分留作现金。增加明确现金动作只改善现金选择的可达性，不解决大篮子的部分仓位或稀疏持仓表达问题。

动作版本在模型创建后固定，训练、验证、续训和回测都按该版本解释。已有配置缺少此字段时按 `hold-target-quartiles-v1` 读取，保留 `0` 为 Hold、`1` 为目标组合的原有两种动作；新建 API 请求省略字段时默认写入 v2。可显式指定 v1 创建对照模型。旧模型不能原地切换动作空间，要使用新现金动作须创建新模型；原有特征版本校验仍然适用。

虽然自主决策是周/月频率，账户每天执行风险检查、结算、利息、公司行动和估值。风险处置可能在周期之外减仓；因此“周度策略”不保证一周只成交一次，“买入持有基准”也受同样的风险约束。

一步覆盖本次决策至下一决策或末尾，奖励为该段扣费后账户净值的对数变化。佣金与滑点已进入净值，不再重复扣费。默认 `gamma: 1`，累计奖励对应净值对数增长；episode 重置前的浮亏仍计入末尾净值。

## 三段日期与训练规模

训练、验证、测试均为 `YYYY-MM-DD` 日历日期闭区间，必须严格满足：

```text
train_start < train_end < validation_start < validation_end < test_start < test_end
```

全部日期须在 `backtest.start..end` 和数据版本覆盖内。各段须包含真实自然周/月决策及其后续执行交易日，不能仅用几天日期凑出三个分区。

训练只从训练段抽取连续 episode，片段内按时间推进；换片段保留模型权重、重置账户。`episode_sessions` 默认 252 个交易日，不能超过训练段实际交易日数，片段必须含可执行决策。验证与测试连续回放完整区间，不随机切片。

片段末尾继续返回 `terminated=True, truncated=False`，保留当前重置账户、有限片段净值增长的训练目标，不在任意截断日加入价值自举。是否改为连续任务，需要结合片段长度对照结果，统一设计决策边界、折扣和自举状态；目前不改变 `gamma` 或终止语义。

`total_timesteps` 默认 4096，计数单位是环境交互步，通常跨一周或一月，并非交易日或优化轮数。SB3 以完整 rollout 采样，实际步数可能超过请求值。`n_steps: 128`、`batch_size: 64`、`n_epochs: 10` 表示每轮采样规模、优化批次及优化次数；批大小不得超过采样步数且必须整除它。`eval_every_timesteps: 512` 的验证在完成 PPO 更新之后执行。

选模标准在创建时固定，可选 `validation_net_log_return`（验证净对数收益）或 `validation_sharpe`（验证期每日净值夏普率）。新模型页面默认夏普率，同时展示收益与回撤；零波动等情况下夏普率无定义，显示为空，不伪造为 0。产物记录 `selection_valid`，不能把无有效评分的快照说成已验证最优。

新模型会话只进行训练和验证，不在每次续训结束自动查看测试段。测试段通过明确选择保存版本进行冻结回测；频繁依据测试结果反复调整研究仍会泄漏测试信息。原独立 `/investing/train` API 的历史流程保留其完成后评估一次测试段的语义。

### 动作与片段长度对照

`scripts/compare_policy_actions.py` 只生成请求和汇总结果，不启动训练。默认比较 v1/v2 两种动作、126/252/504 个交易日片段和 17/42/83 三个固定种子，共 18 组。每组创建独立模型，只训练一次；同一组种子的两种动作使用相同数据、日期、费用、风控、网络、选模规则和 PPO 步数预算。

先保存一份本次实验专用的 protocol JSON，含 `dataset_path` 和完整 `spec`。`dataset_path` 必须指向工作区已登记的冻结数据目录；`dataset_id` 和 `dataset-content-hash` 使用该版本的登记信息。工具会校验目录文件哈希，原始 CSV 导入目录不能冒充登记后的 Parquet 版本。所有组使用同一份代码和依赖，保留生成的矩阵、协议和训练任务记录。矩阵记录准备时的 Git 提交，汇总检查产物中的环境版本和运行依赖；要确认实际训练源码一致，仍须核对任务冻结输入，单凭导出的指标 JSON 不能证明这一点。

```powershell
uv run --no-sync python scripts/compare_policy_actions.py prepare --protocol comparison-protocol.json --dataset-id <数据版本ID> --dataset-content-hash <登记的内容哈希> --output outputs/action-comparison --budget 4096
```

预算必须整除 `n_steps`，以免 PPO 完整采样轮次造成实际交互数不同。工具先检查全部片段长度，再写出每组的 `create-model.json`、`train-model.json` 和总表 `matrix.json`。原 2022–2023 两年协议只有 484 个交易日，会拒绝 504 日片段；若延长训练区间，必须为所有组统一重新固定日期，不能拿新的 504 日结果与旧区间训练结果直接比较。`window_start_positions` 记录各片段长度的候选起点数上限，尚未过滤自然决策边界；接近整段历史的长片段起点很少，解读结果时同时考虑样本重复程度。

对每组依次使用 `quanto investing create-model --json <组目录>/create-model.json` 和 `quanto investing train-model <新模型ID> --json <组目录>/train-model.json`，查询任务直到成功，再通过 `quanto investing get policies <训练任务ID>` 导出 JSON，按组 ID 命名为 `<组ID>.json` 存入单独目录。不要使用独立 `investing train` 或在此阶段发起测试段回测。

```powershell
uv run --no-sync python scripts/compare_policy_actions.py aggregate --matrix outputs/action-comparison/matrix.json --manifests outputs/action-comparison-results --output outputs/action-comparison-summary.json
```

汇总核对每组恰好一份结果、固定配置和数据哈希一致、从零开始且实际完成相同预算、测试指标显式为空；输出逐组验证指标和同一种子下 v2 减 v1 的差值。平均现金比例按全部验证交易日的 `cash / equity` 取均值，应收款不算现金；换手和交易成本沿用日线账本口径。夏普率无定义时保持空值，不补零。检查多个种子的收益、回撤、现金、换手和费用后再决定是否采用新配置；这些对照结果仍属于验证集研究，不能代替最终独立测试。

## 网页操作

1. 在“行情数据”准备并登记日线数据，核对覆盖、价格口径、ETF 代码和来源限制。
2. 打开“强化学习模型”，点击“创建模型”，显式填写有序篮子、名称、自然周/月、资金和三段日期；检查折叠的高级参数。创建不会自动开始学习。
3. 进入模型详情，检查身份与配置，指定本次新增训练步数，点击“开始训练”或“继续训练”。查看训练记录、日志和状态；已有版本保留。
4. 在“保存版本”查看累计训练步数、验证指标及快照来源，点击“使用此版本回测”。
5. 在“回测 → 新建回测”选择固定策略实验，或强化学习模型及其明确的保存版本。模型篮子继承且只读；回测开始日期必须严格晚于版本的 `validation_end`。
6. 在回测历史统一查看固定策略、PPO 和历史分钟结果，按指标筛选或勾选比较。页面会列出不同区间、ETF 范围、数据、成本和风控的差异；条件不同不能直接排名。
7. 点击结果名称进入每日净值、订单、成交、信号和持仓，下载完整 ZIP。全部功能共用一个工作区。

冻结回测仅执行确定性 `predict`，不调用 `learn`。`policy`、`cash`、`equal_weight`、`buy_hold` 分别为冻结策略、现金、周期等权、首次决策买入后持有。所有基准都必须引用 `policy_id`，沿用该实验的固定篮子、资金、频率、成交与风险配置。成本倍数默认 1，可显式进行压力实验，报告保留实际倍数。

## API 与 Agent CLI

持久模型的接口如下，创建请求沿用下文完整 spec 模板：

| HTTP 接口 | 用途 |
| --- | --- |
| `GET /api/investing/models` | 分页列出模型，`cursor`、`limit` |
| `POST /api/investing/models` | `{dataset_id, spec}`；只创建，不训练 |
| `GET /api/investing/models/{id}` | 固定配置、训练记录、保存版本 |
| `POST /api/investing/models/{id}/train` | `{additional_timesteps, device}`；从最近成功状态追加训练 |
| `POST /api/investing/models/{id}/backtests` | `{dataset_id, version_id, spec}`；指定本模型的保存版本回测 |

模型回测 spec 仍为 `{start,end,baseline,cost_multiplier}`；`version_id` 是成功训练任务 ID，不能填写模型 ID 或“latest”。返回的任务仍通过共用 `/api/jobs` 查询、日志及取消接口管理。

```powershell
uv run --no-sync quanto --url http://127.0.0.1:8770 investing models
uv run --no-sync quanto --url http://127.0.0.1:8770 investing create-model --json create-model.json
uv run --no-sync quanto --url http://127.0.0.1:8770 investing get models <模型ID>
uv run --no-sync quanto --url http://127.0.0.1:8770 investing train-model <模型ID> --json additional-training.json
uv run --no-sync quanto --url http://127.0.0.1:8770 investing model-backtest <模型ID> --json model-backtest.json
```

`additional-training.json` 示例为 `{"additional_timesteps":4096,"device":"cpu"}`。这些命令是操作说明，不会自动执行；ETF 篮子和日期应在研究前明确。

### 原独立策略 API 与完整 spec 模板

浏览器、Agent 和 CLI 使用同一套本机 API 与队列，没有另一条绕过版本校验的训练入口。Agent 可调用标准 HTTP 或 `quanto investing`。认证沿用工作台令牌：

```powershell
$session = Invoke-RestMethod http://127.0.0.1:8770/api/session
$env:QUANTO_TOKEN = $session.token
uv run --no-sync quanto --url http://127.0.0.1:8770 investing overview
```

[weekly.json](../examples/investing/weekly.json) 是完整训练 **spec**，不是提交请求。先按实际版本修改日期、篮子与覆盖，再包装 `dataset_id`；示例范围不表示当前版本已有足够数据。以下写入无 BOM 的请求文件，供 CLI 读取：

```powershell
$spec = Get-Content examples/investing/weekly.json -Raw | ConvertFrom-Json
$request = @{ dataset_id = '替换为日线版本ID'; spec = $spec } | ConvertTo-Json -Depth 30
[IO.File]::WriteAllText((Join-Path $PWD 'train-request.json'), $request, [Text.UTF8Encoding]::new($false))
uv run --no-sync quanto --url http://127.0.0.1:8770 investing train --json train-request.json
uv run --no-sync quanto --url http://127.0.0.1:8770 investing logs <训练任务ID>
uv run --no-sync quanto --url http://127.0.0.1:8770 investing get policies <训练任务ID>
uv run --no-sync quanto --url http://127.0.0.1:8770 investing cancel <运行中任务ID>
```

改成月度时将 `frequency` 设为 `monthly`，并确认三段和训练片段均包含可执行月末决策。不要把训练片段长度换算为固定每 20 日动作。

回测请求文件的结构为：

```json
{
  "dataset_id": "日线版本ID",
  "policy_id": "已成功训练的策略任务ID",
  "spec": {
    "start": "2025-01-01",
    "end": "2026-09-30",
    "baseline": "policy",
    "cost_multiplier": 1
  }
}
```

```powershell
uv run --no-sync quanto --url http://127.0.0.1:8770 investing backtest --json backtest-request.json
uv run --no-sync quanto --url http://127.0.0.1:8770 investing get backtests <回测任务ID>
```

这里的日期以示例验证截止 `2024-12-31` 为前提；实际应改成所选策略和版本允许的区间。换用现金等基准时仍保留 `policy_id`。

| HTTP 接口 | 用途 |
| --- | --- |
| `GET /api/investing/overview` | 环境、日线版本、策略、回测、任务 |
| `POST /api/investing/train` | `{dataset_id, spec}` |
| `POST /api/investing/backtests` | `{dataset_id, policy_id, spec}` |
| `GET /api/investing/policies/{id}` | 策略配置及验证/测试报告 |
| `GET /api/investing/backtests/{id}` | 每日回测报告 |
| `GET /api/investing/backtests/{id}/tables/{name}` | 明细；支持 `cursor`、`limit` |
| `GET /api/investing/backtests/{id}/charts` | 有界净值点；支持 `max_points` |
| `GET /api/investing/backtests/{id}/export` | 完整 ZIP |
| `GET /api/jobs/{id}/logs`、`POST /api/jobs/{id}/cancel` | 共用日志与取消 |

HTTP 请求带 `Authorization: Bearer <token>`。结果表支持 `equity`、`daily_equity`、`orders`、`fills`、`signals`、`positions`；自主投资的两种 equity 表均是每日数据。列表还可通过 `GET /api/investing/policies` 或 `/backtests` 分页读取。

## 数据与研究边界

日线版本保留 `raw_cash` 与 `adjusted_research` 区别。研究复权价格不会再重复加入公司行动现金流，但供应商复权锚点、单位、可知时间等限制仍随报告保存；它们不能被解释为完整历史可知性。未知行情缺口拒绝，确认停牌按现有陈旧估值上限处理；整手、可卖份额和结算约束继续生效。

训练冻结源码、请求、数据内容和依赖，策略文件只从本地已校验登记产物加载。保存参数、优化器、验证历史和 manifest；不接受任意外来模型路径。支持成功会话之间继续训练，但不恢复环境账户、随机数及未完成 rollout 状态，不提供中断会话未发布进度的精确恢复、自动 walk-forward、自动超参搜索或跨实验密封测试。用户或 Agent 反复查看测试期仍可能形成研究泄漏，应在新研究开始前固定评估设计。

小型合成或真实日线 smoke run 验证的是 learn/save/load、账户一致性、API 与产物闭环，不证明训练充分、策略有效或未来盈利。收益、回撤和基准差异须在足够长且独立的样本中另行研究。

官方参考：[SB3 PPO](https://stable-baselines3.readthedocs.io/en/master/modules/ppo.html)、[SB3 保存格式](https://stable-baselines3.readthedocs.io/en/master/guide/save_format.html)、[Gymnasium 自定义环境](https://gymnasium.farama.org/introduction/create_custom_env/)。项目需求与约束见[ETF 自主投资设计](superpowers/specs/2026-10-03-etf-policy-design.md)。
