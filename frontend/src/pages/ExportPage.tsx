import { useState } from 'react'
import { api, downloadUrl } from '../api/client'
import type { ExportResult } from '../api/types'
import { Button, Card, ErrorBanner, StatPill } from '../components/Primitives'

const FORMATS = ['xlsx', 'csv', 'md', 'json'] as const

export function ExportPage() {
  const [fmt, setFmt] = useState<(typeof FORMATS)[number]>('xlsx')
  const [template, setTemplate] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<ExportResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function doExport() {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const body: Record<string, unknown> = { fmt }
      if (template) body.template = template
      setResult(await api.post<ExportResult>('/api/export', body))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Export">
      <ErrorBanner message={error} />
      <div className="form-row">
        <label>
          Format
          <select value={fmt} onChange={(e) => setFmt(e.target.value as (typeof FORMATS)[number])}>
            {FORMATS.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="form-row">
        <label>
          Template path (optional — overrides scoutqa.yaml's template.path)
          <input value={template} onChange={(e) => setTemplate(e.target.value)} placeholder="my-template.xlsx" />
        </label>
      </div>
      <Button onClick={doExport} disabled={busy}>
        {busy ? 'Exporting…' : 'Export'}
      </Button>

      {result && (
        <div className="result-block">
          <div className="stat-row">
            <StatPill label="Cases" value={result.cases} />
            <StatPill label="Rows" value={result.rows} />
            <StatPill label="Need review" value={result.needs_review} />
          </div>
          <p className="result-line">
            <a href={downloadUrl(result.name)} download={result.name}>
              Download {result.name}
            </a>
          </p>
          {result.custom_columns.length > 0 && (
            <p className="muted">
              Left blank: {result.custom_columns.join(', ')} (set template.columns/template.defaults in
              scoutqa.yaml, or the LLM stage can fill them)
            </p>
          )}
        </div>
      )}
    </Card>
  )
}
