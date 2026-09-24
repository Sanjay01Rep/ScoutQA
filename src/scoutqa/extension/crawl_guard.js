// ScoutQA crawl guard — page world, document_start, only while the extension is crawling.
// In the crawl tab (marked with a per-tab sessionStorage flag set by the content script) it answers
// confirm() with "Cancel", swallows alert()/prompt() and refuses window.open(), reporting each dialog.
// In any other tab of the same app it changes nothing: the check happens when a dialog is called.
(() => {
  if (window.__scoutqaGuard) return;
  window.__scoutqaGuard = true;
  const FLAG = 'scoutqa-crawl-tab';
  const inCrawlTab = () => { try { return sessionStorage.getItem(FLAG) === '1'; } catch (e) { return false; } };
  // Dialogs often fire while the page is still loading, before the content script listens: buffer them.
  let ready = false;
  const early = [];
  const report = (type, message) => {
    const data = { __scoutqa: 'scoutqa-observer', kind: 'dialog',
                   dialog: { type, message: String(message || '').slice(0, 500) } };
    if (ready) window.postMessage(data, window.location.origin);
    else if (early.length < 50) early.push(data);
  };
  window.addEventListener('message', (event) => {
    if (event.source !== window || !event.data || event.data.__scoutqa !== 'scoutqa-content-ready' || ready) return;
    ready = true;
    early.splice(0).forEach((m) => window.postMessage(m, window.location.origin));
  });

  const original = { confirm: window.confirm, alert: window.alert, prompt: window.prompt, open: window.open };
  window.confirm = function (message) {
    if (!inCrawlTab()) return original.confirm.apply(this, arguments);
    report('confirm', message);
    return false; // "Cancel": whatever needed confirmation does not happen
  };
  window.alert = function (message) {
    if (!inCrawlTab()) return original.alert.apply(this, arguments);
    report('alert', message);
  };
  window.prompt = function (message) {
    if (!inCrawlTab()) return original.prompt.apply(this, arguments);
    report('prompt', message);
    return null;
  };
  window.open = function (url) {
    if (!inCrawlTab()) return original.open.apply(this, arguments);
    report('popup', url);
    return null;
  };
})();
