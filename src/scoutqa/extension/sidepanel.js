// @ts-check
import { api, download, originPatterns, settings } from './api.js';

const $ = (id) => /** @type {HTMLElement} */ (document.getElementById(id));
const input = (id) => /** @type {HTMLInputElement} */ (document.getElementById(id));
const select = (id) => /** @type {HTMLSelectElement} */ (document.getElementById(id));

function say(text, error = false) {
  const el = $('message');
  el.textContent = text;
  el.className = error ? 'error' : '';
}

async function guarded(fn) {
  try { await fn(); } catch (e) { say(String(e.message || e), true); }
}

function setConnected(on, label) {
  const pill = $('conn');
  pill.textContent = label;
  pill.className = `pill ${on}`;
}

async function refreshStatus() {
  const status = await chrome.runtime.sendMessage({ type: 'panel:status' });
  const rec = status && status.recording;
  $('start').hidden = !!rec;
  $('stop').hidden = !rec;
  select('role').disabled = !!rec;
  if (rec) setConnected('rec', `recording as ${rec.role}`);
  const s = (status && status.stats) || {};
  $('s-captures').textContent = String(s.captures || 0);
  $('s-states').textContent = String(s.states || 0);
  $('s-api').textContent = String(s.apiCalls || 0);
  $('s-shapes').textContent = String(s.shapes || 0);
  if (s.lastError) say(s.lastError, true);

  const c = status && status.crawl;
  const running = !!(c && c.running);
  $('crawl-start').hidden = running;
  $('crawl-stop').hidden = !running;
  $('start').disabled = running;
  $('crawl-start').toggleAttribute('disabled', !!rec);
  if (running) {
    setConnected('rec', c.stopping ? 'stopping crawl…' : 'crawling');
    for (const [id, key] of [['c-pages', 'pages'], ['c-pending', 'pending'], ['c-clicks', 'clicks'],
                             ['c-blocked', 'blocked'], ['c-dialogs', 'dialogs']]) {
      $(id).textContent = String(c[key] || 0);
    }
  } else if (c && c.last) {
    const last = c.last;
    $('crawl-info').textContent = last.error ? `Crawl failed: ${last.error}`
      : `Crawl finished (${last.stopped_reason}): ${last.pages} pages, ${last.clicks} controls clicked.`;
    $('crawl-info').dataset.done = last.stopped_reason || 'error';
    if (!rec) setConnected('on', 'connected');
  }
}

async function refreshGaps() {
  const role = select('role').value;
  const data = await api(`/api/gaps?role=${encodeURIComponent(role)}`);
  const list = $('gaps');
  list.replaceChildren(...data.gaps.slice(0, 15).map((g) => {
    const li = document.createElement('li');
    li.textContent = `${g.label || '(link)'} — ${g.url}`;
    return li;
  }));
  $('gaps-section').hidden = data.gaps.length === 0;
}

/** @type {string[]} match patterns of the project's domains, loaded before any click */
let projectOrigins = [];

async function loadProject() {
  const project = await api('/api/project');
  projectOrigins = originPatterns(project.allowed_domains, project.base_url);
  $('project-name').textContent = project.project;
  $('project-url').textContent = project.base_url;
  const role = select('role');
  role.replaceChildren(...project.roles.map((r) => Object.assign(document.createElement('option'), { value: r, textContent: r })));
  $('project-section').hidden = false;
  $('crawl-section').hidden = false;
  $('cases-section').hidden = false;
  setConnected('on', 'connected');
  return project;
}

async function init() {
  const s = await settings();
  input('port').value = String(s.port);
  if (!s.token) return;
  await guarded(async () => {
    await loadProject();
    await refreshStatus();
    await refreshGaps();
    $('pair-section').hidden = true;
  });
}

$('pair').addEventListener('click', () => guarded(async () => {
  const port = Number(input('port').value) || 8765;
  await chrome.storage.local.set({ port, token: null });
  const res = await api('/api/pair', { code: input('code').value });
  await chrome.storage.local.set({ token: res.token });
  input('code').value = '';
  $('pair-section').hidden = true;
  await loadProject();
  await refreshStatus();
  say(`Paired with project ${res.project}.`);
}));

$('start').addEventListener('click', () => guarded(async () => {
  // Host access is requested only for the project's own domains, directly in the click handler:
  // chrome.permissions.request must run inside the user gesture (no await before it).
  const granted = await chrome.permissions.request({ origins: projectOrigins });
  if (!granted) throw new Error('Permission for the app\'s domain is needed to record.');
  const res = await chrome.runtime.sendMessage({ type: 'panel:start', role: select('role').value });
  if (res && res.error) throw new Error(res.error);
  say('Recording. Reload or open a page of the app to start capturing.');
  await refreshStatus();
}));

$('stop').addEventListener('click', () => guarded(async () => {
  const res = await chrome.runtime.sendMessage({ type: 'panel:stop' });
  if (res && res.error) throw new Error(res.error);
  const d = res.deltas || {};
  say(`Stopped. States: ${d.new || 0} new, ${d.changed || 0} changed, ${d.unchanged || 0} unchanged.`);
  setConnected('on', 'connected');
  await refreshStatus();
  await refreshGaps();
}));

$('crawl-start').addEventListener('click', () => guarded(async () => {
  const granted = await chrome.permissions.request({ origins: projectOrigins });  // inside the user gesture
  if (!granted) throw new Error('Permission for the app\'s domain is needed to crawl.');
  $('crawl-info').textContent = '';
  delete $('crawl-info').dataset.done;
  const res = await chrome.runtime.sendMessage({ type: 'panel:crawl-start', role: select('role').value });
  if (res && res.error) throw new Error(res.error);
  say('Crawling in a separate window (read-only).');
  await refreshStatus();
}));

$('crawl-stop').addEventListener('click', () => guarded(async () => {
  await chrome.runtime.sendMessage({ type: 'panel:crawl-stop' });
  say('Stopping the crawl…');
}));

$('generate').addEventListener('click', () => guarded(async () => {
  say('Generating…');
  const res = await api('/api/generate', {});
  $('cases-info').textContent = `${res.cases} test cases · ${res.needs_review} need review`;
  say('Done — 0 AI tokens used.');
}));

$('export').addEventListener('click', () => guarded(async () => {
  const res = await api('/api/export', { format: select('format').value });
  const blob = await download(res.name);
  const link = Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: res.name });
  link.click();
  URL.revokeObjectURL(link.href);
  say(`Exported ${res.cases} cases to ${res.name}.`);
}));

setInterval(() => { refreshStatus().catch(() => {}); }, 2000);
init();
