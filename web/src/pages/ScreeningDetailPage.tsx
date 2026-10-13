import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, describeError } from '../api'
import { taskTime } from '../components/timePresentation'
import { instrumentKey } from '../frameworkTypes'
import type { ScreeningRecord } from '../types'

const resultFilters = [['selected', '选中'], ['excluded', '排除'], ['not_researched', '未研究'], ['all', '全部']] as const
type ResultFilter = typeof resultFilters[number][0]

const reasons: Record<string, string> = {
  missing_status: '缺少交易状态证据', missing_industry: '缺少行业证据', missing_listing_date: '缺少上市日期证据', missing_name: '缺少公司名称',
  missing_board: '缺少板块证据', missing_business_summary: '缺少业务说明', missing_calendar: '缺少已完成交易日日历',
  incomplete_calendar: '交易日历未覆盖筛选截止日期，无法核实状态和流动性时效',
  unsupported_instrument: '证券类型或交易所不支持', unsupported_board: '板块不符合条件', listing_too_recent: '上市时间不足',
  unsupported_status: '不属于普通可交易股票', excluded_status: '命中排除状态', stale_status: '状态未覆盖最近完成交易日',
  unknown_amount_unit: '成交额单位不明确', insufficient_history: '有效行情历史不足', insufficient_liquidity_window: '成交额观察窗口不足',
  liquidity_below_minimum: '平均成交额低于下限', incomplete_research: '公司说明、理由或风险未补齐', agent_exclude: 'Agent 建议排除',
  agent_not_researched: '尚未研究', industry_limit: '行业数量已达上限', target_limit: '目标数量已满',
}
function reasonText(value: string) {
  const [code, detail] = value.split(':', 2)
  if (code === 'missing_citation') return `缺少事实来源引用：${detail}`
  if (code === 'unavailable_evidence') return '引用材料在截止时点尚不可用'
  if (code === 'superseded_evidence') return '引用材料已有修订，需要更新事实'
  return reasons[value] || value
}

export function ScreeningDetailPage() {
  const { id = '' } = useParams(), navigate = useNavigate()
  const [record, setRecord] = useState<ScreeningRecord | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState<ResultFilter>('selected')
  useEffect(() => {
    const controller = new AbortController()
    setRecord(null); setError(''); setFilter('selected')
    api.screening(id, controller.signal).then(value => { if (!controller.signal.aborted) setRecord(value) }).catch(value => { if (!controller.signal.aborted) setError(describeError(value)) })
    return () => controller.abort()
  }, [id])
  const outcomes = new Map(record?.result.candidates.map(outcome => [instrumentKey(outcome.instrument_id), outcome]))
  const candidates = record?.request.candidates || []
  const filteredCandidates = candidates.filter(candidate => filter === 'all' || outcomes.get(instrumentKey(candidate.instrument_id))?.decision === filter)
  const use = async () => {
    if (!record) return
    setBusy(true); setError('')
    try { const basket = await api.useScreening(id, record.request.name, `screening-ui-${id}`); navigate(`/models?basket=${encodeURIComponent(basket.basket_id)}&revision=${basket.revision}`) }
    catch (value) { setError(describeError(value)) } finally { setBusy(false) }
  }
  const download = async () => {
    setBusy(true)
    try { const blob = await api.exportScreening(id), url = URL.createObjectURL(blob), anchor = document.createElement('a'); anchor.href = url; anchor.download = 'screening.json'; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) }
    catch (value) { setError(describeError(value)) } finally { setBusy(false) }
  }
  return <div className="page-stack"><Link to="/screenings">返回个股筛选</Link>{error && <p role="alert" className="error callout">{error}</p>}{!record && !error && <p>读取研究证据…</p>}{record && <>
    <header className="page-heading"><h1>{record.request.name}</h1><p>{record.request.purpose} · 截至 {taskTime(record.request.as_of)}</p><p>{record.request.universe_description} · 入选 {record.result.members.length} 只</p></header>
    <section className="panel"><p>资料及事实由 Agent 提交，引用可核对；数值未经过独立核实。当前选股配合历史行情回测仅用于开发研究，不能证明历史选股有效。</p>{record.result.shortfall > 0 && <p>证据与规则条件下比目标少 {record.result.shortfall} 只。</p>}
      <div className="inline-actions"><button disabled={busy || !record.result.members.length} onClick={() => void use()}>用于创建个股模型</button><button disabled={busy} onClick={() => void download()}>导出研究与证据</button></div><p className="muted">下一步保存股票组合并检查训练数据；创建组合不会启动训练。</p></section>
    <section className="panel"><h2>个股结果</h2><div className="result-tabs" role="group" aria-label="按筛选结果查看">{resultFilters.map(([value, label]) => <button key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}（{value === 'all' ? candidates.length : candidates.filter(candidate => outcomes.get(instrumentKey(candidate.instrument_id))?.decision === value).length}）</button>)}</div><p className="muted" role="status">当前显示 {filteredCandidates.length} 只 · 切换列表不影响组合和完整证据导出</p></section>
    {!filteredCandidates.length && <p className="muted">{filter === 'selected' ? '本轮没有选中的标的' : '该分类暂无标的'}</p>}
    {filteredCandidates.map(candidate => { const key = instrumentKey(candidate.instrument_id), outcome = outcomes.get(key); return <section className="panel" key={key}><div className="record-head"><h2>{candidate.name}</h2><span className="badge">{outcome?.decision === 'selected' ? '入选' : outcome?.decision === 'not_researched' ? '未研究' : '排除'}</span></div><p>{key} · {candidate.business_summary}</p>
      {!!outcome?.reasons.length && <ul>{outcome.reasons.map((reason, i) => <li key={i}>{reasonText(reason)}</li>)}</ul>}
      {([['研究理由', candidate.reasons], ['风险', candidate.risks], ['反面证据', candidate.counter_evidence], ['未知项', candidate.unknowns]] as const).map(([label, items]) => <div key={label}><h3>{label}</h3>{items.length ? <ul>{items.map((value, i) => <li key={i}>{value}</li>)}</ul> : <p className="muted">未提供</p>}</div>)}
      <details><summary>事实引用</summary>{Object.entries(candidate.citations).map(([field, cites]) => <p key={field}>{field}：{cites.map(cite => `“${cite.quote}” (${cite.evidence_id})`).join('；')}</p>)}</details>
    </section> })}
    <section className="panel"><h2>来源材料</h2>{record.request.evidence.map(source => <article className="record" key={source.id}>{/^https?:\/\//i.test(source.source_locator) ? <a href={source.source_locator} target="_blank" rel="noreferrer">{source.source_name}</a> : <strong>{source.source_name}</strong>}<p>{source.archive_level === 'excerpt' ? '摘录归档 · 有限证据，未保存完整原文' : '原文归档（提交者声明）'}</p><p>发布：{source.published_at || '未知'} · 获取：{taskTime(source.retrieved_at)}</p><details><summary>查看归档内容</summary><p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{source.text}</p></details></article>)}</section>
    <p className="muted">研究模型：{record.request.agent_model} · 提示模板：{record.request.prompt_version}</p>
  </>}</div>
}
