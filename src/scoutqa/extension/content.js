// ScoutQA content script (isolated world) — runs only on the project's domains while recording or crawling.
//
// Record mode: captures each page with the shared extractor after it settles, remembers what the user
//   clicked, and records the FORMAT of typed values ('Aa-9999'), never the values. Password, hidden,
//   one-time-code, payment and username fields are ignored entirely.
// Crawl mode (only in the extension's own crawl tab): captures each settled page for the service and clicks
//   only the controls the service chose. Every other tab of the app is left alone.
(() => {
  if (globalThis.__scoutqaContent) return;
  globalThis.__scoutqaContent = true;

  const QUIET_MS = 400;
  const MAX_WAIT_MS = 5000;
  const SCROLL_STEPS = 5;
  const CRAWL_FLAG = 'scoutqa-crawl-tab';
  const SKIP_TYPES = new Set(['password', 'hidden', 'file', 'checkbox', 'radio', 'submit', 'button', 'reset', 'image']);
  const SKIP_AUTOCOMPLETE = /one-time-code|cc-|current-password|new-password|username/i;
  const CLICKABLE = 'a[href], button, summary, [role="button"], [role="tab"], [role="menuitem"], input[type="submit"], input[type="button"]';

  const send = (msg) => chrome.runtime.sendMessage(msg).catch(() => ({}));
  let mode = null; // 'record' | 'crawl' | null
  let stateId = null;
  let lastUrl = location.href;
  let inflight = 0;
  let capturing = false;
  let queued = null;
  let lastClickAt = 0;
  const apiQueue = [];
  const dialogQueue = [];
  const shapes = new Map();

  // ---------------------------------------------------------------- helpers
  function shapeOf(value) {
    return value
      .replace(/\p{Lu}/gu, 'A').replace(/\p{Ll}/gu, 'a').replace(/\p{Nd}/gu, '9')
      .replace(/(?![Aa])\p{L}/gu, 'a')  // letters without case (e.g. CJK); keep the A/a placeholders
      .replace(/[^\x20-\x7e\t]/g, '*')
      .slice(0, 40);
  }

  /** Resolves once the DOM and the page's API calls are quiet; true if anything changed meanwhile. */
  function quiet() {
    return new Promise((resolve) => {
      const started = Date.now();
      let timer = null;
      let changed = false;
      const done = () => { observer.disconnect(); clearTimeout(timer); resolve(changed); };
      const check = () => {
        if (inflight > 0 && Date.now() - started < MAX_WAIT_MS) { timer = setTimeout(check, QUIET_MS); return; }
        done();
      };
      const observer = new MutationObserver(() => {
        changed = true;
        clearTimeout(timer);
        timer = setTimeout(check, QUIET_MS);
      });
      observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
      timer = setTimeout(check, QUIET_MS);
      setTimeout(done, MAX_WAIT_MS);
    });
  }

  /** Scroll down in steps so lazy-loaded sections render, then back to the top. */
  async function lazyScroll() {
    const root = document.documentElement;
    if (!root || root.scrollHeight <= window.innerHeight) return;
    let height = root.scrollHeight;
    for (let i = 0; i < SCROLL_STEPS; i++) {
      window.scrollTo(0, root.scrollHeight);
      await quiet();
      if (root.scrollHeight <= height) break;
      height = root.scrollHeight;
    }
    window.scrollTo(0, 0);
  }

  async function flush() {
    if (!mode || (!apiQueue.length && !shapes.size && !dialogQueue.length)) return;
    const calls = apiQueue.splice(0);
    const dialogs = dialogQueue.splice(0);
    const shaped = [...shapes.values()];
    shapes.clear();
    await send({ type: mode === 'crawl' ? 'content:crawl-events' : 'content:events', state_id: stateId,
                 page_url: location.href, api_calls: calls, shapes: shaped, dialogs });
  }

  // ---------------------------------------------------------------- record mode
  async function upload(kind) {
    const snapshot = globalThis.__scoutqaExtract();
    const res = await send({ type: 'content:capture', url: location.href, kind, snapshot });
    if (res && res.state_id) stateId = res.state_id;
    lastUrl = location.href;
  }

  async function capture(kind) {
    if (mode !== 'record') return;
    if (capturing) { queued = queued === 'click' || !queued ? kind : queued; return; }
    capturing = true;
    const began = Date.now();
    try {
      await flush();  // observations belong to the state the user was on
      // A new page is captured at once (people may leave quickly), then again if it changes while settling
      // (async content). After a click, only the settled page matters. If the user clicked while a page was
      // settling, that change belongs to the (queued) click capture, not to the page itself.
      if (kind !== 'click') await upload(kind);
      const changed = await quiet();
      if (kind === 'click' || (changed && lastClickAt < began)) await upload(kind);
    } catch (e) {
      // the page navigated away mid-capture; the next page captures itself
    } finally {
      capturing = false;
      if (queued) { const next = queued; queued = null; capture(next); }
    }
  }

  document.addEventListener('click', (event) => {
    if (mode !== 'record') return;
    const el = event.target instanceof Element ? event.target.closest(CLICKABLE) : null;
    if (!el) return;
    lastClickAt = Date.now();
    const ref = el.getAttribute('data-scoutqa-ref');
    const name = (el.getAttribute('aria-label') || el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    send({ type: 'content:click', page_url: location.href, click: { from_state_id: stateId, ref, name } });
    const before = location.href;
    setTimeout(() => { if (location.href === before) capture('click'); }, 50);
  }, true);

  document.addEventListener('focusout', (event) => {
    if (mode !== 'record') return;
    const el = event.target;
    if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) return;
    if (el instanceof HTMLInputElement && SKIP_TYPES.has((el.type || 'text').toLowerCase())) return;
    if (SKIP_AUTOCOMPLETE.test(el.autocomplete || '')) return;
    const ref = el.getAttribute('data-scoutqa-ref');
    if (!ref || !el.value) return;
    shapes.set(ref, { ref, shape: shapeOf(el.value), length: el.value.length });
  }, true);

  // ---------------------------------------------------------------- crawl mode
  async function crawlCapture(kind, ref) {
    await quiet();
    if (kind === 'load') { await lazyScroll(); await quiet(); }
    const snapshot = globalThis.__scoutqaExtract();
    await flush();  // events first: once the page is sent, the crawl moves on and this document goes away
    await send({ type: 'content:crawl-page', kind, ref, url: location.href, snapshot });
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (mode !== 'crawl') return false;
    if (msg.type === 'crawl:click') {
      const el = document.querySelector(`[data-scoutqa-ref="${CSS.escape(msg.ref)}"]`);
      if (!el) { sendResponse({ clicked: false }); return false; }
      el.click();
      sendResponse({ clicked: true });
      crawlCapture('click', msg.ref);
      return false;
    }
    if (msg.type === 'crawl:capture') { crawlCapture('load'); sendResponse({}); }
    return false;
  });

  // ---------------------------------------------------------------- page observer + navigation
  // Observations are buffered even before the mode is known: pages make calls (and may be blocked) while
  // the hello round-trip is still in flight. They are sent once the mode is set.
  window.addEventListener('message', (event) => {
    const data = event.data;
    if (event.source !== window || !data || data.__scoutqa !== 'scoutqa-observer') return;
    if (data.kind === 'start') inflight += 1;
    else if (data.kind === 'end') {
      inflight = Math.max(0, inflight - 1);
      if (data.call && typeof data.call.url === 'string' && apiQueue.length < 200) {
        apiQueue.push(data.call);
        // data-changing calls often precede a navigation: send them right away
        const mutating = !['GET', 'HEAD', 'OPTIONS'].includes(String(data.call.method).toUpperCase());
        if (mode && (mutating || apiQueue.length >= 20)) flush();
      }
    } else if (data.kind === 'dialog' && data.dialog && dialogQueue.length < 50) {
      dialogQueue.push({ type: String(data.dialog.type), message: String(data.dialog.message || '') });
      if (mode) flush();
    } else if (data.kind === 'nav' && mode === 'record') {
      setTimeout(() => { if (location.href !== lastUrl) capture('navigate'); }, 0);
    }
  });
  // Tell the page-world scripts we are listening, so they replay what happened before we started.
  window.postMessage({ __scoutqa: 'scoutqa-content-ready' }, window.location.origin);

  window.addEventListener('popstate', () => { if (mode === 'record') capture('navigate'); });
  window.addEventListener('hashchange', () => { if (mode === 'record') capture('navigate'); });
  window.addEventListener('pagehide', () => { flush(); });

  send({ type: 'content:hello' }).then((res) => {
    if (res && res.crawl) {
      mode = 'crawl';
      try { sessionStorage.setItem(CRAWL_FLAG, '1'); } catch (e) { /* storage disabled */ }
      crawlCapture('load');
    } else if (res && res.recording) {
      mode = 'record';
      capture('load');
    }
  });
})();
