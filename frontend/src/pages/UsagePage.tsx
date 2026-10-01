import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { UsageReport, UsageRow } from '../api/types'
import { Card, ErrorBanner, StatPill } from '../components/Primitives'

function formatCost(cost: number | null): string {
  return cost !== null ? `$${cost.toFixed(4)}` : '—'
}

function byStage(rows: UsageRow[]): { stage: string; tokens: number }[] {
  const totals = new Map<string, number>()
  for (const row of rows) {
    totals.set(row.stage, (totals.get(row.stage) ?? 0) + row.input_tokens + row.output_tokens)
  }
  return [...totals.entries()]
    .map(([stage, tokens]) => ({ stage, tokens }))
    .sort((a, b) => b.tokens - a.tokens)
}

export function UsagePage() {
  const [report, setReport] = useState<UsageReport | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<UsageReport>('/api/usage').then(setReport).catch((e: Error) => setError(e.message))
  }, [])

  if (error) {
    return (
      <Card title="Usage">
        <ErrorBanner message={error} />
      </Card>
    )
  }

  if (!report) {
    return <Card title="Usage" />
  }

  if (report.rows.length === 0) {
    return (
      <Card title="Usage">
        <p className="muted">
          No LLM calls recorded yet — this fills in once you generate with the LLM stage enabled
          (<code>generate --no-rules-only</code> or <code>generation.use_llm: true</code>).
        </p>
      </Card>
    )
  }

  const stages = byStage(report.rows)
  const maxTokens = Math.max(...stages.map((s) => s.tokens), 1)

  return (
    <>
      <Card title="Totals">
        <div className="stat-row">
          <StatPill label="Input tokens" value={report.total_input_tokens.toLocaleString()} />
          <StatPill label="Output tokens" value={report.total_output_tokens.toLocaleString()} />
          <StatPill label="Cost" value={formatCost(report.total_cost_usd)} />
          <StatPill label="Cached responses" value={report.cache_entries} />
        </div>
        <p className="muted">
          A cached response (same provider, model, stage, prompt and schema as a previous call) costs 0
          tokens on re-use.
        </p>
      </Card>

      <Card title="Tokens by stage">
        <div className="bar-chart">
          {stages.map((s) => (
            <div className="bar-row" key={s.stage}>
              <span className="bar-label">{s.stage}</span>
              <div className="bar-track">
                <div className="bar-fill" style={{ width: `${(s.tokens / maxTokens) * 100}%` }} />
              </div>
              <span className="bar-value">{s.tokens.toLocaleString()}</span>
            </div>
          ))}
        </div>
      </Card>

      <Card title="By stage / provider / model">
        <table className="simple-table">
          <thead>
            <tr>
              <th>Stage</th>
              <th>Provider</th>
              <th>Model</th>
              <th>Calls</th>
              <th>Input</th>
              <th>Cached</th>
              <th>Output</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {report.rows.map((r) => (
              <tr key={`${r.stage}-${r.provider}-${r.model}`}>
                <td>{r.stage}</td>
                <td>{r.provider}</td>
                <td>{r.model}</td>
                <td>{r.calls}</td>
                <td>{r.input_tokens.toLocaleString()}</td>
                <td>{r.cached_input_tokens.toLocaleString()}</td>
                <td>{r.output_tokens.toLocaleString()}</td>
                <td>{formatCost(r.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  )
}
