# 个股量价训练

首版支持沪深主板普通 A 股、人民币多头现金账户、100 股整手和 T+1 可卖。公司资讯用于上游选股，观察仅使用量价和账户状态。当前没有提供完整真实股票历史数据包；示例配置不是投资结果或真实数据覆盖声明。

1. 准备可信的 `raw_cash` 导入目录：`manifest.json`、prices、benchmark、calendar、instruments、status、events，以及下述三张辅助表。核心表支持 CSV/Parquet；股票身份使用来源明确的 SSE/SZSE `stock`，不能根据代码猜板块。
2. 使用已有数据导入 API/CLI 和 `identity_evidence.json` 注册数据快照。通过 `make_import_evidence` 固定原始文件摘要；股票映射、基准、核心与辅助表语义必须全部显式声明。`standard_table_semantics` 会保留 instruments.board。
3. 保存股票组合，选择“个股量价 PPO”和“沪深主板股票日线交易”。修改 `training.json` 的数据 ID、组合引用、日期和参数，提交 `POST /api/capabilities/check`（在请求中增加 `operation: "train"`）。检查通过后向 `POST /api/investing/models` 提交原配置；创建模型不会开始训练。
4. 使用模型页面单独开始训练。模型固定成员及其顺序、训练数据和方案；换股票池需要新建模型。保存轮次可用于 policy、cash、buy_hold 基线和成本倍数实验。

## 导入附加要求

`instruments.board` 必须为 `main_board`，`lot_size=100`、`settlement_sessions=1`。单位为 `volume_unit=share`、`amount_unit=CNY`。不能用复权数据直接代替原始价格与公司行动。

manifest 声明 `auxiliary_tables` 的三个相对文件名：

| 表 | 语义版本 | 必备列 |
|---|---|---|
| fee_rules | stock-fee-rules@1 | exchange, instrument_type, board, side, effective_from, effective_to, stamp_duty_rate, transfer_fee_rate, available_at, source_ref |
| stock_open_status | stock-open-status@1 | symbol, date, status, can_buy, can_sell, available_at, source_ref |
| stock_sources | stock-sources@1 | source_ref, source_url, payload_base64, payload_sha256, published_at, retrieved_at, available_at, coverage_start, coverage_end |

辅助表的 `TableSemanticBinding.temporal_rules` 使用 `availability_rule=execution_event`，只用于撮合和证据校验，不进入观察。business_keys 分别为 `[exchange, board, side, effective_from]`、`[symbol, date]`、`[source_ref]`。schema_definition 中 `source_ref` 为 `{dtype: "string", unit: "reference"}`；其他列按实际类型声明。核心表由 `standard_table_semantics` 声明。

费率区间为 `[effective_from, effective_to)`，结束日期不可省略；买卖两侧分别声明，买入印花税必须为零。每个执行日期必须有唯一生效规则。税率来自归档事实，不把某一天的现行税率套到全部历史。交易定义按每笔成交将印花税、过户费四舍五入到分；成本实验缩放已计算费用，佣金/滑点使用原有独立配置。

开盘状态须每个执行日每只股票一条，status 为 trading/suspended，买卖权限是真布尔值，available_at 不晚于当地 09:30。不能根据当日收盘涨跌、成交量反推开盘是否可买卖。未知权限或状态直接阻塞。

source_ref 必须引用已归档来源。原文/原始响应以 base64 字节存入 payload_base64，payload_sha256 为这些字节的 SHA-256。published_at、available_at、retrieved_at 均带时区；published_at ≤ available_at ≤ retrieved_at，覆盖起止日期明确。摘要匹配证明字节未变，不代表系统已独立核实内容真实性。

source_url 支持 HTTP(S) 和带明确主机、端口的 TCP 行情端点，不能用说明文档地址冒充实际数据端点。TCP 来源同样必须保存原始报文并通过摘要、时间、覆盖范围校验。历史行情中的原始市场事件时间与本次下载时间分开记录；只有报文时间语义及交易所发布规则支持时，才可将原始事件的公开时间用于 published_at/available_at，并明确记录这一解释依据，不能把下载日期任意回填到历史开盘。

manifest.field_semantics.stock_event_coverage_source 引用覆盖整个研究区间的公司行动证据。`event_coverage=complete` 不能代替这份证据；即使 events 为空也必须证明确认区间内无事件。未知退市/清算等事件会阻塞。股息按个人股息税前入账，报告会注明这一限制；请在原始 manifest.limitations 同时披露。

`training.json` 使用非常短的验证预算供接线检查。真实研究需要单独设计样本、成本和评估边界；回看训练/验证段应选择 development_replay，不能称为选股盲测。
