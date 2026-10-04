# 固定篮子模型与四域工作台设计

## 用户目标与授权

用户需要每个固定、有序 ETF 篮子对应一个专用 PPO 模型。同一模型反复抽取历史片段学习，模型参数累积，保存到本地，随后指定保存版本回测并查看报告和数据。不同篮子创建不同模型，不实现可变篮子的通用模型。用户授权直接在 `training` 分支实现，不等待设计或代码审批，不 push。开发结束后不开始实际研究训练，ETF 篮子选择规则另行讨论。

## 设计审查与信息架构

已分别进行资深 UX 与投资研究平台产品/技术设计审查。采用持久模型为中心的结构，避免原 ETF 自主投资页把数据、训练、产物和回测混成一张长表单。

主导航严格四项：行情数据 `/datasets`、固定策略 `/experiments`、强化学习模型 `/models`、回测 `/runs`。对比属于回测域；分钟训练退出主导航。原始行情库、固定策略参数与所有历史报告保留。历史 PPO、分钟回测的旧深链仍能打开只读结果，不展示旧训练表单。

模型列表先展示模型，再提供创建入口。模型详情包含固定身份摘要、训练记录、保存版本。创建需显式输入有序篮子，不从数据版本自动填充全部 ETF，不提供未经讨论的推荐篮子。重复代码报错。Agent 使用同一认证 API 创建模型并指定篮子，创建不会排入训练队列。

## 对象与训练语义

- 模型：持久 `model_id`，保存 `dataset_id` 和完整 `PolicyTrainingSpec`。名字、篮子顺序、日周/月频率、观察和网络结构、日期划分、成本、风控及验证选模规则冻结。修改这些研究条件需创建新模型。
- 训练会话：一次有限的追加步数预算；可设置较长预算，在同一任务中反复抽取 episode。用户可在完成后继续追加任意多次。不会自动创建无限任务。
- 保存版本：成功会话产物，以训练任务 ID 为版本 ID，复用已校验的 `learning_artifacts`。同时保存 `policy.zip`（整个模型训练历史中验证最优）与 `last.zip`（本次最终参数、优化器和累计步数）。续训使用 last，回测使用 policy。
- 新建模型为待训练记录。首次开始训练才初始化 PPO；后续通过 `PPO.load` 恢复最后成功版本的参数与优化器，用 `reset_num_timesteps=False` 追加学习。新会话重置模拟账户和采样环境，不承诺与不中断训练逐位一致。
- 同一模型最多一个 queued/running/cancel_requested 会话，提交事务同时检查并登记模型关联。失败/取消不覆盖已保存版本；界面说明取消会放弃本次未发布进度，后续从上次成功版本继续。
- 会话只接收 `additional_timesteps`（2..10000000）和 `device`（cpu/cuda）；不可通过续训更换模型身份或历史分区。
- 新模型训练只使用训练段更新、验证段选模，不自动重复评估测试段。测试段通过显式冻结回测使用。保留已有独立训练 API 以支持历史工具/调用，不把旧推断制品冒充可续训模型。
- `selection_metric` 支持 `validation_net_log_return` 与 `validation_sharpe`；已有请求默认净对数收益，新模型 UI 默认验证夏普率，均展示收益、夏普、回撤。无定义的夏普不伪造为 0，不胜过有定义值；如果全为空，保留一个可用快照并明确报告空指标。

## API 契约

`/api/investing/models` GET 返回 `{items,next_cursor}`，POST 接收 `{dataset_id,spec}`，创建模型但不提交任务。模型摘要：`id,name,dataset_id,spec,created_at,state,active_job_id,session_count,version_count,total_timesteps,latest_version_id`。

`GET /api/investing/models/{id}` 返回上述字段及 `sessions`（JobRecord，含 `base_version_id`）、`versions`（现有 policy artifact 结构）。

`POST /api/investing/models/{id}/train` 接收 `{additional_timesteps,device}`，默认值来自模型 spec。服务器选择最近成功会话的 last 状态，冻结其引用与请求后排队。返回 JobRecord。`POST /api/jobs/{id}/cancel` 沿用取消机制。

`POST /api/investing/models/{id}/backtests` 接收 `{version_id,dataset_id,spec}`；必须显式指定属于该模型的成功版本。转入现有冻结 policy 回测，保留版本、模型、数据和参数来源。继续训练不会改变旧版本或排队中的回测。

Agent CLI 在 `quanto investing` 下增加 `models`、`create-model --json`、`get models ID`、`train-model ID --json` 和 `model-backtest ID --json`，所有路径与网页共用服务。

## 回测与历史

回测页默认展示统一历史，发起操作收于“新建回测”。输入类型为固定策略实验或强化学习保存版本。必须先选择模型再明确选择版本；从“使用此版本回测”深链可以预填那个明确版本。模型篮子只读，默认原数据但允许选择覆盖合格且身份一致的新日线版本。结果包含每日净值、指标、订单、成交、持仓、信号及 ZIP。原历史记录仍通过统一目录访问。

## 验证与运行边界

先复核历史测试是否仍符合新需求，更新变更的 UI/API 断言；不为旧页面保留兼容表单。覆盖创建不训练、篮子不可变、并发提交拒绝、同一模型续训的参数/优化器与累计步数、全局最佳保留、旧版本哈希不变、选定版本回测、取消保留旧版本、两类选模、无测试期自动评估和四域导航。

只在临时工作区与合成夹具中运行必要的短程训练测试。不得给实际工作区创建模型、选择 ETF 或提交训练。保留未跟踪 `docs/planning-artifacts/prd.md`。每个可检查阶段验证并本地提交。
