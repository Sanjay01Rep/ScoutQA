import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { ReviewApplyResult, ReviewList } from '../api/types'
import { Button, Card, ErrorBanner } from '../components/Primitives'

export function ReviewPage() {
  const [list, setList] = useState<ReviewList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api.get<ReviewList>('/api/review').then(setList).catch((e: Error) => setError(e.message))
  }, [])

  useEffect(refresh, [refresh])

  async function decide(id: string, field: 'approve' | 'reject') {
    setBusyId(id)
    setError(null)
    try {
      const response = await api.post<ReviewApplyResult>('/api/review', { [field]: [id] })
      const action = response.actions[0]
      if ('error' in action) throw new Error(action.error)
      refresh()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusyId(null)
    }
  }

  return (
    <Card title="Review">
      <ErrorBanner message={error} />
      {!list || list.pending.length === 0 ? (
        <p className="muted">Nothing awaiting review — generate some cases first.</p>
      ) : (
        <table className="simple-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Module</th>
              <th>Priority</th>
              <th>Title</th>
              <th>Flag</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {list.pending.map((c) => (
              <tr key={c.id}>
                <td>{c.id}</td>
                <td>{c.module}</td>
                <td>{c.priority}</td>
                <td>{c.title}</td>
                <td>{c.needs_review ? 'needs review' : ''}</td>
                <td className="row-actions">
                  <Button onClick={() => decide(c.id, 'approve')} disabled={busyId === c.id} variant="secondary">
                    Approve
                  </Button>
                  <Button onClick={() => decide(c.id, 'reject')} disabled={busyId === c.id} variant="danger">
                    Reject
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {list && list.pending_count > list.pending.length && (
        <p className="muted">
          Showing {list.pending.length} of {list.pending_count} — refine from the CLI (`scoutqa review`) to see more.
        </p>
      )}
    </Card>
  )
}
