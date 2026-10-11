import { useEffect, useRef, useState } from 'react'
import { ApiError, api, describeError } from '../api'
import { BasketRevisionSelect } from './BasketRevisionSelect'
import { CapabilityReport as CapabilityReportView } from './CapabilityReport'
import { ParameterForm } from './ParameterForm'
import type { BasketRevisionRef, CapabilityReport, Definition, ModelCreateRequest } from '../frameworkTypes'
import type { InvestingModel, InvestingOverview } from '../investingTypes'
import type { ParameterSchema } from '../types'

const initialSpec: ModelCreateRequest['spec'] = {
  name: '', selection_metric: 'validation_sharpe', train_start: '', train_end: '',
  validation_start: '', validation_end: '', test_start: null, test_end: null,
  episode_sessions: 252, total_timesteps: 4096, n_steps: 128, batch_size: 64,
  n_epochs: 10, learning_rate: .0003, gamma: 1, gae_lambda: .95, ent_coef: .01,
  hidden_size: 64, seed: 42, device: 'cpu', eval_every_timesteps: 512,
  backtest: {
    start: '', end: '', initial_cash: 1000000, seed: 42,
    execution: { model: 'next_open', commission_rate: .0002, minimum_commission: 5, slippage_rate: .0005, max_stale_sessions: 5 },
    risk: { max_total_weight: .8, max_symbol_weight: .35, group_caps: {}, stress_scenarios: [],
      max_stress_loss: null, drawdown_pause: .2, pause_action: 'block_new', recovery_mode: 'never' },
    metrics: { annual_sessions: 252, risk_free_rate: 0, cash_rate: 0, definition_version: 'v1' },
  },
}
const dates = [['train_start', '训练开始'], ['train_end', '训练结束'], ['validation_start', '验证开始'],
  ['validation_end', '验证结束'], ['test_start', '留出测试开始'], ['test_end', '留出测试结束']] as const

function matchingTrading(learning: Definition | undefined, trading: Definition) {
  const allowed = learning?.allowed_trading_refs
  return Array.isArray(allowed) && allowed.some(value => {
    if (!value || typeof value !== 'object') return false
    const ref = value as Record<string, unknown>
    return ref.kind === trading.ref.kind && ref.id === trading.ref.id && ref.version === trading.ref.version
  })
}

function definitionKey(ref: Definition['ref']) {
  return JSON.stringify([ref.kind, ref.id, ref.version])
}

function serverReport(error: unknown): CapabilityReport | null {
  const value = error instanceof ApiError ? error.details.report : null
  return value && typeof value === 'object' && 'status' in value ? value as CapabilityReport : null
}

export function ModelCreation({ overview, onCreated }: { overview: InvestingOverview; onCreated: (model: InvestingModel) => void }) {
  const [definitions, setDefinitions] = useState<Definition[]>([])
  const [datasetId, setDatasetId] = useState(''), [basket, setBasket] = useState<BasketRevisionRef | null>(null)
  const [learningId, setLearningId] = useState(''), [tradingId, setTradingId] = useState('')
  const [spec, setSpec] = useState(initialSpec), [learningParams, setLearningParams] = useState<Record<string, unknown>>({})
  const [paramsValid, setParamsValid] = useState(true), [report, setReport] = useState<CapabilityReport | null>(null)
  const [checkedKey, setCheckedKey] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('')
  const currentKey = JSON.stringify({ datasetId, basket, learningId, tradingId, spec, learningParams, paramsValid })
  const latestKey = useRef(currentKey)
  latestKey.current = currentKey

  useEffect(() => { let active = true; api.listDefinitions().then(value => { if (active) setDefinitions(value.items) })
    .catch(value => { if (active) setError(describeError(value)) }); return () => { active = false } }, [])

  const learning = definitions.find(item => item.ref.kind === 'learning' && definitionKey(item.ref) === learningId)
  const trading = definitions.filter(item => item.ref.kind === 'trading' && matchingTrading(learning, item))
  const selectedTrading = trading.find(item => definitionKey(item.ref) === tradingId)
  const rawSchema = learning?.parameter_schema as ParameterSchema | undefined
  const parameterSchema: ParameterSchema = { type: 'object', properties: Object.fromEntries(
    Object.entries(rawSchema?.properties || {}).filter(([key]) => key !== 'action_version' && key !== 'reward_version')) }
  const visibleReport = checkedKey === currentKey ? report : null

  const request = (): ModelCreateRequest => {
    if (!datasetId || !basket || !learning || !selectedTrading) throw new Error('请选择数据、篮子、学习定义和匹配的交易定义。')
    if (!spec.name?.trim()) throw new Error('请填写模型名称。')
    if (!paramsValid || !Number.isInteger(learningParams.lookback) || !['weekly', 'monthly'].includes(String(learningParams.frequency)))
      throw new Error('请按所选学习定义填写有效的观察窗口与决策频率。')
    if (!spec.train_start || !spec.train_end || !spec.validation_start || !spec.validation_end) throw new Error('请填写训练和验证边界。')
    if (!!spec.test_start !== !!spec.test_end) throw new Error('留出测试开始与结束必须同时填写。')
    if (typeof learning.action_version !== 'string' || typeof learning.reward_version !== 'string') throw new Error('学习定义缺少动作或奖励版本。')
    return {
      framework_contract_version: 'multi-asset-v1', basket_revision_ref: basket, dataset_id: datasetId,
      learning_definition_ref: { ...learning.ref, kind: 'learning' },
      trading_definition_ref: { ...selectedTrading.ref, kind: 'trading' },
      spec: { ...spec, ...learningParams, name: spec.name.trim(), lookback: learningParams.lookback as number,
        frequency: learningParams.frequency as 'weekly' | 'monthly',
        action_version: learning.action_version as ModelCreateRequest['spec']['action_version'],
        reward_version: learning.reward_version as ModelCreateRequest['spec']['reward_version'],
        gamma: learning.reward_version === 'net-log-return-v1' ? spec.gamma : 1,
        backtest: { ...spec.backtest, start: spec.train_start, end: spec.test_end || spec.validation_end,
          seed: spec.seed ?? 42, metrics: { ...spec.backtest.metrics,
            risk_free_rate: learning.reward_version === 'net-log-return-v1' ? spec.backtest.metrics.risk_free_rate : 0 } } },
    }
  }

  const check = async () => {
    setError(''); setReport(null); setCheckedKey('')
    const key = latestKey.current
    try {
      const payload = request()
      setBusy(true)
      const next = await api.checkCapabilities({ operation: 'train', ...payload })
      if (latestKey.current === key) { setReport(next); setCheckedKey(key) }
    } catch (value) { if (latestKey.current === key) { const next = serverReport(value); if (next) { setReport(next); setCheckedKey(key) }; setError(describeError(value)) } }
    finally { setBusy(false) }
  }
  const create = async () => {
    if (visibleReport?.status !== 'ready') return
    setBusy(true); setError('')
    const key = latestKey.current
    try { onCreated(await api.createInvestingModel(request())) }
    catch (value) { if (latestKey.current === key) { const next = serverReport(value); if (next) { setReport(next); setCheckedKey(key) }; setError(describeError(value)) } }
    finally { setBusy(false) }
  }

  return <section className="panel"><h2>新建模型</h2><p>选择已保存的有序篮子和实际可用的学习、交易定义。创建仅保存模型，训练另行提交。</p>
    {error && <p role="alert" className="error">{error}</p>}
    <fieldset disabled={busy}><div className="form-grid">
      <label>日线数据版本<select value={datasetId} onChange={event => setDatasetId(event.target.value)}><option value="">请选择数据版本</option>{overview.datasets.map(item => <option key={item.id} value={item.id}>{item.manifest.source} · {item.manifest.data_start}—{item.manifest.data_end}</option>)}</select></label>
      <label>学习定义<select value={learningId} onChange={event => { setLearningId(event.target.value); setTradingId(''); setLearningParams({}) }}><option value="">请选择学习定义</option>{definitions.filter(item => item.ref.kind === 'learning').map(item => <option key={definitionKey(item.ref)} value={definitionKey(item.ref)}>{item.ref.id}</option>)}</select></label>
      <label>交易定义<select value={tradingId} onChange={event => setTradingId(event.target.value)}><option value="">请选择匹配的交易定义</option>{trading.map(item => <option key={definitionKey(item.ref)} value={definitionKey(item.ref)}>{item.ref.id}</option>)}</select></label>
    </div><BasketRevisionSelect value={basket} onChange={setBasket} disabled={busy} />
      {learning && <>
        <ParameterForm key={definitionKey(learning.ref)} schema={parameterSchema} value={learningParams} showJson={false}
          idPrefix="model-learning" onChange={setLearningParams} onValidityChange={setParamsValid} /></>}
      <div className="form-grid"><label>模型名称<input value={spec.name} onChange={event => setSpec({ ...spec, name: event.target.value })} /></label>
        <label>选模指标<select value={spec.selection_metric} onChange={event => setSpec({ ...spec, selection_metric: event.target.value as typeof spec.selection_metric })}><option value="validation_sharpe">验证夏普</option><option value="validation_net_log_return">验证净对数收益</option></select></label>
        {dates.map(([key, label]) => <label key={key}>{label}<input type="date" value={spec[key] || ''} onChange={event => setSpec({ ...spec, [key]: event.target.value || null })} /></label>)}
        <label>初始资金<input type="number" min={1} value={spec.backtest.initial_cash} onChange={event => setSpec({ ...spec, backtest: { ...spec.backtest, initial_cash: Number(event.target.value) } })} /></label>
        <label>训练步数<input type="number" min={2} value={spec.total_timesteps} onChange={event => setSpec({ ...spec, total_timesteps: Number(event.target.value) })} /></label>
        <label>随机种子<input type="number" value={spec.seed} onChange={event => setSpec({ ...spec, seed: Number(event.target.value) })} /></label>
      </div><details><summary>高级训练、成交与风险参数</summary><div className="form-grid">
        {([['episode_sessions', '训练片段'], ['n_steps', '每轮采样步数'], ['batch_size', '批大小'],
          ['n_epochs', '每轮优化次数'], ['learning_rate', '学习率'], ['gamma', '奖励折扣'],
          ['gae_lambda', 'GAE 系数'], ['ent_coef', '探索系数'], ['hidden_size', '隐藏维度'],
          ['eval_every_timesteps', '验证间隔']] as const).map(([key, label]) => <label key={key}>{label}<input type="number"
            step={['learning_rate', 'gamma', 'gae_lambda', 'ent_coef'].includes(key) ? 'any' : 1}
            disabled={key === 'gamma' && learning?.reward_version !== 'net-log-return-v1'} value={spec[key] ?? ''}
            onChange={event => setSpec({ ...spec, [key]: Number(event.target.value) })} /></label>)}
        <label>训练设备<select value={spec.device} onChange={event => setSpec({ ...spec, device: event.target.value as 'cpu' | 'cuda' })}><option value="cpu">CPU</option><option value="cuda" disabled={!overview.capabilities.cuda_available}>CUDA</option></select></label>
        {([['commission_rate', '佣金率'], ['minimum_commission', '最低佣金'], ['slippage_rate', '滑点率'],
          ['max_stale_sessions', '停牌估值上限']] as const).map(([key, label]) => <label key={key}>{label}<input type="number" min={0} step="any" value={spec.backtest.execution[key]}
          onChange={event => setSpec({ ...spec, backtest: { ...spec.backtest,
            execution: { ...spec.backtest.execution, [key]: Number(event.target.value) } } })} /></label>)}
        {([['max_total_weight', '总仓位上限'], ['max_symbol_weight', '单标的仓位上限'],
          ['drawdown_pause', '回撤暂停阈值']] as const).map(([key, label]) => <label key={key}>{label}<input type="number" min={0} max={1} step="any" value={spec.backtest.risk[key] ?? ''}
          onChange={event => setSpec({ ...spec, backtest: { ...spec.backtest,
            risk: { ...spec.backtest.risk, [key]: Number(event.target.value) } } })} /></label>)}
        <label>无风险利率<input type="number" step="any" disabled={learning?.reward_version !== 'net-log-return-v1'} value={spec.backtest.metrics.risk_free_rate}
          onChange={event => setSpec({ ...spec, backtest: { ...spec.backtest,
            metrics: { ...spec.backtest.metrics, risk_free_rate: Number(event.target.value) } } })} /></label>
      </div>{learning?.reward_version !== 'net-log-return-v1' && <p className="muted">当前奖励定义固定 γ=1、无风险利率=0；这不代表回测利润。</p>}</details>
      <div className="inline-actions"><button type="button" onClick={() => void check()}>检查运行能力</button>
        <button type="button" disabled={visibleReport?.status !== 'ready'} onClick={() => void create()}>创建模型</button></div>
    </fieldset>{visibleReport && <CapabilityReportView report={visibleReport} />}
  </section>
}
