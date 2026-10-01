import { useEffect, useRef, useState } from 'react'
import { api, eventsUrl } from '../api/client'
import type { JobSnapshot, JobStarted } from '../api/types'
import { Button, Card, ErrorBanner, Spinner, StatPill } from '../components/Primitives'

export function CrawlPage() {
  const [maxPages, setMaxPages] = useState('')
  const [maxDepth, setMaxDepth] = useState('')
  const [job, setJob] = useState<JobSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const sourceRef = useRef<EventSource | null>(null)

  useEffect(() => () => sourceRef.current?.close(), []) // close the stream if the user navigates away

  async function start() {
    setError(null)
    setJob(null)
    sourceRef.current?.close()
    try {
      const body: Record<string, unknown> = {}
      if (maxPages) body.max_pages = Number(maxPages)
      if (maxDepth) body.max_depth = Number(maxDepth)
      const started = await api.post<JobStarted>('/api/crawl', body)
      const source = new EventSource(eventsUrl(started.job_id))
      sourceRef.current = source
      source.addEventListener('job', (event) => {
        const snapshot = JSON.parse((event as MessageEvent).data) as JobSnapshot
        setJob(snapshot)
        if (snapshot.status !== 'running') source.close()
      })
      source.onerror = () => {
        setError('Lost connection to the crawl progress stream.')
        source.close()
      }
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const running = job?.status === 'running'

  return (
    <Card title="Crawl">
      <ErrorBanner message={error} />
      <div className="form-row">
        <label>
          Max pages (optional)
          <input value={maxPages} onChange={(e) => setMaxPages(e.target.value)} placeholder="scope.max_pages" />
        </label>
      </div>
      <div className="form-row">
        <label>
          Max depth (optional)
          <input value={maxDepth} onChange={(e) => setMaxDepth(e.target.value)} placeholder="scope.max_depth" />
        </label>
      </div>
      <Button onClick={start} disabled={running}>
        {running ? 'Crawling…' : 'Start crawl'}
      </Button>

      {job && (
        <div className="crawl-progress">
          <div className="stat-row">
            <StatPill label="Status" value={job.status} />
            <StatPill label="Pages done" value={job.progress.pages_done} />
            <StatPill label="Frontier" value={job.progress.frontier_size} />
          </div>
          {running && (
            <p className="muted">
              <Spinner /> {job.progress.current_url || 'starting…'}
            </p>
          )}
          {job.status === 'completed' && job.result && (
            <p className="result-line">
              Finished ({job.result.stopped_reason}): {job.result.stats.pages ?? 0} page(s),{' '}
              {job.result.stats.ui_states ?? 0} UI state(s).
            </p>
          )}
          {job.status === 'failed' && <ErrorBanner message={job.error ?? 'Crawl failed.'} />}
        </div>
      )}
    </Card>
  )
}
