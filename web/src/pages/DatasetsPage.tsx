import { useEffect, useState } from 'react'
import { api, describeError } from '../api'
import type { DatasetVersion, JobRecord, Preflight } from '../types'

const terminal = new Set(['succeeded', 'failed', 'cancelled', 'interrupted'])
const mode = (value: string) => value === 'raw_cash' ? '原始价格现金账户' : '复权研究价'
const kindLabel: Record<string, string> = { demo: '演示生成', fetch: '行情获取', import: 'ZIP 导入' }
const stateLabel: Record<string, string> = { queued: '排队中', running: '运行中', cancel_requested: '等待取消', succeeded: '已结束', failed: '失败', cancelled: '已取消', interrupted: '中断' }
const appendUnique = <T extends { id: string }>(old: T[], next: T[]) => [...old, ...next.filter(item => !old.some(existing => existing.id === item.id))]

export function DatasetsPage() {
  const [datasets, setDatasets] = useState<DatasetVersion[]>([])
  const [jobs, setJobs] = useState<JobRecord[]>([])
  const [datasetCursor, setDatasetCursor] = useState<string | null>(null)
  const [jobCursor, setJobCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadFailed, setLoadFailed] = useState(false)
  const [error, setError] = useState('')
  const [symbols, setSymbols] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [cacheVersion, setCacheVersion] = useState('')
  const [years, setYears] = useState(1)
  const [seed, setSeed] = useState(7)
  const [file, setFile] = useState<File | null>(null)
  const [uploadId, setUploadId] = useState('')
  const [preflight, setPreflight] = useState<Preflight | null>(null)
  const load = async () => { const [data, work] = await Promise.all([api.datasets(), api.jobs()]); const items = work.items.filter(item => ['demo', 'fetch', 'import'].includes(item.kind)); return { data, work, jobs: await Promise.all(items.map(item => api.job(item.id))) } }
  const refresh = async () => { const next = await load(); setDatasets(old => appendUnique(next.data.items, old)); setJobs(old => appendUnique(next.jobs, old)); setLoadFailed(false) }
  useEffect(() => { let alive = true; load().then(next => { if (alive) { setDatasets(next.data.items); setDatasetCursor(next.data.next_cursor); setJobs(next.jobs); setJobCursor(next.work.next_cursor); setLoading(false) } }).catch(value => { if (alive) { setError(describeError(value)); setLoadFailed(true); setLoading(false) } }); return () => { alive = false } }, [])
  const loadMoreDatasets = async () => { if (!datasetCursor) return; try { const page = await api.datasets(datasetCursor); setDatasets(old => appendUnique(old, page.items)); setDatasetCursor(page.next_cursor) } catch (value) { setError(describeError(value)) } }
  const loadMoreJobs = async () => { if (!jobCursor) return; try { const page = await api.jobs(jobCursor); const items = page.items.filter(item => ['demo', 'fetch', 'import'].includes(item.kind)); const detailed = await Promise.all(items.map(item => api.job(item.id))); setJobs(old => appendUnique(old, detailed)); setJobCursor(page.next_cursor) } catch (value) { setError(describeError(value)) } }
  useEffect(() => { if (!jobs.some(job => !terminal.has(job.state))) return; const timer = window.setInterval(async () => { try { await refresh() } catch (value) { setError(describeError(value)) } }, 2000); return () => clearInterval(timer) }, [jobs.map(job => `${job.id}:${job.state}`).join('|')])
  const submit = async (kind: 'demo' | 'fetch', payload: Record<string, unknown>) => { setError(''); try { const job = await api.dataJob(kind, payload); setJobs(old => [...old, job]) } catch (value) { setError(describeError(value)) } }
  const upload = async () => { if (!file) return; setError(''); setPreflight(null); try { const result = await api.upload(file); const inspected = await api.preflight(result.upload_id); setUploadId(result.upload_id); setPreflight(inspected) } catch (value) { setError(describeError(value)) } }
  const importFile = async () => { setError(''); try { const job = await api.importDataset(uploadId); setJobs(old => [...old, job]); setUploadId(''); setPreflight(null); setFile(null) } catch (value) { setError(describeError(value)) } }
  return <div className="page-stack"><header className="page-heading"><h1>数据版本</h1><p>数据导入与获取先经过任务验证。版本及覆盖以服务端登记结果为准。</p></header>
    {error && <p role="alert" className="error callout">{error}</p>}
    <section className="panel"><div className="section-heading"><div><h2>已登记数据</h2><p>选择实验时将冻结其中一个版本。</p></div><button onClick={() => refresh().catch(value => setError(describeError(value)))}>刷新</button></div>
      {loading ? <p>加载中…</p> : loadFailed ? <p className="empty">无法读取数据版本，请检查上方错误并刷新。</p> : datasets.length === 0 ? <p className="empty">尚无已验证数据版本。可生成演示数据、获取行情或上传 ZIP。</p> : <div className="record-list">{datasets.map(data => <article key={data.id} className="record"><div className="record-head"><strong>{data.manifest.source}</strong><span className="badge">{data.manifest.is_demo ? '演示数据' : '真实来源声明'}</span><span className="badge">{mode(data.manifest.price_mode)}</span></div><small>版本 {data.id}</small><p>价格覆盖 {data.manifest.data_start} — {data.manifest.data_end} · 日历 {data.manifest.calendar_start} — {data.manifest.calendar_end}</p><p>事件覆盖：{data.manifest.event_coverage} · 基准：{data.manifest.benchmark_type || '未声明'}</p>{data.manifest.limitations?.length > 0 && <p className="muted">限制：{data.manifest.limitations.join('；')}</p>}</article>)}</div>}{datasetCursor && <button type="button" onClick={loadMoreDatasets}>加载更多数据版本</button>}</section>
    <div className="two-column"><section className="panel"><h2>生成演示数据</h2><p className="muted">仅供流程演示，不代表真实可交易数据。</p><div className="form-grid"><label>年数<input type="number" min="1" max="20" value={years} onChange={event => setYears(Number(event.target.value))} /></label><label>种子<input type="number" step="1" value={seed} onChange={event => setSeed(Number(event.target.value))} /></label></div><button onClick={() => submit('demo', { years, seed })}>生成演示</button></section>
      <section className="panel"><h2>获取 ETF 数据</h2><p className="muted">实际覆盖由任务返回；缺失交易日时不会登记可用版本。</p><div className="form-grid"><label>ETF 代码（逗号分隔）<input value={symbols} onChange={event => setSymbols(event.target.value)} placeholder="000001" /></label><label>起始日<input type="date" value={start} onChange={event => setStart(event.target.value)} /></label><label>结束日<input type="date" value={end} onChange={event => setEnd(event.target.value)} /></label><label>缓存版本（可选）<input value={cacheVersion} onChange={event => setCacheVersion(event.target.value)} /></label></div><button onClick={() => submit('fetch', { symbols: symbols.split(/[,，\s]+/).filter(Boolean), start_date: start, end_date: end, ...(cacheVersion ? { cache_version: cacheVersion } : {}) })}>发起获取</button></section></div>
    <section className="panel"><h2>导入 ZIP 数据包</h2><p className="muted">ZIP 包含 manifest.json 与六张基础表。预检只检查声明元数据，任务完成后才确认实际数据。</p><div className="inline-actions"><label>ZIP 文件<input type="file" accept=".zip,application/zip" onChange={event => { setFile(event.target.files?.[0] || null); setPreflight(null); setUploadId('') }} /></label><button disabled={!file} onClick={upload}>上传并预检</button></div>{preflight && <div className="callout"><strong>声明检查 · {preflight.validation}</strong><p>{preflight.is_demo ? '演示数据' : '真实来源声明'} · {mode(preflight.price_mode)} · 事件覆盖 {preflight.event_coverage}</p><p>价格覆盖 {preflight.data_start} — {preflight.data_end} · 日历 {preflight.calendar_start} — {preflight.calendar_end}</p><p>缺失表：{preflight.missing_tables.length ? preflight.missing_tables.join('、') : '声明中未发现'}</p>{preflight.limitations.length > 0 && <p>限制：{preflight.limitations.join('；')}</p>}<button onClick={importFile}>提交导入验证</button></div>}</section>
    <section className="panel"><h2>数据任务</h2>{jobs.length === 0 ? <p className="empty">暂无数据任务。</p> : <div className="record-list">{[...jobs].reverse().map(job => <article className="record" key={job.id}><div className="record-head"><strong>{kindLabel[job.kind] || job.kind}</strong><span className="badge">{stateLabel[job.state] || job.state}</span></div><small>{job.id}</small>{job.heartbeat && <p>心跳 {job.heartbeat}</p>}{job.error && <p className="error">{job.error}</p>}{job.result?.state === 'complete' && job.result.dataset_version_id && <p className="success">已验证并登记数据版本 {job.result.dataset_version_id} · {mode(job.result.price_mode || '')}</p>}{job.result?.state === 'incomplete' && <div className="error"><p>获取已结束，覆盖不完整（{job.result.reason || '未知原因'}），没有登记数据版本。</p><pre>{JSON.stringify({ coverage: job.result.coverage, failed_symbols: job.result.failed_symbols }, null, 2)}</pre></div>}</article>)}</div>}{jobCursor && <button type="button" onClick={loadMoreJobs}>加载更多数据任务</button>}</section>
  </div>
}
