import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, describeError } from '../api'
import { JsonDetails, Metrics, record } from '../components/LearningResults'
import { taskTime } from '../components/timePresentation'
import type { InvestingArtifact, InvestingChart, InvestingOverview, InvestingTablePage, PolicyBacktestSpec, PolicyTrainingSpec } from '../investingTypes'
import type { JobRecord, RunEvent } from '../types'

const defaults: PolicyTrainingSpec = {
  name: 'ETF PPO 策略', frequency: 'weekly', lookback: 120, train_start: '', train_end: '', validation_start: '', validation_end: '', test_start: '', test_end: '',
  episode_sessions: 252, total_timesteps: 4096, n_steps: 128, batch_size: 64, n_epochs: 10, learning_rate: .0003, gamma: 1, gae_lambda: .95, ent_coef: .01, hidden_size: 64, seed: 42, device: 'cpu', eval_every_timesteps: 512,
  backtest: { start: '', end: '', symbols: [], initial_cash: 1000000, seed: 42, execution: { model: 'next_open', commission_rate: .0002, minimum_commission: 5, slippage_rate: .0005, max_stale_sessions: 5 }, risk: { max_total_weight: .8, max_symbol_weight: .35, group_caps: {}, stress_scenarios: [], max_stress_loss: null, drawdown_pause: .2, pause_action: 'block_new', recovery_mode: 'never' }, metrics: { annual_sessions: 252, risk_free_rate: 0, cash_rate: 0, definition_version: '1' } },
}
const dateFields = ['train_start', 'train_end', 'validation_start', 'validation_end', 'test_start', 'test_end'] as const
const dateLabels = ['训练开始', '训练结束', '验证开始', '验证结束', '测试开始', '测试结束']
const numericFields: [keyof PolicyTrainingSpec, string, number, number, number?, boolean?][] = [
  ['lookback', '历史窗口（交易日）', 3, 1, 512, true], ['episode_sessions', '训练片段（交易日）', 2, 1, 5000, true], ['total_timesteps', '训练决策步数', 2, 128, 10000000, true], ['n_steps', '每轮采样步数', 2, 1, 8192, true], ['batch_size', '批大小', 2, 1, 8192, true], ['n_epochs', '每轮优化次数', 1, 1, 100, true], ['learning_rate', '学习率', .000001, .0001, 1], ['gamma', '奖励折扣', .000001, .01, 1], ['gae_lambda', 'GAE 系数', .000001, .01, 1], ['ent_coef', '探索系数', 0, .001, 1], ['hidden_size', '隐藏维度', 8, 1, 512, true], ['seed', '随机种子', 0, 1, 2 ** 32 - 1, true], ['eval_every_timesteps', '验证间隔（决策步）', 1, 128, 10000000, true],
]
const states: Record<string, string> = { queued: '排队中', running: '运行中', cancel_requested: '取消中', cancelled: '已取消', interrupted: '已中断', failed: '失败', succeeded: '成功' }
const active = (state: string) => ['queued', 'running', 'cancel_requested'].includes(state)
const name = (item: InvestingArtifact) => String(item.manifest.name || record(item.manifest.config).name || item.id)
function validateDate(value: string) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(Date.parse(value)) || new Date(value).toISOString().slice(0, 10) !== value) throw new Error('请填写有效的日历日期。')
}
function number(value: unknown, label: string, min: number, max = Infinity, integer = false) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max || (integer && !Number.isInteger(value))) throw new Error(`${label}必须为${integer ? '整数' : '数值'}，范围 ${min}–${max === Infinity ? '无限制' : max}。`)
}
function DailyCurve({ chart }: { chart: InvestingChart }) {
  const points = chart.points.filter(point => Number.isFinite(point.equity))
  if (!points.length) return <p>暂无曲线数据。</p>
  const min = Math.min(...points.map(point => point.equity)), max = Math.max(...points.map(point => point.equity)), range = max - min || 1
  const path = points.map((point, index) => `${index ? 'L' : 'M'}${30 + index / Math.max(points.length - 1, 1) * 940},${190 - (point.equity - min) / range * 160}`).join(' ')
  return <><svg className="learning-curve" viewBox="0 0 1000 220" role="img" aria-label="每日净值曲线"><title>每日净值曲线</title><path d="M30 20 V190 H970" fill="none" stroke="#bccbd0" /><path d={path} fill="none" stroke="#087688" strokeWidth="2" /><text x="30" y="212">{points[0].time.slice(0, 10)}</text><text x="970" y="212" textAnchor="end">{points[points.length - 1].time.slice(0, 10)}</text></svg><p className="muted">显示 {points.length} / {chart.total_count} 个日线点（最多 1000 点）；指标使用完整每日结果。</p></>
}
function InvestingResults({ id }: { id: string }) {
  const [artifact, setArtifact] = useState<InvestingArtifact | null>(null), [chart, setChart] = useState<InvestingChart | null>(null)
  const [table, setTable] = useState<InvestingTablePage | null>(null), [tableName, setTableName] = useState('daily_equity')
  const [cursors, setCursors] = useState<(number | string)[]>([0]), [error, setError] = useState(''), [tableError, setTableError] = useState(''), [downloading, setDownloading] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    Promise.allSettled([api.investingArtifact('backtests', id, controller.signal), api.investingChart(id, controller.signal)]).then(([detail, curve]) => {
      if (controller.signal.aborted) return
      if (detail.status === 'fulfilled') setArtifact(detail.value)
      if (curve.status === 'fulfilled') setChart(curve.value)
      setError([detail, curve].filter(value => value.status === 'rejected').map(value => describeError((value as PromiseRejectedResult).reason)).join('；'))
    })
    return () => controller.abort()
  }, [id])
  useEffect(() => {
    const controller = new AbortController(); setTable(null); setTableError('')
    api.investingTable(id, tableName, cursors[cursors.length - 1], controller.signal).then(value => { if (!controller.signal.aborted) setTable(value) }).catch(value => { if (!controller.signal.aborted) setTableError(describeError(value)) })
    return () => controller.abort()
  }, [id, tableName, cursors])
  const download = async () => {
    setDownloading(true); setError('')
    try { const blob = await api.exportInvesting(id), url = URL.createObjectURL(blob), anchor = document.createElement('a'); anchor.href = url; anchor.download = `investing-${id}.zip`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) }
    catch (value) { setError(describeError(value)) } finally { setDownloading(false) }
  }
  const columns = Array.from(new Set(table?.items.flatMap(row => Object.keys(row)) || []))
  return <section className="panel" aria-label="日线回测结果"><div className="section-heading"><h2>日线回测结果 · {id}</h2><button disabled={downloading} onClick={() => void download()}>{downloading ? '导出中…' : '下载完整 ZIP'}</button></div>
    {error && <p role="alert" className="error">{error}</p>}{artifact && <><Metrics values={record(artifact.manifest.metrics)} /><JsonDetails title="执行假设、数据来源与完整配置" value={artifact.manifest} /></>}{chart && <DailyCurve chart={chart} />}
    <label>结果明细<select value={tableName} onChange={event => { setTableName(event.target.value); setCursors([0]) }}>{Object.entries({ daily_equity: '每日净值', orders: '订单', fills: '成交', signals: '决策信号', positions: '持仓' }).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label>
    {tableError && <p role="alert" className="error">{tableError}</p>}{!table && !tableError && <p>加载明细…</p>}{table && <><div className="table-wrap"><table><thead><tr>{columns.map(key => <th key={key}>{key}</th>)}</tr></thead><tbody>{table.items.map((row, index) => <tr key={index}>{columns.map(key => <td key={key}>{typeof row[key] === 'object' ? JSON.stringify(row[key]) : String(row[key] ?? '—')}</td>)}</tr>)}</tbody></table></div>{!table.items.length && <p>暂无明细。</p>}<div className="inline-actions"><button disabled={cursors.length < 2} onClick={() => setCursors(previous => previous.slice(0, -1))}>上一页明细</button><span>第 {cursors.length} 页 · 每页最多 100 行</span><button disabled={table.next_cursor === null} onClick={() => { if (table.next_cursor !== null) setCursors(previous => [...previous, table.next_cursor!]) }}>下一页明细</button></div></>}
  </section>
}

export function InvestingPage() {
  const [searchParams] = useSearchParams()
  const selectedResult = searchParams.get('backtest') || ''
  const [overview, setOverview] = useState<InvestingOverview | null>(null), [overviewError, setOverviewError] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState(''), [busy, setBusy] = useState(false)
  const [datasetId, setDatasetId] = useState(''), [policyId, setPolicyId] = useState(''), [train, setTrain] = useState(defaults), [symbols, setSymbols] = useState('')
  const [backtest, setBacktest] = useState<PolicyBacktestSpec>({ start: '', end: '', baseline: 'policy', cost_multiplier: 1 })
  const [jobUpdates, setJobUpdates] = useState<JobRecord[]>([]), [selectedLog, setSelectedLog] = useState(''), [logs, setLogs] = useState<RunEvent[]>([]), [logError, setLogError] = useState('')
  const [selectedPolicy, setSelectedPolicy] = useState(''), [detail, setDetail] = useState<InvestingArtifact | null>(null), [detailError, setDetailError] = useState(''), [refresh, setRefresh] = useState(0)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>
    const load = async () => {
      try {
        const value = await api.investingOverview(controller.signal)
        if (controller.signal.aborted) return
        setOverview(value); setOverviewError(''); setDatasetId(previous => previous || value.datasets[0]?.id || ''); setPolicyId(previous => previous || value.policies[0]?.id || '')
        setJobUpdates(previous => previous.filter(job => !value.jobs.some(current => current.id === job.id)))
      } catch (value) { if (!controller.signal.aborted) setOverviewError(describeError(value)) }
      finally { if (!controller.signal.aborted) timer = setTimeout(load, 3000) }
    }
    void load(); return () => { controller.abort(); clearTimeout(timer) }
  }, [refresh])
  const dataset = overview?.datasets.find(item => item.id === datasetId)
  useEffect(() => {
    if (!datasetId) return
    const controller = new AbortController()
    setSymbols('')
    api.dataset(datasetId, controller.signal).then(value => {
      if (!controller.signal.aborted) setSymbols(value.instruments?.map(item => item.symbol).join(', ') || '')
    }).catch(value => { if (!controller.signal.aborted) setError(describeError(value)) })
    return () => controller.abort()
  }, [datasetId])
  const jobs = [...jobUpdates, ...(overview?.jobs || []).filter(job => !jobUpdates.some(update => update.id === job.id))]
  const logActive = jobs.some(job => job.id === selectedLog && active(job.state))
  useEffect(() => {
    if (!selectedLog) return
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>; let cursor: string | undefined
    setLogs([]); setLogError('')
    const load = async () => {
      try {
        const value = await api.jobLogs(selectedLog, cursor, controller.signal)
        if (controller.signal.aborted) return
        setLogs(previous => [...previous, ...value.items].filter((event, index, array) => array.findIndex(other => other.seq === event.seq) === index).slice(-500)); setLogError('')
        if (value.next_cursor !== null) cursor = String(value.next_cursor)
        if (value.items.length >= 100 || logActive) timer = setTimeout(load, 2000)
      } catch (value) { if (!controller.signal.aborted) setLogError(describeError(value)) }
    }
    void load(); return () => { controller.abort(); clearTimeout(timer) }
  }, [selectedLog, logActive])
  useEffect(() => {
    if (!selectedPolicy) return
    const controller = new AbortController(); setDetail(null); setDetailError('')
    api.investingArtifact('policies', selectedPolicy, controller.signal).then(value => { if (!controller.signal.aborted) setDetail(value) }).catch(value => { if (!controller.signal.aborted) setDetailError(describeError(value)) })
    return () => controller.abort()
  }, [selectedPolicy])
  const submit = async (operation: () => Promise<JobRecord>) => {
    setBusy(true); setError(''); setNotice('')
    try { const job = await operation(); if (alive.current) { setJobUpdates(previous => [job, ...previous.filter(item => item.id !== job.id)]); setNotice(`任务 ${job.id} 已提交：${states[job.state] || job.state}`) } }
    catch (value) { if (alive.current) setError(describeError(value)) } finally { if (alive.current) setBusy(false) }
  }
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
  const backtestPayload = async () => {
    if (!datasetId || !policyId) throw new Error('请选择日线数据版本与冻结策略；基准也必须引用同一策略实验。')
    validateDate(backtest.start); validateDate(backtest.end)
    if (backtest.start >= backtest.end) throw new Error('回测开始必须早于结束。')
    const policy = await api.investingArtifact('policies', policyId), config = record(policy.manifest.config)
    const cutoff = String(config.validation_end || policy.manifest.cutoff || '')
    if (cutoff && backtest.start <= cutoff) throw new Error('回测开始必须晚于策略验证截止日期。')
    number(backtest.cost_multiplier, '成本倍数', 0, 100)
    return { dataset_id: datasetId, policy_id: policyId, spec: backtest }
  }
  const setExecution = (key: keyof PolicyTrainingSpec['backtest']['execution'], value: number) => setTrain({ ...train, backtest: { ...train.backtest, execution: { ...train.backtest.execution, [key]: value } } })
  const setRisk = (key: keyof PolicyTrainingSpec['backtest']['risk'], value: number) => setTrain({ ...train, backtest: { ...train.backtest, risk: { ...train.backtest.risk, [key]: value } } })
  return <div className="page-stack learning-page investing-page">
    <header className="page-heading section-heading"><div><h1>ETF 自主投资</h1><p>日线观察 · 自然周 / 月决策 · 扣费后净值增长驱动 PPO 学习</p></div><button disabled={busy} onClick={() => setRefresh(value => value + 1)}>刷新</button></header>
    <section className="panel"><h2>中长期策略研究</h2><p>策略可以持有数周至数月、保持现有份额（Hold），或持有现金等待。周 / 月末收盘决策，下一交易日开盘成交；每日风险约束持续生效。</p><p>奖励为账户净值的对数变化，费用已计入净值。验证段选取最佳权重，随后仅评估一次测试段；测试不参与本次选模。</p><p className="muted">所有区间均为日历日期闭区间，训练结束 &lt; 验证开始，验证结束 &lt; 测试开始。冻结回测必须从验证截止之后开始。</p></section>
    {overviewError && <p role="alert" className="callout error">{overviewError}</p>}{error && <p role="alert" className="callout error">{error}</p>}{notice && <p role="status" className="callout success">{notice}</p>}
    {selectedResult && <InvestingResults key={selectedResult} id={selectedResult} />}
    <section className="panel"><h2>1 · 日线数据版本</h2><label>训练 / 回测数据版本<select disabled={busy} value={datasetId} onChange={event => setDatasetId(event.target.value)}><option value="">请选择日线版本</option>{overview?.datasets.map(item => <option key={item.id} value={item.id}>{item.id} · {item.manifest.data_start}–{item.manifest.data_end}</option>)}</select></label>{dataset && <><p>{dataset.manifest.price_mode} · {dataset.manifest.data_start} 至 {dataset.manifest.data_end}</p><p className="muted">{dataset.manifest.limitations.join('；')}</p><JsonDetails title="版本身份与来源限制" value={dataset} /></>}{overview && !overview.datasets.length && <p>暂无日线版本。请到 <Link to="/datasets">数据版本</Link> 准备或导入日线数据。</p>}{overview && <p className="muted">设备 {overview.capabilities.device_name || 'CPU'} · CUDA {overview.capabilities.cuda_available ? '可用' : '不可用'} · 小型 PPO 默认 CPU。</p>}{overview && (!overview.capabilities.torch_available || overview.capabilities.ppo_available === false) && <p className="callout error">训练依赖尚未安装，请安装 learning 依赖。</p>}</section>
    <section className="panel"><h2>2 · PPO 策略训练</h2><form noValidate onSubmit={event => { event.preventDefault(); void submit(() => api.trainInvesting(trainPayload())) }}><fieldset disabled={busy}><div className="form-grid"><label>策略名称<input value={train.name} onChange={event => setTrain({ ...train, name: event.target.value })} /></label><label>决策频率<select value={train.frequency} onChange={event => setTrain({ ...train, frequency: event.target.value as PolicyTrainingSpec['frequency'] })}><option value="weekly">自然周</option><option value="monthly">自然月</option></select></label><label>固定 ETF 篮子（按顺序，逗号分隔）<input value={symbols} onChange={event => setSymbols(event.target.value)} /></label><label>初始资金<input type="number" min={1} value={train.backtest.initial_cash} onChange={event => setTrain({ ...train, backtest: { ...train.backtest, initial_cash: Number(event.target.value) } })} /></label>{dateFields.map((key, index) => <label key={key}>{dateLabels[index]}<input type="date" value={train[key]} onChange={event => setTrain({ ...train, [key]: event.target.value })} /></label>)}</div>
      <details><summary>高级训练、成交与风险参数</summary><div className="form-grid">{numericFields.map(([key, label, min, step, max]) => <label key={key}>{label}<input type="number" min={min} max={max} step={step} value={train[key] as number} onChange={event => setTrain({ ...train, [key]: Number(event.target.value) })} /></label>)}<label>训练设备<select value={train.device} onChange={event => setTrain({ ...train, device: event.target.value as 'cpu' | 'cuda' })}><option value="cpu">CPU</option><option value="cuda" disabled={!overview?.capabilities.cuda_available}>CUDA</option></select></label>{([['commission_rate', '佣金率', .0001], ['minimum_commission', '最低佣金', 1], ['slippage_rate', '滑点率', .0001], ['max_stale_sessions', '停牌估值上限（交易日）', 1]] as const).map(([key, label, step]) => <label key={key}>{label}<input type="number" min={0} step={step} value={train.backtest.execution[key]} onChange={event => setExecution(key, Number(event.target.value))} /></label>)}{([['max_total_weight', '总仓位上限'], ['max_symbol_weight', '单标的仓位上限'], ['drawdown_pause', '回撤暂停阈值']] as const).map(([key, label]) => <label key={key}>{label}<input type="number" min={0} max={1} step={.01} value={train.backtest.risk[key] ?? ''} onChange={event => setRisk(key, Number(event.target.value))} /></label>)}</div></details><JsonDetails title="完整训练配置（日期与篮子以表单为准）" value={{ ...train, backtest: { ...train.backtest, start: train.train_start, end: train.test_end, symbols: symbols.split(/[,，\s]+/).filter(Boolean), seed: train.seed } }} /><button disabled={!datasetId}>提交训练</button></fieldset></form></section>
    <section className="panel"><h2>3 · 冻结策略与评估</h2>{!overview?.policies.length && <p>训练成功后会在此登记冻结策略。</p>}<div className="record-list">{overview?.policies.map(item => <article className="record" key={item.id}><div className="record-head"><strong>{name(item)}</strong><span>{item.id}</span><button onClick={() => setSelectedPolicy(item.id)}>查看策略 {item.id}</button></div><p>验证截止 {String(record(item.manifest.config).validation_end || item.manifest.cutoff || '—')}</p></article>)}</div>{selectedPolicy && <div>{detailError && <p role="alert" className="error">{detailError}</p>}{!detail && !detailError && <p>加载策略详情…</p>}{detail && <><h3>验证段指标</h3><Metrics values={record(record(detail.manifest.metrics).validation)} /><h3>测试段指标</h3><Metrics values={record(record(detail.manifest.metrics).test)} /><p>选模标准：验证净对数收益（validation_net_log_return）</p><JsonDetails title="验证轮次、实际设备与策略完整配置" value={detail.manifest} /></>}</div>}</section>
    <section className="panel"><h2>4 · 冻结回测与基准</h2><form noValidate onSubmit={event => { event.preventDefault(); void submit(async () => api.backtestInvesting(await backtestPayload())) }}><fieldset disabled={busy}><div className="form-grid"><label>参考冻结策略<select value={policyId} onChange={event => setPolicyId(event.target.value)}><option value="">请选择冻结策略</option>{overview?.policies.map(item => <option key={item.id} value={item.id}>{name(item)} · {item.id}</option>)}</select></label><label>回测方法<select value={backtest.baseline} onChange={event => setBacktest({ ...backtest, baseline: event.target.value as PolicyBacktestSpec['baseline'] })}><option value="policy">PPO 冻结策略</option><option value="cash">现金基准</option><option value="equal_weight">等权基准</option><option value="buy_hold">买入持有基准</option></select></label>{(['start', 'end'] as const).map((key, index) => <label key={key}>{index ? '回测结束' : '回测开始'}<input type="date" value={backtest[key]} onChange={event => setBacktest({ ...backtest, [key]: event.target.value })} /></label>)}<label>成本倍数<input type="number" min={0} step={.1} value={backtest.cost_multiplier} onChange={event => setBacktest({ ...backtest, cost_multiplier: Number(event.target.value) })} /></label></div><p className="muted">四种方法均引用同一策略实验的固定篮子、资金、频率、风险和成交配置；成本倍数为显式压力实验。冻结策略仅推断，不继续训练。</p><button disabled={!datasetId || !policyId}>提交回测</button></fieldset></form></section>
    <section className="panel"><h2>任务进度与日志</h2>{!jobs.length && <p>暂无自主投资任务。</p>}<div className="record-list">{jobs.map(job => <article className="record" key={job.id}><div className="record-head"><strong>{job.id}</strong><span className="badge">{states[job.state] || job.state}</span><button onClick={() => setSelectedLog(job.id)}>查看日志 {job.id}</button>{active(job.state) && <button disabled={busy || job.state === 'cancel_requested'} onClick={() => void submit(() => api.cancelJob(job.id))}>取消 {job.id}</button>}</div><p className="muted">进度 {job.completed_through || '—'} · 心跳 {taskTime(job.heartbeat)}</p>{job.error && <p className="error">{job.error}</p>}</article>)}</div>{selectedLog && <div><h3>任务日志 · {selectedLog}</h3>{logError && <p role="alert" className="error">{logError}</p>}<ol className="event-timeline">{logs.map(event => <li key={event.seq}><small>{event.phase} · {taskTime(event.asof)}</small><p>{event.message}</p></li>)}</ol><p className="muted">保留最近 500 条事件。取消的训练不会登记成功策略。</p></div>}</section>
    <section className="panel"><h2>回测记录</h2><p>在 <Link to="/runs">全部回测</Link> 查看状态、指标和跨方法比较；选择一条 PPO 回测可在此打开完整结果。</p></section>
  </div>
}
