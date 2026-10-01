import { useState } from 'react'
import { api } from '../api/client'
import type { SaveConfigResult, TemplatePreview } from '../api/types'
import { Button, Card, ErrorBanner } from '../components/Primitives'

export function TemplatePage() {
  const [path, setPath] = useState('')
  const [preview, setPreview] = useState<TemplatePreview | null>(null)
  const [saved, setSaved] = useState<SaveConfigResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function doPreview() {
    setBusy(true)
    setError(null)
    setPreview(null)
    setSaved(null)
    try {
      const query = path ? `?path=${encodeURIComponent(path)}` : ''
      setPreview(await api.get<TemplatePreview>(`/api/template${query}`))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function saveAsDefault() {
    setBusy(true)
    setError(null)
    try {
      setSaved(await api.post<SaveConfigResult>('/api/config', { fields: { 'template.path': path || null } }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Template">
      <ErrorBanner message={error} />
      <p className="muted">
        Preview how a template's columns map to test-case fields before exporting with it, or without a
        path to preview what's currently configured (or the built-in default).
      </p>
      <div className="form-row">
        <label>
          Template path
          <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="my-template.xlsx" />
        </label>
      </div>
      <div className="button-row">
        <Button onClick={doPreview} disabled={busy} variant="secondary">
          Preview
        </Button>
        <Button onClick={saveAsDefault} disabled={busy}>
          {path ? 'Save as default template' : 'Clear default template'}
        </Button>
      </div>

      {preview && (
        <div className="result-block">
          <p>
            <strong>{preview.template}</strong> ({preview.layout})
          </p>
          <table className="simple-table">
            <thead>
              <tr>
                <th>Column</th>
                <th>Filled from</th>
                <th>Why</th>
              </tr>
            </thead>
            <tbody>
              {preview.columns.map((c) => (
                <tr key={c.column}>
                  <td>{c.column}</td>
                  <td>{c.field}</td>
                  <td>{c.why}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {preview.custom_columns.length > 0 && (
            <p className="muted">Left blank: {preview.custom_columns.join(', ')}</p>
          )}
        </div>
      )}
      {saved && (
        <p className="result-line">
          Saved. {saved.note}
        </p>
      )}
    </Card>
  )
}
