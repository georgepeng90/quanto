import { Link, useSearchParams } from 'react-router-dom'
import { LearningResults } from '../components/LearningResults'

export function LearningPage() {
  const [params] = useSearchParams()
  const id = params.get('backtest')
  return <div className="page-stack learning-page"><header className="page-heading"><h1>历史分钟回测报告</h1><Link to="/runs">全部回测</Link></header>{id ? <LearningResults key={id} id={id} /> : <p>请从回测目录选择历史报告。</p>}</div>
}
