// @ts-check
// Background service worker: owns the recording state, registers the page scripts only for the project's
// domains while recording, and forwards captures to the local service.
import { api, originPatterns } from './api.js';

const SCRIPT_IDS = ['scoutqa-observer', 'scoutqa-guard', 'scoutqa-content'];
const CLICK_WINDOW_MS = 5000;
const PAGE_TIMEOUT_MS = 25000;
const BLOCK_RULE_ID = 1;
const ALLOW_RULE_BASE = 100;
// Every resource type, including main_frame: a POST form submission is a top-level navigation.
const ALL_TYPES = ['main_frame', 'sub_frame', 'stylesheet', 'script', 'image', 'font', 'object', 'xmlhttprequest',
  'ping', 'csp_report', 'media', 'websocket', 'other'];

/** tabId -> last click in that tab (lost if the worker restarts; that only drops a transition label) */
const lastClick = new Map();
const stats = { captures: 0, states: 0, apiCalls: 0, shapes: 0, ignored: 0, lastError: '' };

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});

async function recording() {
  const { recording } = await chrome.storage.session.get({ recording: null });
  return recording;
}

async function registerScripts(patterns) {
  await unregisterScripts();
  await chrome.scripting.registerContentScripts([
    { id: SCRIPT_IDS[0], js: ['page_observer.js'], matches: patterns, runAt: 'document_start', world: 'MAIN',
      persistAcrossSessions: false },
    { id: SCRIPT_IDS[1], js: ['crawl_guard.js'], matches: patterns, runAt: 'document_start', world: 'MAIN',
      persistAcrossSessions: false },
    { id: SCRIPT_IDS[2], js: ['extractor.js', 'content.js'], matches: patterns, runAt: 'document_idle',
      persistAcrossSessions: false },
  ]);
}

async function unregisterScripts() {
  const existing = await chrome.scripting.getRegisteredContentScripts({ ids: SCRIPT_IDS });
  if (existing.length) await chrome.scripting.unregisterContentScripts({ ids: existing.map((s) => s.id) });
}

async function start(role) {
  const project = await api('/api/project');
  const patterns = originPatterns(project.allowed_domains, project.base_url);
  const run = await api('/api/record/start', { role });
  await chrome.storage.session.set({ recording: { run_id: run.run_id, role: run.role, patterns, project: project.project } });
  Object.assign(stats, { captures: 0, states: 0, apiCalls: 0, shapes: 0, ignored: 0, lastError: '' });
  await registerScripts(patterns);
  // pages already open: the next navigation or reload starts capturing (the observer must load first)
  return { ...run, patterns };
}

async function stop() {
  const rec = await recording();
  await unregisterScripts();
  await chrome.storage.session.set({ recording: null });
  if (!rec) return { stopped: false };
  return api('/api/record/stop', { run_id: rec.run_id });
}

function recentClick(tabId, windowMs = CLICK_WINDOW_MS) {
  const entry = lastClick.get(tabId);
  return entry && Date.now() - entry.at < windowMs ? entry : null;
}

/**
 * The click that explains a capture: for a click capture, the click on that same page; for a page load,
 * a click on a *different* page (the navigation it caused). A late capture of the page the click happened
 * on must not consume it.
 */
function clickFor(tabId, msg) {
  const entry = recentClick(tabId);
  if (!entry) return null;
  const samePage = entry.pageUrl === msg.url;
  if (msg.kind === 'click' ? !samePage : samePage) return null;
  if (msg.kind !== 'click') lastClick.delete(tabId);
  return entry.click;
}

async function capture(msg, sender) {
  const rec = await recording();
  if (!rec || !sender.tab) return { recording: false };
  const click = clickFor(sender.tab.id, msg);
  try {
    const res = await api('/api/record/capture', {
      run_id: rec.run_id, url: msg.url, main: msg.snapshot, trigger: { kind: msg.kind, click },
    });
    if (res.ignored) stats.ignored += 1;
    else { stats.captures += 1; if (res.status === 'new') stats.states += 1; }
    return res;
  } catch (e) {
    stats.lastError = String(e.message || e);
    return { error: stats.lastError };
  }
}

async function events(msg, sender) {
  const rec = await recording();
  if (!rec || !sender.tab) return { recording: false };
  const entry = recentClick(sender.tab.id, 3000);
  try {
    const res = await api('/api/record/events', {
      run_id: rec.run_id, state_id: msg.state_id, page_url: msg.page_url, api_calls: msg.api_calls || [],
      shapes: msg.shapes || [], trigger_label: entry ? entry.click.name : null,
    });
    stats.apiCalls += res.api_calls || 0;
    stats.shapes += res.shapes || 0;
    return res;
  } catch (e) {
    stats.lastError = String(e.message || e);
    return { error: stats.lastError };
  }
}

// ------------------------------------------------------------------ Crawl mode
// The service decides what to visit and which controls are safe to click; this loop only drives the tab.

/** @type {null | {run_id: string, tabId: number, windowId: number, stopping: boolean, pages: number,
 *   pending: number, clicks: number, blocked: number, dialogs: number, done: boolean, reason: string}} */
let crawl = null;
let lastCrawl = null;
/** the page capture the loop is waiting for */
let waiting = null;

/** glob ('*' wildcards) -> declarativeNetRequest urlFilter */
const toUrlFilter = (glob) => glob.replace(/\?/g, '*');

async function addReadOnlyRules(tabId, start) {
  const existing = await chrome.declarativeNetRequest.getSessionRules();
  const rules = [];
  if (start.read_only) {
    rules.push({ id: BLOCK_RULE_ID, priority: 1, action: { type: 'block' },
                 condition: { tabIds: [tabId], requestMethods: start.block_methods, resourceTypes: ALL_TYPES } });
    (start.allow_patterns || []).forEach((glob, i) => rules.push({
      id: ALLOW_RULE_BASE + i, priority: 2, action: { type: 'allow' },
      condition: { tabIds: [tabId], urlFilter: toUrlFilter(glob), requestMethods: start.block_methods,
                   resourceTypes: ALL_TYPES } }));
  }
  await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: existing.map((r) => r.id), addRules: rules });
}

async function removeReadOnlyRules() {
  const existing = await chrome.declarativeNetRequest.getSessionRules();
  await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: existing.map((r) => r.id) });
}

function waitForPage(phase, url, ref) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => { waiting = null; resolve({ timeout: true }); }, PAGE_TIMEOUT_MS);
    waiting = { phase, url, ref, resolve: (res) => { clearTimeout(timer); waiting = null; resolve(res); } };
  });
}

async function visit(url, phase) {
  const tab = await chrome.tabs.get(crawl.tabId);
  const promise = waitForPage(phase, url);
  const sameDocument = tab.url && tab.url.split('#')[0] === url.split('#')[0] && tab.url !== url;
  if (phase === 'restore' || tab.url === url) await chrome.tabs.reload(crawl.tabId);
  else await chrome.tabs.update(crawl.tabId, { url });
  if (sameDocument) {  // hash routes do not load a new document: ask the page to capture itself
    setTimeout(() => chrome.tabs.sendMessage(crawl.tabId, { type: 'crawl:capture' }).catch(() => {}), 500);
  }
  return promise;
}

async function clickAction(url, action) {
  await api('/api/crawl/clicking', { run_id: crawl.run_id, url, ref: action.ref });
  const promise = waitForPage('click', url, action.ref);
  const res = await chrome.tabs.sendMessage(crawl.tabId, { type: 'crawl:click', ref: action.ref }).catch(() => null);
  if (!res || !res.clicked) { if (waiting) waiting.resolve({ missing: true }); }
  return promise;
}

async function crawlLoop() {
  const run = crawl.run_id;
  try {
    while (crawl && !crawl.stopping) {
      const next = await api('/api/crawl/next', { run_id: run });
      if (next.done) { crawl.done = true; crawl.reason = next.reason; break; }
      crawl.pending = next.pending;
      const res = await visit(next.url, 'load');
      if (res.timeout) {
        await api('/api/crawl/error', { run_id: run, url: next.url, message: 'page did not load or capture in time' });
        continue;
      }
      if (res.session_lost) { crawl.reason = 'session_lost'; break; }
      if (res.state_id) crawl.pages += 1;
      for (const action of res.actions || []) {
        if (!crawl || crawl.stopping) break;
        const restored = await visit(next.url, 'restore');
        if (restored.timeout) break;
        await clickAction(next.url, action);
        crawl.clicks += 1;
      }
    }
  } catch (e) {
    stats.lastError = String(e.message || e);
  } finally {
    await finishCrawl();
  }
}

async function crawlStart(role) {
  if (crawl) throw new Error('A crawl is already running.');
  if (await recording()) throw new Error('Stop recording before crawling.');
  const project = await api('/api/project');
  const patterns = originPatterns(project.allowed_domains, project.base_url);
  const start = await api('/api/crawl/start', { role });
  // A separate, unfocused window: the user keeps working; the crawl tab is the only one with read-only rules.
  const win = await chrome.windows.create({ url: 'about:blank', focused: false, width: 1280, height: 900 });
  const tabId = win.tabs[0].id;
  await addReadOnlyRules(tabId, start);
  await registerScripts(patterns);
  crawl = { run_id: start.run_id, tabId, windowId: win.id, stopping: false, pages: 0, pending: 0, clicks: 0,
            blocked: 0, dialogs: 0, done: false, reason: '' };
  lastCrawl = null;
  await chrome.storage.session.set({ crawl: { run_id: start.run_id, tabId, windowId: win.id } });
  // Warm-up load: the content script marks this tab so the dialog guard is active from the first real page.
  await visit(project.base_url, 'warmup');
  crawlLoop();
  return { run_id: start.run_id, role: start.role };
}

async function finishCrawl() {
  if (!crawl) return;
  const current = crawl;
  crawl = null;
  if (waiting) waiting.resolve({ cancelled: true });
  await removeReadOnlyRules().catch(() => {});
  if (!(await recording())) await unregisterScripts().catch(() => {});
  await chrome.windows.remove(current.windowId).catch(() => {});
  await chrome.storage.session.set({ crawl: null });
  try {
    const summary = await api('/api/crawl/stop', { run_id: current.run_id, cancelled: current.stopping });
    lastCrawl = { ...summary, pages: current.pages, clicks: current.clicks };
  } catch (e) {
    lastCrawl = { error: String(e.message || e) };
  }
}

async function crawlStop() {
  if (!crawl) return { stopped: false, last: lastCrawl };
  crawl.stopping = true;
  if (waiting) waiting.resolve({ cancelled: true });
  return { stopping: true };
}

async function crawlPage(msg, sender) {
  if (!crawl || !sender.tab || sender.tab.id !== crawl.tabId || !waiting) return { ignored: true };
  const w = waiting;
  if (w.phase === 'warmup') { w.resolve({ ok: true }); return { ok: true }; }
  const kind = w.phase === 'restore' ? 'restore' : msg.kind === 'click' || w.phase === 'click' ? 'click' : 'load';
  try {
    const res = await api('/api/crawl/page', { run_id: crawl.run_id, requested_url: w.url, url: msg.url,
                                               main: msg.snapshot, kind, ref: w.ref || msg.ref || null });
    w.resolve(res);
    return res;
  } catch (e) {
    stats.lastError = String(e.message || e);
    w.resolve({ error: stats.lastError });
    return { error: stats.lastError };
  }
}

async function crawlEvents(msg, sender) {
  if (!crawl || !sender.tab || sender.tab.id !== crawl.tabId) return { ignored: true };
  const res = await api('/api/crawl/events', { run_id: crawl.run_id, page_url: msg.page_url,
                                               api_calls: msg.api_calls || [], dialogs: msg.dialogs || [] });
  crawl.blocked += res.blocked || 0;
  crawl.dialogs += res.dialogs || 0;
  return res;
}

function crawlStatus() {
  if (!crawl) return lastCrawl ? { running: false, last: lastCrawl } : null;
  const { run_id, pages, pending, clicks, blocked, dialogs, stopping } = crawl;
  return { running: true, run_id, pages, pending, clicks, blocked, dialogs, stopping };
}

async function handle(msg, sender) {
  switch (msg.type) {
    case 'panel:start': return start(msg.role);
    case 'panel:stop': return stop();
    case 'panel:crawl-start': return crawlStart(msg.role);
    case 'panel:crawl-stop': return crawlStop();
    case 'panel:status': return { recording: await recording(), stats, crawl: crawlStatus() };
    case 'content:hello':
      if (crawl && sender.tab && sender.tab.id === crawl.tabId) return { crawl: true };
      return { recording: !!(await recording()) };
    case 'content:crawl-page': return crawlPage(msg, sender);
    case 'content:crawl-events': return crawlEvents(msg, sender);
    case 'content:click':
      if (sender.tab) lastClick.set(sender.tab.id, { click: msg.click, pageUrl: msg.page_url, at: Date.now() });
      return {};
    case 'content:capture': return capture(msg, sender);
    case 'content:events': return events(msg, sender);
    default: return { error: `unknown message ${msg.type}` };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handle(msg, sender).then(sendResponse, (e) => sendResponse({ error: String(e.message || e) }));
  return true; // async response
});
