import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { GetConfigResult, SaveConfigResult } from '../api/types'
import { Button, Card, ErrorBanner } from '../components/Primitives'

export function SetupPage() {
  const [loaded, setLoaded] = useState<GetConfigResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<SaveConfigResult | null>(null)
  const [busy, setBusy] = useState(false)

  const [baseUrl, setBaseUrl] = useState('')
  const [authType, setAuthType] = useState<'none' | 'form'>('none')
  const [loginUrl, setLoginUrl] = useState('')
  const [usernameEnv, setUsernameEnv] = useState('')
  const [passwordEnv, setPasswordEnv] = useState('')
  const [maxPages, setMaxPages] = useState('')
  const [maxDepth, setMaxDepth] = useState('')
  const [templatePath, setTemplatePath] = useState('')

  function populate(result: GetConfigResult) {
    setLoaded(result)
    if (result.config) {
      setBaseUrl(result.config.base_url)
      setAuthType(result.config.auth.type === 'form' ? 'form' : 'none')
      setLoginUrl(result.config.auth.login_url ?? '')
      setUsernameEnv(result.config.auth.username_env ?? '')
      setPasswordEnv(result.config.auth.password_env ?? '')
      setMaxPages(String(result.config.scope.max_pages))
      setMaxDepth(String(result.config.scope.max_depth))
      setTemplatePath(result.config.template.path ?? '')
    }
  }

  useEffect(() => {
    api.get<GetConfigResult>('/api/config').then(populate).catch((e: Error) => setError(e.message))
  }, [])

  async function restore() {
    setBusy(true)
    setError(null)
    try {
      await api.post<SaveConfigResult>('/api/config', { fields: {} })
      populate(await api.get<GetConfigResult>('/api/config'))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function saveEdits() {
    setBusy(true)
    setError(null)
    setSaved(null)
    try {
      const fields: Record<string, unknown> = {
        base_url: baseUrl,
        'auth.type': authType,
        'scope.max_pages': Number(maxPages),
        'scope.max_depth': Number(maxDepth),
      }
      if (authType === 'form') {
        fields['auth.login_url'] = loginUrl
        fields['auth.username_env'] = usernameEnv
        fields['auth.password_env'] = passwordEnv
      }
      if (templatePath) fields['template.path'] = templatePath
      setSaved(await api.post<SaveConfigResult>('/api/config', { fields }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (!loaded) {
    return (
      <Card title="Project setup">
        <ErrorBanner message={error} />
      </Card>
    )
  }

  if (!loaded.exists) {
    return (
      <Card title="Project setup">
        <ErrorBanner message={error} />
        <p>
          <code>{loaded.path}</code> is missing, but this server is still running with the project it
          started with. Restore the file from that, then edit it below.
        </p>
        <Button onClick={restore} disabled={busy}>
          {busy ? 'Restoring…' : 'Restore scoutqa.yaml'}
        </Button>
      </Card>
    )
  }

  return (
    <Card title={`Edit project: ${loaded.config!.project}`}>
      <ErrorBanner message={error} />
      <p className="muted">
        Editing <code>{loaded.path}</code>. The project name can't be changed here — this server is
        already running with it. Advanced settings (multiple roles, custom selectors, rule packs, LLM
        profiles) aren't covered here yet — edit those by hand. Changes take effect next time you start
        `scoutqa ui`, `scoutqa crawl`, etc.
      </p>
      <div className="form-row">
        <label>
          Base URL
          <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
        </label>
      </div>
      <div className="form-row">
        <label>
          Auth type
          <select value={authType} onChange={(e) => setAuthType(e.target.value as 'none' | 'form')}>
            <option value="none">None</option>
            <option value="form">Username/password form</option>
          </select>
        </label>
      </div>
      {authType === 'form' && (
        <>
          <div className="form-row">
            <label>
              Login URL
              <input value={loginUrl} onChange={(e) => setLoginUrl(e.target.value)} />
            </label>
          </div>
          <div className="form-row">
            <label>
              Username env var name
              <input value={usernameEnv} onChange={(e) => setUsernameEnv(e.target.value)} placeholder="APP_USERNAME" />
            </label>
          </div>
          <div className="form-row">
            <label>
              Password env var name
              <input value={passwordEnv} onChange={(e) => setPasswordEnv(e.target.value)} placeholder="APP_PASSWORD" />
            </label>
          </div>
          <p className="muted">
            Only the env var <em>names</em> are stored — set the actual values yourself in the terminal
            you run <code>scoutqa login</code>/<code>scoutqa ui</code> from.
          </p>
        </>
      )}
      <div className="form-row">
        <label>
          Max pages
          <input value={maxPages} onChange={(e) => setMaxPages(e.target.value)} />
        </label>
      </div>
      <div className="form-row">
        <label>
          Max depth
          <input value={maxDepth} onChange={(e) => setMaxDepth(e.target.value)} />
        </label>
      </div>
      <div className="form-row">
        <label>
          Template path (optional)
          <input value={templatePath} onChange={(e) => setTemplatePath(e.target.value)} placeholder="my-template.xlsx" />
        </label>
      </div>
      <Button onClick={saveEdits} disabled={busy}>
        {busy ? 'Saving…' : 'Save'}
      </Button>
      {saved && <p className="result-line">Saved. {saved.note}</p>}
    </Card>
  )
}
