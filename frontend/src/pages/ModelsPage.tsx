import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { ConfigureModelResult, ModelsList, SaveConfigResult, TestModelResult } from '../api/types'
import { Button, Card, ErrorBanner, StatPill } from '../components/Primitives'

const PROVIDERS = ['anthropic', 'openai', 'azure_openai', 'gemini', 'fake'] as const

export function ModelsPage() {
  const [models, setModels] = useState<ModelsList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [provider, setProvider] = useState<(typeof PROVIDERS)[number]>('anthropic')
  const [model, setModel] = useState('')
  const [profileName, setProfileName] = useState('')
  const [apiKeyEnv, setApiKeyEnv] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [azureEndpoint, setAzureEndpoint] = useState('')
  const [asDefault, setAsDefault] = useState(true)

  const [validated, setValidated] = useState<ConfigureModelResult | null>(null)
  const [saved, setSaved] = useState<SaveConfigResult | null>(null)
  const [testResult, setTestResult] = useState<TestModelResult | null>(null)

  function refresh() {
    api.get<ModelsList>('/api/models').then(setModels).catch((e: Error) => setError(e.message))
  }

  useEffect(refresh, [])

  async function validate() {
    setBusy(true)
    setError(null)
    setValidated(null)
    setSaved(null)
    try {
      setValidated(
        await api.post<ConfigureModelResult>('/api/models/configure', {
          provider,
          model,
          profile_name: profileName || 'default',
          api_key_env: apiKeyEnv || undefined,
          base_url: provider === 'openai' && baseUrl ? baseUrl : undefined,
          azure_endpoint: provider === 'azure_openai' ? azureEndpoint : undefined,
        }),
      )
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    if (!validated) return
    setBusy(true)
    setError(null)
    try {
      const fields: Record<string, unknown> = {
        [`llm.profiles.${validated.profile_name}`]: {
          provider: validated.provider,
          model: validated.model,
          ...(apiKeyEnv ? { api_key_env: apiKeyEnv } : {}),
          ...(provider === 'openai' && baseUrl ? { base_url: baseUrl } : {}),
          ...(provider === 'azure_openai' ? { azure_endpoint: azureEndpoint } : {}),
        },
      }
      if (asDefault) fields['llm.default_profile'] = validated.profile_name
      setSaved(await api.post<SaveConfigResult>('/api/config', { fields }))
      refresh()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function testDefault() {
    setBusy(true)
    setError(null)
    setTestResult(null)
    try {
      setTestResult(await api.post<TestModelResult>('/api/models/test', {}))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Card title="Configured profiles">
        <ErrorBanner message={error} />
        {models && Object.keys(models.profiles).length > 0 ? (
          <>
            <table className="simple-table">
              <thead>
                <tr>
                  <th>Profile</th>
                  <th>Provider</th>
                  <th>Model</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(models.profiles).map(([name, p]) => (
                  <tr key={name}>
                    <td>
                      {name}
                      {name === models.default_profile && ' (default)'}
                    </td>
                    <td>{p.provider}</td>
                    <td>{p.model}</td>
                    <td></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="button-row button-row-spaced">
              <Button onClick={testDefault} disabled={busy} variant="secondary">
                Test default profile
              </Button>
            </div>
            {testResult && (
              <p className="result-line">
                <strong>{testResult.provider}/{testResult.model}</strong>: {testResult.reply}
                {testResult.cached && ' (cached)'}
              </p>
            )}
          </>
        ) : (
          <p className="muted">No model profiles configured yet — none of the LLM-based features will work.</p>
        )}
      </Card>

      <Card title="Add a profile">
        <p className="muted">Validates first (checks the shape and that the API-key env var exists, never its value).</p>
        <div className="form-row">
          <label>
            Provider
            <select value={provider} onChange={(e) => setProvider(e.target.value as (typeof PROVIDERS)[number])}>
              {PROVIDERS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="form-row">
          <label>
            Model
            <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="claude-sonnet-5" />
          </label>
        </div>
        <div className="form-row">
          <label>
            Profile name
            <input value={profileName} onChange={(e) => setProfileName(e.target.value)} placeholder="default" />
          </label>
        </div>
        {provider !== 'fake' && (
          <div className="form-row">
            <label>
              API key env var name
              <input value={apiKeyEnv} onChange={(e) => setApiKeyEnv(e.target.value)} placeholder="ANTHROPIC_API_KEY" />
            </label>
          </div>
        )}
        {provider === 'openai' && (
          <div className="form-row">
            <label>
              Base URL (optional — any OpenAI-compatible endpoint, e.g. Ollama)
              <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://localhost:11434/v1" />
            </label>
          </div>
        )}
        {provider === 'azure_openai' && (
          <div className="form-row">
            <label>
              Azure endpoint
              <input value={azureEndpoint} onChange={(e) => setAzureEndpoint(e.target.value)} />
            </label>
          </div>
        )}
        <div className="form-row">
          <label className="checkbox">
            <input type="checkbox" checked={asDefault} onChange={(e) => setAsDefault(e.target.checked)} />
            Make this the default profile
          </label>
        </div>
        <div className="button-row">
          <Button onClick={validate} disabled={busy || !model} variant="secondary">
            Validate
          </Button>
          <Button onClick={save} disabled={busy || !validated}>
            Save to scoutqa.yaml
          </Button>
        </div>

        {validated && (
          <div className="result-block">
            <div className="stat-row">
              <StatPill label="Valid" value={validated.valid ? 'yes' : 'no'} />
              {validated.env_var_set !== null && (
                <StatPill label="Env var set" value={validated.env_var_set ? 'yes' : 'no'} />
              )}
            </div>
            <pre className="yaml-snippet">{validated.yaml_snippet}</pre>
          </div>
        )}
        {saved && (
          <p className="result-line">
            Saved to {saved.path}. {saved.note}
          </p>
        )}
      </Card>
    </>
  )
}
