import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Link, NavLink, Navigate, Route, Routes, useBlocker, useParams, useSearchParams } from 'react-router-dom'
import { DatasetsPage } from './pages/DatasetsPage'
import { RunsPage } from './pages/RunsPage'
const ModelsPage = lazy(() => import('./pages/ModelsPage').then(module => ({ default: module.ModelsPage })))
const ModelDetailPage = lazy(() => import('./pages/ModelDetailPage').then(module => ({ default: module.ModelDetailPage })))
const ModelDetailRoute = () => { const { id } = useParams(); return <ModelDetailPage key={id} /> }
const ExperimentsPage = lazy(() => import('./pages/ExperimentsPage').then(module => ({ default: module.ExperimentsPage })))
const ExperimentListPage = lazy(() => import('./pages/ExperimentListPage').then(module => ({ default: module.ExperimentListPage })))
const ExperimentListRoute = () => { const [searchParams] = useSearchParams(); const draft = searchParams.get('draft'); return draft ? <Navigate to={`/experiments/${encodeURIComponent(draft)}`} replace /> : <ExperimentListPage /> }
const ExperimentEditorRoute = ({ onDirtyChange, allowNextNavigation }: { onDirtyChange: (dirty: boolean) => void; allowNextNavigation: () => void }) => <ExperimentsPage onDirtyChange={onDirtyChange} allowNextNavigation={allowNextNavigation} />
const RunDetailPage = lazy(() => import('./pages/RunDetailPage').then(module => ({ default: module.RunDetailPage })))
const ComparePage = lazy(() => import('./pages/ComparePage').then(module => ({ default: module.ComparePage })))
const LearningPage = lazy(() => import('./pages/LearningPage').then(module => ({ default: module.LearningPage })))
const InvestingPage = lazy(() => import('./pages/InvestingPage').then(module => ({ default: module.InvestingPage })))
const RunDetailRoute = () => { const { id } = useParams(); return <RunDetailPage key={id} /> }

export function App() {
  const [dirty, setDirty] = useState(false)
  const bypassNextBlock = useRef(false)
  const allowNextNavigation = () => { bypassNextBlock.current = true }
  const blocker = useBlocker(({ currentLocation, nextLocation }) => {
    if (bypassNextBlock.current) { bypassNextBlock.current = false; return false }
    return dirty && currentLocation.pathname !== nextLocation.pathname
  })
  useEffect(() => {
    if (blocker.state !== 'blocked') return
    if (window.confirm('当前修改尚未保存。确定离开并丢弃这些修改吗？')) { setDirty(false); blocker.proceed() }
    else blocker.reset()
  }, [blocker])
  const nav = [['/datasets', '行情数据'], ['/experiments', '固定策略'], ['/models', '强化学习模型'], ['/runs', '回测']] as const
  return <div className="app-shell">
    <aside className="sidebar"><Link className="brand" to="/datasets">Quanto <small>研究工作台</small></Link><nav aria-label="主导航">{nav.map(([path, label]) => <NavLink key={path} to={path}>{label}</NavLink>)}</nav><p className="sidebar-note">本地回测 · A 股行业 ETF</p></aside>
    <main className="main"><Suspense fallback={<p>加载工作台…</p>}><Routes><Route path="/" element={<Navigate to="/datasets" replace />} /><Route path="/datasets" element={<DatasetsPage />} /><Route path="/experiments" element={<ExperimentListRoute />} /><Route path="/experiments/new" element={<ExperimentEditorRoute onDirtyChange={setDirty} allowNextNavigation={allowNextNavigation} />} /><Route path="/experiments/:id" element={<ExperimentEditorRoute onDirtyChange={setDirty} allowNextNavigation={allowNextNavigation} />} /><Route path="/models" element={<ModelsPage />} /><Route path="/models/:id" element={<ModelDetailRoute />} /><Route path="/runs" element={<RunsPage />} /><Route path="/runs/:id" element={<RunDetailRoute />} /><Route path="/compare" element={<ComparePage />} /><Route path="/investing" element={<InvestingPage />} /><Route path="/learning" element={<LearningPage />} /></Routes></Suspense></main>
  </div>
}
