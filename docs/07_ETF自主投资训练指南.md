# ETF 自主投资训练指南

当前研究主线是 ETF 中长线自主投资：用日线观察，以自然周或月决策，允许连续持有和现金等待。本机 Windows 工作台提供 PPO 训练、验证选模、测试评估和冻结策略回测，不接入券商实盘。已有“分钟收益预测”仍是分钟监督学习工具，预测收益与自主学习投资动作是两项不同研究。

## 成熟框架与安装

训练直接使用 Stable-Baselines3 2.9.0 的 `PPO`、PyTorch 和标准 Gymnasium 环境；项目只适配 ETF 观察、日线账户及实验管理。策略梯度、优化器、采样缓存和训练循环由 SB3 提供，成交账本、风险约束、数据版本和任务生命周期沿用现有 quanto 服务。

从当前 checkout 根目录执行：

```powershell
uv sync --locked --extra dev --extra web --extra data --extra learning
npm.cmd --prefix web ci
npm.cmd --prefix web run build
.\scripts\start-workbench.ps1
```

强化学习依赖归在 `learning` extra。默认小型 MLP 使用 `device: "cpu"`；可显式选择 `cuda`，须先确认运行环境支持。CPU 能完成首轮研究，显卡利用率不是训练效果指标。运行时冻结依赖版本，提交后修改依赖应重新提交任务。

## 观察与交易语义

固定且有序的 `backtest.symbols` 是策略身份的一部分。模型训练与冻结推断使用同一顺序，不能通过交换代码或替换为未见 ETF 来宣称迁移能力。

当前特征版本为 `daily-etf-v2`，每个 ETF 有 12 个特征：5/20/60/历史窗口的对数价格变化、近 20 日波动、窗口回撤、成交额相对均值、当日高低价幅度、当日报价标记、允许新增标记、实际持仓权重、可卖份额比例。组合另含现金比例、应收比例、风险暂停状态和组合回撤，合计 `12 × ETF 数量 + 4` 维。只使用当前收盘及之前的数据，固定裁剪，不拟合全样本归一化；观察不含 episode 剩余天数、区间结束日期或倒计时。

`lookback` 默认 120 根历史日线，接口范围 3–512。允许新增还要求当前报价与足够历史；不足窗口的标的不可新增。数据版本应保留训练起点之前的历史。每段首次可执行决策至少须有一个篮子标的达到窗口长度；这不代表其他标的已满足交易资格。

自然周/月边界按完整交易日历识别，节假日不会变成固定每 5/20 日调仓。只有区间内还有下一执行交易日，才开放动作：周期末收盘发出信号，下一交易日开盘撮合。区间截断不会伪装成周期末，结束持仓按收盘估值，不强制卖出。

动作首维选择 Hold 或目标组合；其余维为每个 ETF 的 0%、25%、50%、75%、100% 目标权重。合计超过 100% 时按比例缩放，再经总仓位、单标的、行业与压力风险约束。Hold 保持实际份额，不重复再平衡；目标权重全为零表示现金目标。只做多、不加杠杆，未投资部分留作现金。

虽然自主决策是周/月频率，账户每天执行风险检查、结算、利息、公司行动和估值。风险处置可能在周期之外减仓；因此“周度策略”不保证一周只成交一次，“买入持有基准”也受同样的风险约束。

一步覆盖本次决策至下一决策或末尾，奖励为该段扣费后账户净值的对数变化。佣金与滑点已进入净值，不再重复扣费。默认 `gamma: 1`，累计奖励对应净值对数增长；episode 重置前的浮亏仍计入末尾净值。

## 三段日期与训练规模

训练、验证、测试均为 `YYYY-MM-DD` 日历日期闭区间，必须严格满足：

```text
train_start < train_end < validation_start < validation_end < test_start < test_end
```

全部日期须在 `backtest.start..end` 和数据版本覆盖内。各段须包含真实自然周/月决策及其后续执行交易日，不能仅用几天日期凑出三个分区。

训练只从训练段抽取连续 episode，片段内按时间推进；换片段保留模型权重、重置账户。`episode_sessions` 默认 252 个交易日，不能超过训练段实际交易日数，片段必须含可执行决策。验证与测试连续回放完整区间，不随机切片。

`total_timesteps` 默认 4096，计数单位是环境交互步，通常跨一周或一月，并非交易日或优化轮数。SB3 以完整 rollout 采样，实际步数可能超过请求值。`n_steps: 128`、`batch_size: 64`、`n_epochs: 10` 表示每轮采样规模、优化批次及优化次数；批大小不得超过采样步数且必须整除它。`eval_every_timesteps: 512` 的验证在完成 PPO 更新之后执行。

选模标准固定为 `validation_net_log_return`：比较各轮验证净对数收益，保留最佳权重。训练结束加载最佳权重，再评估一次测试段；本次测试不参与权重更新或 checkpoint 选择。产物记录验证轮次、最佳步数、实际步数、验证/测试指标、配置、数据内容哈希和版本信息。

## 网页操作

1. 在“数据版本”准备并登记日线数据，核对覆盖、价格口径、ETF 代码和来源限制。
2. 打开“ETF 自主投资”，选择版本；页面读取版本中的标的作为篮子，可按研究范围缩小并固定顺序。
3. 填写策略名称、自然周/月、资金和三段日期。高级参数提供历史窗口、片段长度、PPO、设备、成本及风险设置，提交前展开检查完整配置。
4. 提交训练，在任务区查看状态、日志或取消。取消训练不登记成功策略；成功后查看冻结策略的验证与测试报告。
5. 选择参考冻结策略、版本、回测区间、方法及成本倍数，提交冻结回测。开始日期必须严格晚于该策略的 `validation_end`。
6. 查看每日净值、订单、成交、信号和持仓，下载完整 ZIP；比较前确认版本、日期、成本和实验配置一致。

冻结回测仅执行确定性 `predict`，不调用 `learn`。`policy`、`cash`、`equal_weight`、`buy_hold` 分别为冻结策略、现金、周期等权、首次决策买入后持有。所有基准都必须引用 `policy_id`，沿用该实验的固定篮子、资金、频率、成交与风险配置。成本倍数默认 1，可显式进行压力实验，报告保留实际倍数。

## API 与 Agent CLI

浏览器、Agent 和 CLI 使用同一套本机 API 与队列，没有另一条绕过版本校验的训练入口。Agent 可调用标准 HTTP 或 `quanto investing`。认证沿用工作台令牌：

```powershell
$session = Invoke-RestMethod http://127.0.0.1:8765/api/session
$env:QUANTO_TOKEN = $session.token
uv run --no-sync quanto investing overview
```

[weekly.json](../examples/investing/weekly.json) 是完整训练 **spec**，不是提交请求。先按实际版本修改日期、篮子与覆盖，再包装 `dataset_id`；示例范围不表示当前版本已有足够数据。以下写入无 BOM 的请求文件，供 CLI 读取：

```powershell
$spec = Get-Content examples/investing/weekly.json -Raw | ConvertFrom-Json
$request = @{ dataset_id = '替换为日线版本ID'; spec = $spec } | ConvertTo-Json -Depth 30
[IO.File]::WriteAllText((Join-Path $PWD 'train-request.json'), $request, [Text.UTF8Encoding]::new($false))
uv run --no-sync quanto investing train --json train-request.json
uv run --no-sync quanto investing logs <训练任务ID>
uv run --no-sync quanto investing get policies <训练任务ID>
uv run --no-sync quanto investing cancel <运行中任务ID>
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
uv run --no-sync quanto investing backtest --json backtest-request.json
uv run --no-sync quanto investing get backtests <回测任务ID>
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

训练冻结源码、请求、数据内容和依赖，策略文件只从本地已校验登记产物加载。保存 `policy.zip`、验证历史和 manifest；不接受任意外来模型路径。SB3 save/load 不等于恢复环境账户、随机数及 rollout 状态，本版不提供精确断点续训、自动 walk-forward、自动超参搜索或跨实验密封测试。用户或 Agent 反复查看测试期仍可能形成研究泄漏，应在新研究开始前固定评估设计。

小型合成或真实日线 smoke run 验证的是 learn/save/load、账户一致性、API 与产物闭环，不证明训练充分、策略有效或未来盈利。收益、回撤和基准差异须在足够长且独立的样本中另行研究。

官方参考：[SB3 PPO](https://stable-baselines3.readthedocs.io/en/master/modules/ppo.html)、[SB3 保存格式](https://stable-baselines3.readthedocs.io/en/master/guide/save_format.html)、[Gymnasium 自定义环境](https://gymnasium.farama.org/introduction/create_custom_env/)。项目需求与约束见[ETF 自主投资设计](superpowers/specs/2026-10-03-etf-policy-design.md)。
