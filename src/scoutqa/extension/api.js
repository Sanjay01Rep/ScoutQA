// @ts-check
// Talks to the local ScoutQA service (127.0.0.1 only). Shared by the background worker and the side panel.

export const DEFAULT_PORT = 8765;

/** @returns {Promise<{port: number, token: string | null}>} */
export async function settings() {
  const s = await chrome.storage.local.get({ port: DEFAULT_PORT, token: null });
  return { port: Number(s.port) || DEFAULT_PORT, token: s.token };
}

/**
 * @param {string} path
 * @param {object | undefined} [body]
 * @param {string} [method]
 */
export async function api(path, body, method) {
  const { port, token } = await settings();
  const res = await fetch(`http://127.0.0.1:${port}${path}`, {
    method: method || (body ? 'POST' : 'GET'),
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `ScoutQA service answered HTTP ${res.status}`);
  return data;
}

/** Download an export produced by the service; returns a Blob. */
export async function download(name) {
  const { port, token } = await settings();
  const res = await fetch(`http://127.0.0.1:${port}/api/download?name=${encodeURIComponent(name)}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(`download failed (HTTP ${res.status})`);
  return res.blob();
}

/**
 * Chrome match patterns for the project's allowed domains ('*.example.com' supported), limited to the
 * scheme of the app's base URL so no broader access than needed is requested.
 */
export function originPatterns(allowedDomains, baseUrl) {
  const scheme = baseUrl && baseUrl.startsWith('http://') ? 'http' : 'https';
  return allowedDomains.map((d) => `${scheme}://${d}/*`);
}
