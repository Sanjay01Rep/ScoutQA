// Thin fetch wrapper: attaches the one-time token every /api/* route needs (printed by `scoutqa ui` in
// the URL it opens), and throws a plain Error with the backend's own message on a non-2xx response.
import type { ApiErrorBody } from './types'

const TOKEN_STORAGE_KEY = 'scoutqa.token'

export function readTokenFromUrl(): string | null {
  const url = new URL(window.location.href)
  const token = url.searchParams.get('token')
  if (token) {
    sessionStorage.setItem(TOKEN_STORAGE_KEY, token)
    url.searchParams.delete('token')
    window.history.replaceState({}, '', url.toString())
  }
  return token ?? sessionStorage.getItem(TOKEN_STORAGE_KEY)
}

export function currentToken(): string {
  return sessionStorage.getItem(TOKEN_STORAGE_KEY) ?? ''
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-ScoutQA-Token': currentToken(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const problem = (await response.json().catch(() => null)) as ApiErrorBody | null
    throw new Error(problem?.error ?? `${method} ${path} failed (${response.status})`)
  }
  return (await response.json()) as T
}

export const api = {
  get: <T>(path: string): Promise<T> => request<T>('GET', path),
  post: <T>(path: string, body?: unknown): Promise<T> => request<T>('POST', path, body ?? {}),
}

/** URL for an EventSource connection: the token has to ride along as a query param since EventSource
 * can't set custom request headers. */
export function eventsUrl(jobId: string): string {
  return `/api/jobs/${encodeURIComponent(jobId)}/events?token=${encodeURIComponent(currentToken())}`
}

/** URL for a plain `<a>`/`window.open` download: same reasoning as eventsUrl. */
export function downloadUrl(name: string): string {
  return `/api/download?name=${encodeURIComponent(name)}&token=${encodeURIComponent(currentToken())}`
}
