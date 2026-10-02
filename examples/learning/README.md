# 深度学习契约模板

这些 JSON 是结构示例，没有附带真实分钟行情。全零快照哈希、`REPLACE_*` ID、来源、下载日期、单位及日期范围都必须依据实际输入调整。示例证券代码仅说明标的身份，日期仅说明时间切分，不构成投资推荐。

- `minute-import.metadata.json`：供应商原始分钟 CSV/Parquet 导入；替换来源和单位，标注未核实限制。
- `calendar-import.metadata.json`：同期真实交易日历导入；午休分两段，节假日不产生交易时段。
- `minute-requirement.json`：导入后完整需求预缓存，生成 ready snapshot。
- `prepare.json`：填写 snapshot ID，准备 5 分钟训练集。
- `train.json`：填写 dataset ID，提交按时间切分的 GRU 训练；改 `model_type` 为 `linear` 可作预测基线。
- `backtest.json`：填写 dataset/model ID，在测试段回测冻结模型。改 `baseline` 为 `cash`、`equal_weight` 或 `momentum` 并移除 `model_id` 可提交持仓对照。

恢复训练使用原训练请求，在顶层加 `resume_job_id`，增加目标 `spec.epochs`；其他身份参数保持一致。完整操作与边界见[指南](../../docs/06_深度学习工作台使用指南.md)。
