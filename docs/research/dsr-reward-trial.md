# DSR 单区间对照试验

入口是 `scripts/compare_policy_rewards.py`。它准备两份模型配置，并比较两个已训练版本；不会自动训练、搜索 seed 或遍历多个时期。奖励定义见 [SDD](../superpowers/specs/2026-10-06-rl-dsr-reward-design.md)。

## 1. 开始前确定一次比较的条件

准备一份合法的 `PolicyTrainingSpec` JSON（只有配置本身，不包含 API 外层的 `dataset_id` 或研究协议其他字段），以及对应数据集目录。保留固定 ETF 篮子、账户假设、seed、训练起点和 PPO 参数。seed 可自行指定，42 只是现有默认值。

在普通试验记录中提前写下两边相同的有限训练预算和会话划分。选择最近一年或两年中的一个区间；不要看到结果后再改长度。

```powershell
.venv\Scripts\python.exe scripts\compare_policy_rewards.py prepare source-spec.json path\to\dataset outputs\dsr-trial --years 2
```

`--years` 默认为 2，也接受 1。程序依据数据覆盖内固定篮子共同可用的最后交易日 T，生成 `(T-N年, T]` 的验证区间；闰日周年边界取月末，实际起点为该边界次日。行情缺口、日期覆盖不足或任一 ETF 的训练/验证首次决策预热不足会报错。

例如，数据截止 `2026-09-30`，两年验证期是 `2024-10-01` 至 `2026-09-30`，训练结束日是 `2024-09-30`。训练起点保持原配置；不会用电脑日期代替数据截止日。

输出目录必须为空，并位于数据集之外。输出文件为：

- `trial.json`：数据哈希、唯一评价区间、两份配置及必要约束的变更记录。
- `control-spec.json`：`net-log-return-with-state-v1`，净对数收益加 A/B 状态输入。
- `dsr-spec.json`：`differential-sharpe-v1`，与对照相同输入，使用每日 DSR 奖励。

两份配置都使用 `validation_sharpe` 选优，固定 `gamma=1`、`risk_free_rate=0`，不设置留出测试期。其他训练参数和用户 seed 保留。请求步数可以不是完整 rollout 的倍数，实际完成步数以产物为准。

## 2. 创建并继续训练两条模型链

使用生成的配置分别创建两个模型。现有创建接口 `POST /api/investing/models` 的请求体是 `{"dataset_id": "对应已注册数据版本ID", "spec": <生成的配置对象>}`；也可按配置填写现有创建页面。注册的数据内容必须与 `trial.json` 对应。

通过已有开始/追加训练功能，对两边执行相同会话划分和预算。两条链各自从自己的 `last.zip` 继续学习，各自在同一近期验证期按 Sharpe 保存 `policy.zip`。可以同时调整两边下一会话的追加步数和设备，不改变固定配置。

保留现有模型历史中的成功、失败、取消和未完成记录。不要把旧模型固定 2024 年的最佳分数沿用为新模型门槛，也不要修改旧模型定义。

## 3. 比较明确指定的两个已保存版本

使用两个版本的产物目录，每个目录应包含 `manifest.json`、`policy.zip` 和 `last.zip`：

```powershell
.venv\Scripts\python.exe scripts\compare_policy_rewards.py compare outputs\dsr-trial\trial.json path\to\control-version path\to\dsr-version outputs\dsr-comparison
```

程序检查产物哈希、数据身份、固定配置、奖励/输入身份、累计实际步数、末次会话步数和验证位置。输出目录必须为空，并位于原数据和模型产物之外。它只加载各自选中的 `policy.zip`，在原验证区间复用现有账户评价器。

**会话检查边界：**最终产物不包含所有早期会话边界，程序不能证明早期训练会话划分完全相同。解释结果前，必须结合两条模型的已有历史核对完整会话划分及失败/取消记录；`comparison.json` 也保留这一限制。

输出包括：

- `control/`、`dsr/`：完整账户 CSV 和 `summary.json`，标记为用于选优的 `policy_validation` / `model_selection`。
- `comparison.json`：并列 Sharpe、收益、回撤、换手、成本、现金比例、实际完成步数及训练/评价数值诊断。训练诊断范围是所选版本的当前会话，不是整条训练链。

只有两边选优成绩均有效且有限，才按严格较高的验证 Sharpe 标记本次胜出方；相同成绩或任一无效时 `winner` 为 null。该标记是当前区间的数值比较。奖励总和只用于各自公式内部诊断，不能跨奖励比较，也不是盈利。

近期区间已经用于选优，不是新的独立样本外测试。普通后续回测仍要求开始日期晚于验证结束日。软件验证通过不代表 DSR 改善了实际绩效；本轮开发没有启动真实市场长训练。
