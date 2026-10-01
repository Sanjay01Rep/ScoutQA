import { useState } from 'react'
import { api } from '../api/client'
import type { DryRunEstimate, GenerateResult } from '../api/types'
import { Button, Card, ErrorBanner, StatPill } from '../components/Primitives'

type Mode = 'config' | 'rules-only' | 'with-llm'

export function GeneratePage() {
  const [mode, setMode] = useState<Mode>('config')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<GenerateResult | null>(null)
  const [estimate, setEstimate] = useState<DryRunEstimate | null>(null)
  const [error, setError] = useState<string | null>(null)

  function rulesOnlyFor(m: Mode): boolean | undefined {
    return m === 'config' ? undefined : m === 'rules-only'
  }

  async function dryRun() {
    setBusy(true)
    setError(null)
    setEstimate(null)
    try {
      setEstimate(await api.post<DryRunEstimate>('/api/generate', { dry_run: true }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function generate() {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      setResult(await api.post<GenerateResult>('/api/generate', { rules_only: rulesOnlyFor(mode) }))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Generate test cases">
      <ErrorBanner message={error} />
      <div className="form-row">
        <label>
          LLM stage
          <select value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
            <option value="config">Follow scoutqa.yaml (generation.use_llm)</option>
            <option value="rules-only">Rules only (0 tokens)</option>
            <option value="with-llm">Rules + LLM scenarios</option>
          </select>
        </label>
      </div>
      <div className="button-row">
        <Button onClick={dryRun} disabled={busy || mode === 'rules-only'} variant="secondary">
          Estimate cost
        </Button>
        <Button onClick={generate} disabled={busy}>
          {busy ? 'Generating…' : 'Generate'}
        </Button>
      </div>

      {estimate && (
        <div className="result-block">
          <p className="muted">LLM scenario-stage estimate — expansion cost depends on how many come back.</p>
          <div className="stat-row">
            <StatPill label="Approx. input tokens" value={estimate.approx_input_tokens.toLocaleString()} />
            <StatPill
              label="Approx. cost"
              value={estimate.approx_cost_usd !== null ? `$${estimate.approx_cost_usd.toFixed(4)}` : 'unknown'}
            />
          </div>
        </div>
      )}

      {result && (
        <div className="result-block">
          <div className="stat-row">
            <StatPill label="Total cases" value={result.total_cases} />
            <StatPill label="Rule-based" value={result.rule_cases} />
            <StatPill label="LLM" value={result.llm_cases} />
            <StatPill label="Need review" value={result.needs_review} />
          </div>
          <table className="simple-table">
            <thead>
              <tr>
                <th>Module</th>
                <th>Cases</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(result.by_module).map(([module, count]) => (
                <tr key={module}>
                  <td>{module}</td>
                  <td>{count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
