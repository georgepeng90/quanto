import type { CapabilityReport as Report } from '../frameworkTypes'

export function CapabilityReport({ report }: { report: Report }) {
  return <section className="callout framework-detail" aria-label="能力检查报告">
    <strong>{report.status === 'ready' ? '服务端检查：可执行' : '服务端检查：已阻止'}</strong>
    <p>操作：{report.operation}</p>
    {report.checks.length > 0 && <ul>{report.checks.map((check, index) => <li key={`${check.name}-${index}`}>
      {check.status === 'blocked' ? '阻止' : '通过'} · {check.message || check.name}
    </li>)}</ul>}
    {report.assumptions.length > 0 && <p>前提：{report.assumptions.join('；')}</p>}
    {report.limitations.length > 0 && <p>限制：{report.limitations.join('；')}</p>}
    <details><summary>检查详情</summary><pre>{JSON.stringify(report, null, 2)}</pre></details>
  </section>
}
