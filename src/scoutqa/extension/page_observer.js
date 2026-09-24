// ScoutQA page observer — runs in the page's own JavaScript world (document_start) while recording.
// Wraps fetch / XMLHttpRequest to observe API calls. What leaves this script:
//   method, URL, status, the SHAPE of request/response JSON (keys and types, never values),
//   and short error messages of failed calls (4xx/5xx), which the local service redacts.
// It also reports SPA navigations (history.pushState / replaceState).
(() => {
  if (window.__scoutqaObserver) return;
  window.__scoutqaObserver = true;

  const TAG = 'scoutqa-observer';
  // The content script starts later (document_idle): buffer until it says it is listening, then replay.
  let ready = false;
  const early = [];
  const post = (data) => {
    const message = { __scoutqa: TAG, ...data };
    if (ready) window.postMessage(message, window.location.origin);
    else if (early.length < 500) early.push(message);
  };
  window.addEventListener('message', (event) => {
    if (event.source !== window || !event.data || event.data.__scoutqa !== 'scoutqa-content-ready' || ready) return;
    ready = true;
    early.splice(0).forEach((m) => window.postMessage(m, window.location.origin));
  });
  const MAX_DEPTH = 3;
  const MAX_KEYS = 30;
  const MESSAGE_KEYS = /^(message|error|error_description|detail|title|msg|description|reason)$/i;

  function shape(value, depth = 0) {
    if (value === null) return 'null';
    if (Array.isArray(value)) return value.length ? [shape(value[0], depth + 1)] : [];
    const type = typeof value;
    if (type !== 'object') return type;
    if (depth >= MAX_DEPTH) return 'object';
    const out = {};
    for (const key of Object.keys(value).slice(0, MAX_KEYS)) out[key] = shape(value[key], depth + 1);
    return out;
  }

  function messages(value) {
    const out = [];
    const visit = (x, depth) => {
      if (out.length >= 5 || depth > 4 || x === null || x === undefined) return;
      if (Array.isArray(x)) { x.forEach((item) => visit(item, depth + 1)); return; }
      if (typeof x !== 'object') return;
      for (const [key, v] of Object.entries(x)) {
        if (typeof v === 'string' && MESSAGE_KEYS.test(key)) out.push(v.slice(0, 300));
        else visit(v, depth + 1);
      }
    };
    if (typeof value === 'string') out.push(value.slice(0, 300));
    else visit(value, 0);
    return out;
  }

  function bodyShape(body) {
    try {
      if (typeof body === 'string') {
        try { return shape(JSON.parse(body)); } catch (e) { /* not JSON */ }
        const params = new URLSearchParams(body);
        const keys = [...params.keys()];
        return keys.length ? Object.fromEntries(keys.map((k) => [k, 'string'])) : null;
      }
      if (body instanceof FormData || body instanceof URLSearchParams) {
        return Object.fromEntries([...body.keys()].map((k) => [k, 'string']));
      }
    } catch (e) { /* ignore */ }
    return null;
  }

  function absolute(url) {
    try { return new URL(url, document.baseURI).href; } catch (e) { return String(url); }
  }

  function report(method, url, status, requestBody, parsed) {
    const call = { method, url: absolute(url), status, request_shape: bodyShape(requestBody) };
    if (parsed !== undefined) {
      call.response_shape = shape(parsed);
      if (status >= 400) call.messages = messages(parsed);
    }
    post({ kind: 'end', call });
  }

  // ---------------------------------------------------------------- fetch
  const originalFetch = window.fetch;
  window.fetch = async function (input, init) {
    const url = typeof input === 'string' || input instanceof URL ? String(input) : input.url;
    const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    const body = init && init.body;
    post({ kind: 'start' });
    let response;
    try {
      response = await originalFetch.apply(this, arguments);
    } catch (e) {
      report(method, url, 0, body, undefined);
      throw e;
    }
    const type = response.headers.get('content-type') || '';
    const size = Number(response.headers.get('content-length') || 0);
    if (type.includes('json') && size < 1_000_000) {
      response.clone().json().then((data) => report(method, url, response.status, body, data),
                                   () => report(method, url, response.status, body, undefined));
    } else if (response.status >= 400 && type.includes('text/plain') && size < 10_000) {
      response.clone().text().then((text) => report(method, url, response.status, body, text),
                                   () => report(method, url, response.status, body, undefined));
    } else {
      report(method, url, response.status, body, undefined);
    }
    return response;
  };

  // ---------------------------------------------------------------- XMLHttpRequest
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__scoutqa = { method: String(method || 'GET').toUpperCase(), url: String(url) };
    return open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const info = this.__scoutqa;
    if (info) {
      post({ kind: 'start' });
      this.addEventListener('loadend', () => {
        let parsed;
        try {
          const type = this.getResponseHeader('content-type') || '';
          if (this.responseType === 'json') parsed = this.response;
          else if ((this.responseType === '' || this.responseType === 'text') && type.includes('json')) {
            parsed = JSON.parse(this.responseText);
          }
        } catch (e) { parsed = undefined; }
        report(info.method, info.url, this.status, body, parsed);
      });
    }
    return send.apply(this, arguments);
  };

  // ---------------------------------------------------------------- SPA navigation
  for (const name of ['pushState', 'replaceState']) {
    const original = history[name];
    history[name] = function () {
      const result = original.apply(this, arguments);
      post({ kind: 'nav' });
      return result;
    };
  }
})();
