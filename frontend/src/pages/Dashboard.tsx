import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { MapSummary, ProjectInfo, ReviewList, UsageReport } from '../api/types'
import { Card, ErrorBanner, StatPill } from '../components/Primitives'

export function Dashboard() {
  const [project, setProject] = useState<ProjectInfo | null>(null)
  const [map, setMap] = useState<MapSummary | null>(null)
  const [review, setReview] = useState<ReviewList | null>(null)
  const [usage, setUsage] = useState<UsageReport | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.get<ProjectInfo>('/api/project').then(setProject).catch((e: Error) => setError(e.message))
    api.get<MapSummary>('/api/map').then(setMap).catch(() => setMap(null)) // no crawl yet is fine here
    api.get<ReviewList>('/api/review').then(setReview).catch(() => setReview(null))
    api.get<UsageReport>('/api/usage').then(setUsage).catch(() => setUsage(null))
  }, [])

  const totalStates = map ? Object.values(map.modules).reduce((a, b) => a + b, 0) : 0

  return (
    <div>
      <ErrorBanner message={error} />
      <Card title={project ? project.project : 'Project'}>
        {project && (
          <>
            <p className="muted">{project.base_url}</p>
            <div className="stat-row">
              <StatPill label="Auth" value={project.auth_type} />
              <StatPill label="Roles" value={project.roles.join(', ') || '—'} />
            </div>
          </>
        )}
      </Card>
      <Card title="App model">
        {map ? (
          <div className="stat-row">
            <StatPill label="States" value={map.states} />
            <StatPill label="Pages (template groups)" value={totalStates} />
            <StatPill label="Modules" value={Object.keys(map.modules).length} />
          </div>
        ) : (
          <p className="muted">No crawl yet — run one from the Crawl tab.</p>
        )}
      </Card>
      <Card title="Review">
        {review ? (
          <p>
            <strong>{review.pending_count}</strong> case(s) awaiting review.
          </p>
        ) : (
          <p className="muted">No cases generated yet.</p>
        )}
      </Card>
      <Card title="Usage">
        {usage && usage.rows.length > 0 ? (
          <div className="stat-row">
            <StatPill label="Input tokens" value={usage.total_input_tokens.toLocaleString()} />
            <StatPill label="Output tokens" value={usage.total_output_tokens.toLocaleString()} />
            <StatPill
              label="Cost"
              value={usage.total_cost_usd !== null ? `$${usage.total_cost_usd.toFixed(4)}` : '—'}
            />
            <StatPill label="Cached responses" value={usage.cache_entries} />
          </div>
        ) : (
          <p className="muted">No LLM calls yet.</p>
        )}
      </Card>
    </div>
  )
}
