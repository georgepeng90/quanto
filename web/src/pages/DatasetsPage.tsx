import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, describeError } from '../api'
import type { DatasetVersion, JobRecord, Preflight } from '../types'
import type { CapabilityReport as Report, FetchJobRequest, IndexInstrumentId, TradableInstrumentId } from '../frameworkTypes'
import { instrumentKey } from '../frameworkTypes'
import { BasketEditor } from '../components/BasketEditor'
import { CapabilityReport } from '../components/CapabilityReport'

const terminal = new Set(['succeeded', 'failed', 'cancelled', 'interrupted'])
const mode = (value: string) => value === 'raw_cash' ? '原始价格现金账户' : '复权研究价'
const kindLabel: Record<string, string> = { demo: '演示生成', fetch: '行情获取', import: 'ZIP 导入' }
const stateLabel: Record<string, string> = { queued: '排队中', running: '运行中', cancel_requested: '等待取消', succeeded: '已结束', failed: '失败', cancelled: '已取消', interrupted: '中断' }
const marketState: Record<string, string> = { queued: '准备中', running: '准备中', ready: '已就绪', incomplete: '数据不完整', failed: '失败', cancelled: '取消' }
const usable = (job: JobRecord) => job.result?.state === 'complete' && job.result.dataset_version_id && (job.kind !== 'fetch' || job.result.market_prefetch_state === 'ready')
const appendUnique = <T extends { id: string }>(old: T[], next: T[]) => [...old, ...next.filter(item => !old.some(existing => existing.id === item.id))]

function DatasetIdentity({ id }: { id: string }) {
  const [detail, setDetail] = useState<DatasetVersion | null>(null)
  const [error, setError] = useState('')
  const [binding, setBinding] = useState(false)
  const [bindingKey, setBindingKey] = useState('')
  const load = async () => { try { setDetail(await api.dataset(id)); setError('') } catch (value) { setError(describeError(value)) } }
  useEffect(() => { void load() }, [id])
  const bind = async () => {
    if (!detail?.identity_evidence_ref) return
    const key = bindingKey || crypto.randomUUID()
    setBindingKey(key); setBinding(true); setError('')
    try { await api.bindDataset(id, { evidence_ref: detail.identity_evidence_ref, idempotency_key: key }); setBindingKey(''); await load() }
    catch (value) { setError(describeError(value)) }
    finally { setBinding(false) }
  }
  return <div className="callout framework-detail"><strong>身份核验</strong>
    {error && <p role="alert" className="error">{error}</p>}
    {!detail ? <p>读取身份资料…</p> : detail.asset_binding ? <><p className="success">已绑定 · {detail.asset_binding.binding_hash}</p><p>标的：{detail.asset_binding.assets.map(item => instrumentKey(item.instrument_id)).join('、')}</p></> : <>
      {detail.identity_binding_issue && <p>{detail.identity_binding_issue.code}：{detail.identity_binding_issue.message}</p>}
      {detail.identity_evidence_ref ? <><p>已有来源证据：{detail.identity_evidence_ref}</p><button type="button" disabled={binding} onClick={bind}>{binding ? '关联中…' : '检查并关联身份'}</button></> : <p>缺少可核验的身份来源。请重新导入包含 identity_evidence.json 证券身份资料的数据包。</p>}
    </>}
  </div>
}

export function DatasetsPage() {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') === 'baskets' ? 'baskets' : 'versions'
  return <div className="page-stack"><header className="page-heading"><h1>数据</h1><p>标的选择、数据版本和身份来源。</p></header>
    <nav aria-label="数据页面" className="inline-actions"><button type="button" aria-current={tab === 'baskets' ? 'page' : undefined} onClick={() => setParams({ tab: 'baskets' })}>标的篮子</button><button type="button" aria-current={tab === 'versions' ? 'page' : undefined} onClick={() => setParams({ tab: 'versions' })}>数据版本</button></nav>
    {tab === 'baskets' ? <BasketEditor /> : <DataVersions />}
  </div>
}

function DataVersions() {
  const [datasets, setDatasets] = useState<DatasetVersion[]>([])
  const [jobs, setJobs] = useState<JobRecord[]>([])
  const [datasetCursor, setDatasetCursor] = useState<string | null>(null)
  const [jobCursor, setJobCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadFailed, setLoadFailed] = useState(false)
  const [error, setError] = useState('')
  const [instruments, setInstruments] = useState<TradableInstrumentId[]>([{ exchange: 'SSE', instrument_type: 'etf', code: '' }])
  const [benchmark, setBenchmark] = useState<IndexInstrumentId>({ exchange: 'SSE', instrument_type: 'index', code: '000300' })
  const [fetchReport, setFetchReport] = useState<Report | null>(null)
  const fetchVersion = useRef(0)
  const [openDataset, setOpenDataset] = useState<string | null>(null)
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [updateMarket, setUpdateMarket] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [uploadId, setUploadId] = useState('')
  const [preflight, setPreflight] = useState<Preflight | null>(null)
  const load = async () => { const [data, work] = await Promise.all([api.datasets(), api.jobs()]); const items = work.items.filter(item => ['demo', 'fetch', 'import'].includes(item.kind)); return { data, work, jobs: await Promise.all(items.map(item => api.job(item.id))) } }
  const refresh = async () => { const next = await load(); const listed = new Set(next.jobs.map(job => job.id)); const tracked = await Promise.all(jobs.filter(job => !terminal.has(job.state) && !listed.has(job.id)).map(job => api.job(job.id))); setDatasets(old => appendUnique(next.data.items, old)); setJobs(old => appendUnique([...next.jobs, ...tracked], old)); setDatasetCursor(old => old ?? next.data.next_cursor); setJobCursor(old => old ?? next.work.next_cursor); setLoadFailed(false) }
  useEffect(() => { let alive = true; load().then(next => { if (alive) { setDatasets(next.data.items); setDatasetCursor(next.data.next_cursor); setJobs(next.jobs); setJobCursor(next.work.next_cursor); setLoading(false) } }).catch(value => { if (alive) { setError(describeError(value)); setLoadFailed(true); setLoading(false) } }); return () => { alive = false } }, [])
  const loadMoreDatasets = async () => { if (!datasetCursor) return; try { const page = await api.datasets(datasetCursor); setDatasets(old => appendUnique(old, page.items)); setDatasetCursor(page.next_cursor) } catch (value) { setError(describeError(value)) } }
  const loadMoreJobs = async () => { if (!jobCursor) return; try { const page = await api.jobs(jobCursor); const items = page.items.filter(item => ['demo', 'fetch', 'import'].includes(item.kind)); const detailed = await Promise.all(items.map(item => api.job(item.id))); setJobs(old => appendUnique(old, detailed)); setJobCursor(page.next_cursor) } catch (value) { setError(describeError(value)) } }
  useEffect(() => { if (!jobs.some(job => !terminal.has(job.state))) return; const timer = window.setInterval(async () => { try { await refresh() } catch (value) { setError(describeError(value)) } }, 2000); return () => clearInterval(timer) }, [jobs.map(job => `${job.id}:${job.state}`).join('|')])
  const invalidateFetch = () => { fetchVersion.current += 1; setFetchReport(null) }
  const submit = async () => { const version = ++fetchVersion.current; setError(''); setFetchReport(null); try { const payload: FetchJobRequest = { instruments: instruments.map(item => ({ ...item, exchange: item.exchange.trim().toUpperCase(), code: item.code.trim() })), benchmark: { ...benchmark, exchange: benchmark.exchange.trim().toUpperCase(), code: benchmark.code.trim() }, start_date: start, end_date: end, refresh: updateMarket }; const { refresh, ...check } = payload; const report = await api.checkCapabilities({ operation: 'fetch', ...check }); if (fetchVersion.current !== version) return; setFetchReport(report); if (report.status !== 'ready') return; const job = await api.dataJob('fetch', payload); setJobs(old => [...old, job]) } catch (value) { if (fetchVersion.current === version) setError(describeError(value)) } }
  const upload = async () => { if (!file) return; setError(''); setPreflight(null); try { const result = await api.upload(file); const inspected = await api.preflight(result.upload_id); setUploadId(result.upload_id); setPreflight(inspected) } catch (value) { setError(describeError(value)) } }
  const importFile = async () => { setError(''); try { const job = await api.importDataset(uploadId); setJobs(old => [...old, job]); setUploadId(''); setPreflight(null); setFile(null) } catch (value) { setError(describeError(value)) } }
  return <div className="page-stack"><header className="page-heading"><h2>数据版本</h2><p>数据导入与获取先经过任务验证。版本及覆盖以服务端登记结果为准。</p></header>
    {error && <p role="alert" className="error callout">{error}</p>}
    <section className="panel"><div className="section-heading"><div><h2>已登记数据</h2><p>选择实验时将冻结其中一个版本。</p></div><button onClick={() => refresh().catch(value => setError(describeError(value)))}>刷新</button></div>
      {loading ? <p>加载中…</p> : loadFailed ? <p className="empty">无法读取数据版本，请检查上方错误并刷新。</p> : datasets.length === 0 ? <p className="empty">尚无已验证数据版本。可获取行情或上传 ZIP。</p> : <div className="record-list">{datasets.map(data => <article key={data.id} className="record"><div className="record-head"><strong>{data.manifest.source}</strong><span className="badge">{data.manifest.is_demo ? '演示数据' : '真实来源声明'}</span><span className="badge">{mode(data.manifest.price_mode)}</span></div><small>版本 {data.id}</small><p>价格覆盖 {data.manifest.data_start} — {data.manifest.data_end} · 日历 {data.manifest.calendar_start} — {data.manifest.calendar_end}</p><p>事件覆盖：{data.manifest.event_coverage} · 基准：{data.manifest.benchmark_type || '未声明'}</p>{data.manifest.limitations?.length > 0 && <p className="muted">限制：{data.manifest.limitations.join('；')}</p>}<button type="button" onClick={() => setOpenDataset(old => old === data.id ? null : data.id)}>{openDataset === data.id ? '收起身份' : '查看身份'}</button>{openDataset === data.id && <DatasetIdentity id={data.id} />}</article>)}</div>}{datasetCursor && <button type="button" onClick={loadMoreDatasets}>加载更多数据版本</button>}</section>
    <section className="panel"><h2>获取数据</h2><p className="muted">明确填写规范身份。实际覆盖由任务返回；获取成功后仍须对具体策略或模型配置重新检查能力。</p>
      {instruments.map((item, index) => <div key={index} className="form-grid">
        <label>标的交易所<input value={item.exchange} onChange={event => { invalidateFetch(); setInstruments(old => old.map((value, position) => position === index ? { ...value, exchange: event.target.value } : value)) }} /></label>
        <label>标的类型<select value={item.instrument_type} onChange={event => { invalidateFetch(); setInstruments(old => old.map((value, position) => position === index ? { ...value, instrument_type: event.target.value as TradableInstrumentId['instrument_type'] } : value)) }}><option value="etf">ETF</option><option value="stock">股票</option></select></label>
        <label>标的代码<input value={item.code} onChange={event => { invalidateFetch(); setInstruments(old => old.map((value, position) => position === index ? { ...value, code: event.target.value } : value)) }} /></label>
        <button type="button" disabled={instruments.length === 1} onClick={() => { invalidateFetch(); setInstruments(old => old.filter((_, position) => position !== index)) }}>移除</button>
      </div>)}
      <button type="button" onClick={() => { invalidateFetch(); setInstruments(old => [...old, { exchange: 'SSE', instrument_type: 'etf', code: '' }]) }}>添加标的</button>
      <div className="form-grid">
        <label>基准交易所<input value={benchmark.exchange} onChange={event => { invalidateFetch(); setBenchmark(old => ({ ...old, exchange: event.target.value })) }} /></label>
        <label>基准类型<select value={benchmark.instrument_type} onChange={event => { invalidateFetch(); setBenchmark(old => ({ ...old, instrument_type: event.target.value as IndexInstrumentId['instrument_type'] })) }}><option value="index">指数</option></select></label>
        <label>基准代码<input value={benchmark.code} onChange={event => { invalidateFetch(); setBenchmark(old => ({ ...old, code: event.target.value })) }} /></label>
        <label>起始日<input type="date" value={start} onChange={event => { invalidateFetch(); setStart(event.target.value) }} /></label>
        <label>结束日<input type="date" value={end} onChange={event => { invalidateFetch(); setEnd(event.target.value) }} /></label>
        <label><input type="checkbox" checked={updateMarket} onChange={event => { invalidateFetch(); setUpdateMarket(event.target.checked) }} />更新已有行情</label>
      </div><button type="button" onClick={submit}>发起获取</button>{fetchReport && <CapabilityReport report={fetchReport} />}
    </section>
    <section className="panel"><h2>导入 ZIP 数据包</h2><p className="muted">ZIP 包含 manifest.json 与六张基础表。预检只检查声明元数据，任务完成后才确认实际数据。</p><div className="inline-actions"><label>ZIP 文件<input type="file" accept=".zip,application/zip" onChange={event => { setFile(event.target.files?.[0] || null); setPreflight(null); setUploadId('') }} /></label><button disabled={!file} onClick={upload}>上传并预检</button></div>{preflight && <div className="callout"><strong>声明检查 · {preflight.validation}</strong><p>{preflight.is_demo ? '演示数据' : '真实来源声明'} · {mode(preflight.price_mode)} · 事件覆盖 {preflight.event_coverage}</p><p>价格覆盖 {preflight.data_start} — {preflight.data_end} · 日历 {preflight.calendar_start} — {preflight.calendar_end}</p><p>缺失表：{preflight.missing_tables.length ? preflight.missing_tables.join('、') : '声明中未发现'}</p>{preflight.limitations.length > 0 && <p>限制：{preflight.limitations.join('；')}</p>}<button onClick={importFile}>提交导入验证</button></div>}</section>
    <section className="panel"><h2>数据任务</h2>{jobs.length === 0 ? <p className="empty">暂无数据任务。</p> : <div className="record-list">{[...jobs].reverse().map(job => <article className="record" key={job.id}><div className="record-head"><strong>{kindLabel[job.kind] || job.kind}</strong><span className="badge">{stateLabel[job.state] || job.state}</span></div><small>{job.id}</small>{job.heartbeat && <p>心跳 {job.heartbeat}</p>}{job.error && <p className="error">{job.error}</p>}{usable(job) && <p className="success">已验证并登记数据版本 {job.result!.dataset_version_id} · {mode(job.result!.price_mode || '')}</p>}{job.kind === 'fetch' && <p>行情：{marketState[job.result?.market_prefetch_state || (terminal.has(job.state) ? job.state : 'running')] || '未就绪'}</p>}{job.result?.limitations?.length ? <p className="muted">限制：{job.result.limitations.join('；')}</p> : null}{job.result && !usable(job) && terminal.has(job.state) && <div className="error"><p>获取已结束，覆盖不完整（{job.result.reason || '未知原因'}），没有登记数据版本。</p><pre>{JSON.stringify({ coverage: job.result.coverage, failed_symbols: job.result.failed_symbols }, null, 2)}</pre></div>}</article>)}</div>}{jobCursor && <button type="button" onClick={loadMoreJobs}>加载更多数据任务</button>}</section>
  </div>
}
