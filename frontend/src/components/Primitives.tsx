import type { PropsWithChildren } from 'react'

export function Card({ title, children }: PropsWithChildren<{ title?: string }>) {
  return (
    <section className="card">
      {title && <h2>{title}</h2>}
      {children}
    </section>
  )
}

export function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null
  return <div className="error-banner">{message}</div>
}

export function Button(
  props: PropsWithChildren<{
    onClick: () => void
    disabled?: boolean
    variant?: 'primary' | 'secondary' | 'danger'
  }>,
) {
  const { onClick, disabled, variant = 'primary', children } = props
  return (
    <button className={`btn btn-${variant}`} onClick={onClick} disabled={disabled}>
      {children}
    </button>
  )
}

export function Spinner() {
  return <span className="spinner" aria-label="loading" />
}

export function StatPill({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="stat-pill">
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  )
}
