# ETF 周/月自主投资系统验收

日期：2026-10-03；Windows 本机，分支 `deeplearning`。本记录验证软件训练与回测流程，不认定策略具备投资有效性。

## 实现与成熟组件

- Stable-Baselines3 2.9.0 PPO / MlpPolicy，Gymnasium 1.3.0，PyTorch 2.11.0+cu128，Python 3.13.12；CPU 为默认训练设备。
- 环境复用 quanto 日线 SessionRunner、PortfolioLedger、下一开盘执行和每日 RiskPolicy。未另写 PPO、采样缓存或成交模拟器。
- 观察版本 `daily-etf-v2`，动作版本 `hold-target-quartiles-v1`。移除实验剩余期限输入，延长回测终点不改变相同历史与账户状态下的当前观察。
- 周/月训练，按验证净对数收益选模，选定模型仅评估一次测试段；冻结回测没有 learn 或归一化更新。

## 行为验证与审查

全量 Python 回归：618 passed、1 skipped（232 秒）；两个既有纳秒时间转换警告。随后正式旧工作区暴露出 SQLite `ALTER TABLE` 后表名带引号的迁移情况，新增复现并修复；学习与投资接口专项 23 passed（33.68 秒），包括旧五类任务表、分钟任务表、重命名后的任务表与重复打开，原记录保留。

前端全量 126 passed；布局与依赖提示修正后投资页面专项 7 passed，TypeScript/生产构建通过。既有 canvas 测试环境提示和大型图表 chunk 提示保留。

覆盖：官方 Gymnasium/SB3 环境检查、旧策略与固定环境动作账本一致性、未来价格/拆分扰动、周/月节假日边界、下一开盘成交、Hold、现金退出、每日风险、停牌、公司行动价格口径、终点估值、奖励与每日净值守恒、PPO 权重实际更新、梯度更新后选模、save/load 一致、冻结模型哈希不变、取消不登记、请求/产物篡改拒绝。

独立审查分别检查环境/账本、PPO/评估、服务/作业，修复了剩余期限观察和非法 ID 类型导致 500 的问题。修改测试期 OHLC 后重新训练，同种子的验证历史、选模步数和全部策略张量完全一致。

## 可复现实跑

```powershell
uv run --no-sync python -m scripts.verify_investing_system --frequency weekly --timesteps 128
uv run --no-sync python -m scripts.verify_investing_system --frequency monthly --timesteps 64
uv run --no-sync python -m scripts.verify_investing_system --dataset-dir <已登记日线包目录> --training-json <训练spec.json>
```

脚本默认复用项目原有合成样本生成器：6 个 ETF、4 年工作日、6,048 条价格。隔离工作区与行情配置，不下载行情或发送真实交易。每次真实运行子进程训练、保存/加载、四种冻结对照、每日表/曲线/ZIP、产物哈希与奖励核对。`--training-json` 是完整 spec，提供时以文件中的步数/频率为准。

| 数据与频率 | 决策训练步数 | 训练任务耗时 | 完整流程 | 采样峰值 RSS | 产物与临时文件峰值 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 合成日线，周度 | 128 | 14.68 秒 | 34.61 秒 | 0.82 GiB | 8.38 MiB |
| 合成日线，月度 | 64 | 18.86 秒 | 39.09 秒 | 0.82 GiB | 7.69 MiB |
| 已有真实研究日线，周度 | 128 | 34.29 秒 | 79.24 秒 | 0.94 GiB | 13.84 MiB |

RSS 采样覆盖验收进程和其 worker 子进程；不包含已有服务、全局依赖及整个共享行情库。并行验收可能影响耗时，不能外推大规模训练吞吐。

本机原始报告（Git 忽略，保留供检查）：

- `outputs/investing-acceptance/weekly-o3xnctej/acceptance.json`
- `outputs/investing-acceptance/monthly-ambell7x/acceptance.json`
- `outputs/investing-acceptance/weekly-f7su2dar/acceptance.json`（真实日线）

## 真实日线实跑范围

复用既有研究版本 `be1ead6f-dcf6-40ab-9dde-2f69caec2db1`，来源为 2026-10-01 冻结的 Tencent newfqkline 响应；选择 `512660,512880,512800,159928,512400,512580` 固定篮子。训练 2019-01-01..2023-12-31，验证 2024 全年，测试 2025-01-01..2026-09-30，历史窗口 120 日，episode 252 日，CPU、128 步、32 步 rollout、16 批次、2 轮优化、32 隐藏维度、种子 42。

以下仅是该短程运行输出；未根据测试表现反复调参。

| 冻结方法 | 测试期总收益 | 最大回撤 | 成交笔数 | 换手倍数 |
| --- | ---: | ---: | ---: | ---: |
| PPO | 15.61% | 13.93% | 852 | 7.33 |
| 现金 | 0.00% | 0.00% | 0 | 0.00 |
| 周度等权 | 16.17% | 16.53% | 1,129 | 2.23 |
| 受相同风控约束的买入持有 | 17.01% | 19.99% | 222 | 0.79 |

PPO 本次收益低于两个持仓基准，回撤较低、换手较高；这些数值不足以评价方法优劣。买入持有依然接受每日风险修正，不能当成没有交易约束的纯买持。数据为 adjusted_research，供应商返回 day/qfqday 混合、复权语义未逐条独立核验、非完整公司行动、样本幸存偏差、单位和交易状态研究假设等限制均随原 manifest 保留。

## 浏览器与正式工作区

隔离页面 `8772/investing` 实际完成表单提交训练并成功、提交冻结回测并成功、查看验证/测试指标、净值图、成交表与翻页。浏览器控制台未发现 warning/error；在 870 像素宽窗口确认页面无横向溢出，宽表在内部滚动。日期控件用分段键盘输入验证；本浏览器工具的 date fill 未触发 React 状态更新，未为自动化添加产品特殊逻辑。ZIP 内容通过真实 API 解包和 CRC 检查，未把浏览器下载事件当作已验证结果。

正式服务使用原 `data/deeplearning` 工作区与 `127.0.0.1:8770/investing`。已登记上述真实日线版本副本 `046fdb69-9a0a-4de9-acfd-576b3370cfaf`，不更改原研究工作区。短程模型及四个对照也通过正式 HTTP API 运行成功：

- 策略：`133b180c-227c-440a-9b57-3780a7068d69`
- PPO：`a22ae61e-d0d1-4ee1-9684-daa30d1c5652`
- 现金：`2e2871cb-3764-432b-ae2b-db06964ecea1`
- 等权：`26015d59-1cc7-480c-b063-96f8d7f002e7`
- 买入持有：`de176bb1-a29b-4b42-9aaa-2bfe40a2f595`

正式流程请求与任务 ID 留在 `outputs/investing-live-report.json`。默认完整研究配置见 `examples/investing/weekly.json`，需按独立研究设计扩充训练预算、种子和样本外评估；本版不提供自动 walk-forward、精确续训或券商执行。
