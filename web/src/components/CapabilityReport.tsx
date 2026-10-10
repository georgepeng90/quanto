import type { CapabilityReport as Report } from '../frameworkTypes'

export function CapabilityReport({ report }: { report: Report }) {
  return <section className="callout framework-detail" aria-label="能力检查报告">
    <strong>{report.status === 'ready' ? '服务端检查：可执行' : '服务端检查：已阻止'}</strong>
    <p>操作：{report.operation} · 检查版本：{report.checker_version}</p>
    {report.checks.length > 0 && <ul>{report.checks.map((check, index) => <li key={`${check.name}-${index}`}>
      {check.status === 'blocked' ? '阻止' : '通过'} · {check.name}{check.field_path && ` · ${check.field_path}`}{check.code && ` · ${check.code}`}{check.message && `：${check.message}`}
    </li>)}</ul>}
    {report.assumptions.length > 0 && <p>前提：{report.assumptions.join('；')}</p>}
    {report.limitations.length > 0 && <p>限制：{report.limitations.join('；')}</p>}
  </section>
}
