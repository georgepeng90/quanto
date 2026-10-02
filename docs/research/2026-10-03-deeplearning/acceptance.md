# 本机深度学习隔离验收记录（2026-10-03）

本记录只描述实际完成的合成行情功能/容量验收。虚构标的代码带 `SYNTH`，来源、训练集、模型及回测均携带合成限制；所得收益和预测误差不是投资有效性证据，没有真实分钟行情盈利结论。

最终验收使用独立分支审查修复后的代码：显式 `observed_bar_steps` 和缺口统计、保留 `trading_sessions` 并校验请求内预期整交易日、晚取消后 final 检查点可恢复。此前两轮 CUDA 闭环早于这些边界修复，最终重新执行本节列出的 CUDA 与 CPU 轮；只保留最终 CUDA 和 CPU 两份 JSON，旧隔离 scratch 目录保留。

## 环境与方法

Windows 11，Python 3.13.12，8 物理核 / 16 逻辑线程，约 31.15 GiB RAM；PyTorch **2.11.0+cu128**，CUDA runtime **12.8**，**NVIDIA GeForce RTX 5070 Ti**。工作子进程 OMP/MKL 线程限制为 4，种子 42，确定性算法开启，cuDNN benchmark 关闭。精确环境/参数/制品哈希保存在下列 JSON。

[`scripts/verify_learning_system.py`](../../../scripts/verify_learning_system.py)逐日生成确定性的原始 1m OHLCV 和来源 evidence，发布临时 MarketData 快照，在隔离配置和工作台中调用实际鉴权 API，真实监督调度器启动冻结源码子进程完成 prepare → train → model_backtest，再执行现金、等权、动量对照。验收检查成功登记、文件哈希、时间截止、各分区样本、来源/聚合行数、回测行数、分页、降采样图表及完整 ZIP。

调用发生在 TestClient 内，无需绑定端口；原有服务、实际行情配置和市场库未修改。默认删除生成的临时行情/权重/结果；保留轮只为浏览器后续验收提供独立工作台。

## 实测规模与资源

| 实际运行 | 原始/准备行数 | 模型 | 准备 | 训练 | 冻结模型回测 | 总耗时 | 采样进程树 RSS 峰值 | 逻辑磁盘峰值 |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 最终 CUDA 保留轮 | 115,200 / 115,200 | GRU，2 epoch | 102.71s | 14.30s | 6.19s | 135.99s | 2.014 GiB | 64.95 MiB |
| 最终 CPU 小规模 | 2,400 / 2,400 | linear，1 epoch | 3.73s | 3.09s | 3.30s | 15.97s | 1.184 GiB | 5.03 MiB |

CUDA 原始行情为 8 标的 × 60 合成工作日 × 240 分钟，午休/隔夜明确分段；合成日历只按工作日生成，不模拟真实节假日。训练/验证/测试各占 36/12/12 日，lookback=16、horizon=2、batch_size=256、hidden_size=16。purge 后各分区样本为 **68,984 / 23,024 / 23,024**，测试回测实际读入 **23,040 行**、**2,880 个分钟时间组**，12 个交易日。

最终准备清单有 120 个 `trading_sessions`，`step_policy` 为 `observed_bar_steps`，`expected_minutes=source_rows=115200`，每标的缺失分钟、gap events、无效行和不完整桶均为 0。模型回测摘要明确记录实际日期与请求内派生的 `expected_trading_days` 相同，确认包括首尾的 12 个交易日；现金、等权、动量三项对照同样完成。该检查覆盖整交易日，不声称证明任意真实输入逐标的分钟完整性。

最终原始合成快照与此前轮相同，预测指标也相同；新增政策与交易时段元数据后，准备数据/模型内容 ID 相应变化。不同源码语义版本的内容 ID 不能直接当成相同制品。本次不声称跨硬件逐位一致。

- 来源快照：`71c13b145c75f896b0598d8ecf27aa00f4794285c1007773f0cf23201158c3c6`。
- 最终 CUDA 模型内容 ID：`39a2cd6cc7bf21b32075c391aa00778145278b974b170b03ff4f6f47b096b1a7`。
- 测试误差：MSE `0.0000042024082446672625`，MAE `0.0013532193967861546`，单位为合成对数收益。这是功能输出，不用于判断模型投资价值。

RSS 每 0.5 秒采样父进程与所有后代的常驻内存之和；共享库/页可能重复计数。磁盘是隔离子目录逻辑文件大小，包含源 evidence、Parquet、源码冻结、检查点、窗口缓存和结果；采样峰值是下界，未计硬盘块分配/系统文件缓存。**GPU 分配峰值未测量，JSON 为 null**。本次只能证明 115,200 行规模，没有千万级数据验收或吞吐外推。

## 可复现记录与命令

最终记录见[CUDA 115,200 行](synthetic-cuda-115200-final.json)与[CPU 2,400 行](synthetic-cpu-2400.json)。两轮退出码为 0。

```powershell
.venv/Scripts/python.exe -m scripts.verify_learning_system --device cuda --keep-workspace --output docs/research/2026-10-03-deeplearning/synthetic-cuda-115200-final.json
.venv/Scripts/python.exe -m scripts.verify_learning_system --symbols 2 --days 5 --device cpu --model linear --epochs 1 --output docs/research/2026-10-03-deeplearning/synthetic-cpu-2400.json
```

另实际执行 `--symbols 1 --days 5 --frequency 60 --lookback 512 --epochs 1 --device cpu --baselines`：准备成功，训练因所有分区缺少有效窗口/标签失败；脚本生成失败 JSON、清理独立目录并以 **1** 退出。没有把不成立的训练配置标成成功。

六个 [JSON 模板](../../../examples/learning)已逐一通过当前 `DataRequest/SourceRecord/DataRequirement/PrepareSpec/TrainingSpec/IntradaySpec` 契约校验。模板 ID/来源需要用户填真实值，契约通过不表示数据源已核实。

## 保留轮供本机页面验收使用

最终保留目录位于被 Git 忽略的 `outputs/learning-acceptance/quanto-learning-acceptance-ky2ci5rp`，其中 `market-config.json` 指向同目录的 `market`，工作台为 `workspace`。测试结束后不自动启动服务，避免与已有服务或调度锁冲突。浏览器验收需在单独进程中带该 `QUANTO_MARKET_CONFIG` 启动这个工作台。

- 训练集 job ID：`c761256f-80d9-4945-ac1b-dd567ac88172`。
- 模型 job ID：`199f76ca-1b1a-4e97-9636-a548cce5e9be`。
- 模型回测 job ID：`36298efb-037f-46cb-9265-cf1d49a66c0a`。

## 整体验收

- Python 全量：`pytest -q`，**579 passed, 1 skipped**，204.01 秒；两条原有纳秒时间转 Python datetime 警告，无失败。
- 前端全量：`npm run test -- --run`，**119 passed**；`npm run build` 包含 TypeScript 检查，构建成功。既有 Canvas 测试环境及大 chunk 提示不影响通过。
- 真实监督队列覆盖准备、训练、模型回测、取消、继续训练、冻结输入篡改、依赖漂移、旧数据库迁移和取消抢先发布。晚取消/中断后已经移动到 final 的检查点仍可恢复，原取消模型不登记。
- 独立整分支审查及修复复核：训练归一化与标签边界、下一根开盘、T+N、逐笔费用/回撤限制；修复并复现首尾缺失交易日拒绝、晚取消恢复、保存结果的样本内标记。
- CUA 浏览器实际克隆模型配置并提交 CUDA 训练 `75ace0a4-49db-4736-88f7-e01361447ab4`，随后提交模型回测 `6f7c2c42-24ee-4e91-ac21-b74b7d733f3d`，均显示成功。查看了真实结果曲线、上海时间成交和成交第二页；浏览器无控制台错误。
- 窄窗口验收：发现并修复 855px 窗口中页面宽 1290px 的溢出；加载实际制品/结果后页面宽度与 viewport 均为 855px，表格保留局部滚动。
- ZIP 已通过真实 API 字节、内容清单及完整性检查。浏览器工具捕获下载事件超时，因此不把该捕获尝试计作下载验收证据。

正式本地入口为 `http://127.0.0.1:8770/learning`，使用 `data/deeplearning` 研究工作区和原共享行情库；不混入合成快照。启动命令为 `.venv/Scripts/python.exe -m quanto.cli serve --host 127.0.0.1 --port 8770 --workspace data/deeplearning`。当前共享库尚无分钟快照，真实分钟训练需要按使用指南导入。隔离浏览器验收使用 8771 端口和上列保留目录；验收服务已停止，制品保留。
