import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { LoginResult, ProjectInfo } from '../api/types'
import { Button, Card, ErrorBanner } from '../components/Primitives'

export function LoginPage() {
  const [roles, setRoles] = useState<string[]>([])
  const [role, setRole] = useState('')
  const [force, setForce] = useState(false)
  const [manual, setManual] = useState(false)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<LoginResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<ProjectInfo>('/api/project').then((p) => setRoles(p.roles)).catch(() => undefined)
  }, [])

  async function submit() {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      const body: Record<string, unknown> = { force, manual }
      if (role) body.role = role
      setResult(await api.post<LoginResult>('/api/login', body))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Login">
      <ErrorBanner message={error} />
      <div className="form-row">
        <label>
          Role
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="">(default)</option>
            {roles.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="form-row">
        <label className="checkbox">
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
          Force (ignore saved session)
        </label>
      </div>
      <div className="form-row">
        <label className="checkbox">
          <input type="checkbox" checked={manual} onChange={(e) => setManual(e.target.checked)} />
          Manual (open a visible browser window — use this for MFA/SSO/CAPTCHA)
        </label>
      </div>
      <Button onClick={submit} disabled={busy}>
        {busy ? 'Signing in…' : 'Sign in'}
      </Button>
      {result && (
        <p className="result-line">
          Role <strong>{result.role}</strong>: <strong>{result.outcome}</strong>
          {result.session_saved && ' — session saved'}
        </p>
      )}
    </Card>
  )
}
