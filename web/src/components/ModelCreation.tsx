import { useState } from 'react'
import { api, describeError } from '../api'
import { JsonDetails } from './LearningResults'
import type { InvestingOverview, InvestingModel, PolicyTrainingSpec } from '../investingTypes'

const defaults: PolicyTrainingSpec = {
  action_version: 'hold-target-cash-quartiles-v2',
  name: 'ETF PPO 模型', selection_metric: 'validation_sharpe', frequency: 'weekly', lookback: 120, train_start: '', train_end: '', validation_start: '', validation_end: '', test_start: '', test_end: '',
  episode_sessions: 252, total_timesteps: 4096, n_steps: 128, batch_size: 64, n_epochs: 10, learning_rate: .0003, gamma: 1, gae_lambda: .95, ent_coef: .01, hidden_size: 64, seed: 42, device: 'cpu', eval_every_timesteps: 512,
  backtest: { start: '', end: '', symbols: [], initial_cash: 1000000, seed: 42, execution: { model: 'next_open', commission_rate: .0002, minimum_commission: 5, slippage_rate: .0005, max_stale_sessions: 5 }, risk: { max_total_weight: .8, max_symbol_weight: .35, group_caps: {}, stress_scenarios: [], max_stress_loss: null, drawdown_pause: .2, pause_action: 'block_new', recovery_mode: 'never' }, metrics: { annual_sessions: 252, risk_free_rate: 0, cash_rate: 0, definition_version: 'v1' } },
}
const dateFields = ['train_start', 'train_end', 'validation_start', 'validation_end', 'test_start', 'test_end'] as const
const dateLabels = ['训练开始', '训练结束', '验证开始', '验证结束', '测试开始', '测试结束']
const numericFields: [keyof PolicyTrainingSpec, string, number, number, number?, boolean?][] = [
  ['lookback', '历史窗口（交易日）', 3, 1, 512, true], ['episode_sessions', '训练片段（交易日）', 2, 1, 5000, true], ['total_timesteps', '训练决策步数', 2, 128, 10000000, true], ['n_steps', '每轮采样步数', 2, 1, 8192, true], ['batch_size', '批大小', 2, 1, 8192, true], ['n_epochs', '每轮优化次数', 1, 1, 100, true], ['learning_rate', '学习率', .000001, .0001, 1], ['gamma', '奖励折扣', .000001, .01, 1], ['gae_lambda', 'GAE 系数', .000001, .01, 1], ['ent_coef', '探索系数', 0, .001, 1], ['hidden_size', '隐藏维度', 8, 1, 512, true], ['seed', '随机种子', 0, 1, 2 ** 32 - 1, true], ['eval_every_timesteps', '验证间隔（决策步）', 1, 128, 10000000, true],
]
function validateDate(value: string) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(Date.parse(value)) || new Date(value).toISOString().slice(0, 10) !== value) throw new Error('请填写有效的日历日期。')
}
function number(value: unknown, label: string, min: number, max = Infinity, integer = false) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max || (integer && !Number.isInteger(value))) throw new Error(`${label}必须为${integer ? '整数' : '数值'}，范围 ${min}–${max === Infinity ? '无限制' : max}。`)
}

export function ModelCreation({ overview, onCreated }: { overview: InvestingOverview; onCreated: (model: InvestingModel) => void }) {
  const [train, setTrain] = useState(defaults), [symbols, setSymbols] = useState(''), [datasetId, setDatasetId] = useState('')
  const [busy, setBusy] = useState(false), [error, setError] = useState('')
  const dataset = overview.datasets.find(item => item.id === datasetId)
  const trainPayload = () => {
    if (!dataset) throw new Error('请选择日线数据版本。')
    if (!train.name.trim()) throw new Error('请填写策略名称。')
    dateFields.forEach(key => validateDate(train[key]))
    const d = dateFields.map(key => train[key])
    if (!(d[0] < d[1] && d[1] < d[2] && d[2] < d[3] && d[3] < d[4] && d[4] < d[5])) throw new Error('训练、验证、测试为闭区间，必须依次排列且不重叠。')
    if (d[0] < dataset.manifest.data_start || d[5] > dataset.manifest.data_end) throw new Error('三段日期必须位于数据版本的覆盖区间内。')
    const basket = symbols.split(/[,，\s]+/).filter(Boolean)
    if (!basket.length || new Set(basket).size !== basket.length) throw new Error('请填写固定且不重复的 ETF 篮子。')
    numericFields.forEach(([key, label, min, , max, integer]) => number(train[key], label, min, max, integer))
    if (train.batch_size > train.n_steps || train.n_steps % train.batch_size !== 0) throw new Error('批大小必须不大于每轮采样步数，并能整除采样步数。')
    if (train.device === 'cuda' && !overview?.capabilities.cuda_available) throw new Error('CUDA 不可用，请选择 CPU。')
    number(train.backtest.initial_cash, '初始资金', 1)
    for (const [key, value] of Object.entries(train.backtest.execution)) if (typeof value === 'number') number(value, key, 0, Infinity, key === 'max_stale_sessions')
    number(train.backtest.risk.max_total_weight, '总仓位上限', 0, 1); number(train.backtest.risk.max_symbol_weight, '单标的仓位上限', 0, 1); number(train.backtest.risk.drawdown_pause, '回撤暂停阈值', 0, 1)
    return { dataset_id: datasetId, spec: { ...train, name: train.name.trim(), backtest: { ...train.backtest, start: train.train_start, end: train.test_end, symbols: basket, seed: train.seed } } }
  }
  const setExecution = (key: keyof PolicyTrainingSpec['backtest']['execution'], value: number) => setTrain({ ...train, backtest: { ...train.backtest, execution: { ...train.backtest.execution, [key]: value } } })
  const setRisk = (key: keyof PolicyTrainingSpec['backtest']['risk'], value: number) => setTrain({ ...train, backtest: { ...train.backtest, risk: { ...train.backtest.risk, [key]: value } } })
  const create = async () => {
    setBusy(true); setError('')
    try { onCreated(await api.createInvestingModel(trainPayload())) }
    catch (value) { setError(describeError(value)) }
    finally { setBusy(false) }
  }
  return <div>{error && <p role="alert" className="error">{error}</p>}
    <label>日线数据版本<select disabled={busy} value={datasetId} onChange={event => setDatasetId(event.target.value)}><option value="">请选择日线版本</option>{overview.datasets.map(item => <option key={item.id} value={item.id}>{item.id} · {item.manifest.data_start}–{item.manifest.data_end}</option>)}</select></label>
    {dataset && <p className="muted">{dataset.manifest.price_mode} · {dataset.manifest.limitations.join('；')}</p>}
    <section className="panel"><h2>新建模型</h2><p>创建后配置固定。训练只使用训练段，验证段选模；测试段需明确发起回测。</p><form noValidate onSubmit={event => { event.preventDefault(); void create() }}><fieldset disabled={busy}><div className="form-grid"><label>模型名称<input value={train.name} onChange={event => setTrain({ ...train, name: event.target.value })} /></label><label>选模标准<select value={train.selection_metric} onChange={event => setTrain({ ...train, selection_metric: event.target.value as PolicyTrainingSpec['selection_metric'] })}><option value="validation_sharpe">验证夏普率</option><option value="validation_net_log_return">验证净对数收益</option></select></label><label>决策频率<select value={train.frequency} onChange={event => setTrain({ ...train, frequency: event.target.value as PolicyTrainingSpec['frequency'] })}><option value="weekly">自然周</option><option value="monthly">自然月</option></select></label><label>固定 ETF 篮子（按顺序，逗号分隔）<input value={symbols} onChange={event => setSymbols(event.target.value)} /></label><label>初始资金<input type="number" min={1} value={train.backtest.initial_cash} onChange={event => setTrain({ ...train, backtest: { ...train.backtest, initial_cash: Number(event.target.value) } })} /></label>{dateFields.map((key, index) => <label key={key}>{dateLabels[index]}<input type="date" value={train[key]} onChange={event => setTrain({ ...train, [key]: event.target.value })} /></label>)}</div>
      <details><summary>高级训练、成交与风险参数</summary><div className="form-grid">{numericFields.map(([key, label, min, step, max]) => <label key={key}>{label}<input type="number" min={min} max={max} step={step} value={train[key] as number} onChange={event => setTrain({ ...train, [key]: Number(event.target.value) })} /></label>)}<label>训练设备<select value={train.device} onChange={event => setTrain({ ...train, device: event.target.value as 'cpu' | 'cuda' })}><option value="cpu">CPU</option><option value="cuda" disabled={!overview?.capabilities.cuda_available}>CUDA</option></select></label>{([['commission_rate', '佣金率', .0001], ['minimum_commission', '最低佣金', 1], ['slippage_rate', '滑点率', .0001], ['max_stale_sessions', '停牌估值上限（交易日）', 1]] as const).map(([key, label, step]) => <label key={key}>{label}<input type="number" min={0} step={step} value={train.backtest.execution[key]} onChange={event => setExecution(key, Number(event.target.value))} /></label>)}{([['max_total_weight', '总仓位上限'], ['max_symbol_weight', '单标的仓位上限'], ['drawdown_pause', '回撤暂停阈值']] as const).map(([key, label]) => <label key={key}>{label}<input type="number" min={0} max={1} step={.01} value={train.backtest.risk[key] ?? ''} onChange={event => setRisk(key, Number(event.target.value))} /></label>)}</div></details><JsonDetails title="完整训练配置（日期与篮子以表单为准）" value={{ ...train, backtest: { ...train.backtest, start: train.train_start, end: train.test_end, symbols: symbols.split(/[,，\s]+/).filter(Boolean), seed: train.seed } }} /><button disabled={!datasetId}>创建模型</button></fieldset></form></section>
  </div>
}
