import type { ReactNode } from 'react'
export function Panel({ title, sub, children, className = '' }: { title: string; sub?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`proto-card ${className}`}><div className="card-h">{title}<span className="sub">{sub}</span></div>{children}</section>
}
export function Stat({ value, label, warning = false }: { value: ReactNode; label: string; warning?: boolean }) {
  return <div className="svc-stat"><div className={`sv ${warning ? 'warning-text' : ''}`}>{value}</div><div className="sl">{label}</div></div>
}
export function Note({ title, children, warning = false }: { title: string; children: ReactNode; warning?: boolean }) {
  return <div className={`note ${warning ? 'warning-note' : 'accent-note'}`}><strong>{title}</strong><div>{children}</div></div>
}
export function PageHeading({ title, description, extra }: { title: string; description: string; extra?: ReactNode }) {
  return <div className="page-heading heading-row"><div><h2>{title}</h2><div className="sec-desc">{description}</div></div>{extra}</div>
}
