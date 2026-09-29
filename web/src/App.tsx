import { lazy, Suspense, useEffect, useState } from 'react'
import { Link, NavLink, Navigate, Route, Routes, useBlocker, useParams } from 'react-router-dom'
import { DatasetsPage } from './pages/DatasetsPage'
import { RunsPage } from './pages/RunsPage'
import { RunDetailPage } from './pages/RunDetailPage'
const ExperimentsPage = lazy(() => import('./pages/ExperimentsPage').then(module => ({ default: module.ExperimentsPage })))
const RunDetailRoute = () => { const { id } = useParams(); return <RunDetailPage key={id} /> }

export function App() {
  const [dirty, setDirty] = useState(false)
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname)
  useEffect(() => {
    if (blocker.state !== 'blocked') return
    if (window.confirm('当前修改尚未保存。确定离开并丢弃这些修改吗？')) { setDirty(false); blocker.proceed() }
    else blocker.reset()
  }, [blocker])
  const nav = [['/datasets', '数据版本'], ['/experiments', '策略与实验'], ['/runs', '回测任务'], ['/compare', '对比']] as const
  return <div className="app-shell">
    <aside className="sidebar"><Link className="brand" to="/datasets">Quanto <small>研究工作台</small></Link><nav aria-label="主导航">{nav.map(([path, label]) => <NavLink key={path} to={path}>{label}</NavLink>)}</nav><p className="sidebar-note">本地回测 · A 股行业 ETF</p></aside>
    <main className="main"><Suspense fallback={<p>加载工作台…</p>}><Routes><Route path="/" element={<Navigate to="/datasets" replace />} /><Route path="/datasets" element={<DatasetsPage />} /><Route path="/experiments" element={<ExperimentsPage onDirtyChange={setDirty} />} /><Route path="/runs" element={<RunsPage />} /><Route path="/runs/:id" element={<RunDetailRoute />} /><Route path="/compare" element={<div className="panel"><h1>对比</h1><p>对比视图将在结果工作流接入后显示。</p></div>} /></Routes></Suspense></main>
  </div>
}
