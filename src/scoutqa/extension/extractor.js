/*
 * ScoutQA page extractor — shared by the Playwright crawler and the browser extension.
 *
 * Defines globalThis.__scoutqaExtract(options) -> RawSnapshot (plain JSON).
 * Rules:
 *   - read-only: never clicks, types or submits; the only DOM write is the data-scoutqa-ref marker
 *   - no field VALUES are read (only structure, labels and constraints)
 *   - walks open shadow roots; frames are handled by the caller (one call per frame)
 * Keep this file dependency-free (it is injected as-is).
 */
(() => {
  if (globalThis.__scoutqaExtract) return;

  const REF_ATTR = 'data-scoutqa-ref';
  const MAX_TEXT = 120;
  const MAX_OPTIONS = 12;
  const clean = (s, n = MAX_TEXT) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);

  // ---------------------------------------------------------------- traversal (incl. open shadow roots)
  function* walk(root) {
    const stack = [root];
    while (stack.length) {
      const node = stack.pop();
      const children = node.children ? [...node.children] : [];
      for (let i = children.length - 1; i >= 0; i--) stack.push(children[i]);
      if (node.nodeType === 1) {
        yield node;
        if (node.shadowRoot) stack.push(...[...node.shadowRoot.children].reverse());
      }
    }
  }

  const byId = (el, id) => (el.getRootNode && el.getRootNode().getElementById
    ? el.getRootNode().getElementById(id) : document.getElementById(id)) || document.getElementById(id);

  function visible(el) {
    if (!el.getClientRects || el.getClientRects().length === 0) return false;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    return !el.closest('[aria-hidden="true"], [hidden], [inert]');
  }

  // ---------------------------------------------------------------- roles & names
  const INPUT_ROLES = {
    button: 'button', submit: 'button', reset: 'button', image: 'button', checkbox: 'checkbox',
    radio: 'radio', range: 'slider', number: 'spinbutton', search: 'searchbox', email: 'textbox',
    tel: 'textbox', text: 'textbox', url: 'textbox', password: 'textbox',
  };

  function roleOf(el) {
    const explicit = (el.getAttribute('role') || '').trim().split(/\s+/)[0];
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') return INPUT_ROLES[(el.type || 'text').toLowerCase()] || 'textbox';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'dialog') return 'dialog';
    if (tag === 'table') return 'table';
    if (tag === 'form') return 'form';
    if (tag === 'nav') return 'navigation';
    return 'generic';
  }

  function textWithoutControls(el) {
    const clone = el.cloneNode(true);
    clone.querySelectorAll('input, select, textarea, button').forEach(c => c.remove());
    return clone.textContent || '';
  }

  function nameOf(el) {
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const t = labelledBy.split(/\s+/).map(id => byId(el, id)?.textContent || '').join(' ');
      if (clean(t)) return clean(t);
    }
    const aria = el.getAttribute('aria-label');
    if (clean(aria)) return clean(aria);
    const tag = el.tagName.toLowerCase();
    if (['input', 'select', 'textarea'].includes(tag)) {
      if (el.labels && el.labels.length) {
        const t = [...el.labels].map(textWithoutControls).join(' ');
        if (clean(t)) return clean(t);
      }
      if (tag === 'input' && ['button', 'submit', 'reset'].includes(el.type)) {
        return clean(el.value) || (el.type === 'submit' ? 'Submit' : el.type === 'reset' ? 'Reset' : '');
      }
      if (tag === 'input' && el.type === 'image') return clean(el.alt);
      return clean(el.placeholder) || clean(el.title) || '';
    }
    const text = clean(el.innerText || el.textContent);
    if (text) return text;
    const img = el.querySelector && el.querySelector('img[alt], svg[aria-label]');
    if (img) return clean(img.getAttribute('alt') || img.getAttribute('aria-label'));
    return clean(el.title);
  }

  function region(el) {
    const dialog = el.closest('dialog[open], [role="dialog"], [role="alertdialog"]');
    if (dialog) return `dialog:${dialog.getAttribute(REF_ATTR) || ''}`;
    if (el.closest('header, nav, footer, [role="banner"], [role="navigation"], [role="contentinfo"]')) {
      // a <header> inside <main>/<article> is page content, not site chrome
      const chrome = el.closest('header, nav, footer, [role="banner"], [role="navigation"], [role="contentinfo"]');
      if (!chrome.closest('main, article, [role="main"]')) return 'chrome';
    }
    return 'main';
  }

  // ---------------------------------------------------------------- locators (stored, never sent to an LLM)
  const GENERATED_ID = /(^|[-_:])([0-9a-f]{6,}|\d{3,})($|[-_:])|^(ember|react|mui|radix|headlessui|:r)/i;

  function cssPath(el) {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 4) {
      if (node.id && !GENERATED_ID.test(node.id)) { parts.unshift(`#${CSS.escape(node.id)}`); break; }
      const tag = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (!parent) { parts.unshift(tag); break; }
      const same = [...parent.children].filter(c => c.tagName === node.tagName);
      parts.unshift(same.length > 1 ? `${tag}:nth-of-type(${same.indexOf(node) + 1})` : tag);
      node = parent;
    }
    return parts.join(' > ');
  }

  function locators(el, role, name) {
    const out = {};
    for (const attr of ['data-testid', 'data-test', 'data-qa', 'data-cy']) {
      if (el.hasAttribute(attr)) { out.testId = `[${attr}="${el.getAttribute(attr)}"]`; break; }
    }
    if (el.id && !GENERATED_ID.test(el.id)) out.id = `#${el.id}`;
    if (el.getAttribute('name')) out.name = `${el.tagName.toLowerCase()}[name="${el.getAttribute('name')}"]`;
    if (name) out.role = [role, name];
    out.css = cssPath(el);
    return out;
  }

  // ---------------------------------------------------------------- main
  globalThis.__scoutqaExtract = (options = {}) => {
    const root = document;
    for (const el of walk(root.documentElement || root)) el.removeAttribute(REF_ATTR);
    let counter = 0;
    const mark = (el) => { const ref = `e${++counter}`; el.setAttribute(REF_ATTR, ref); return ref; };

    const out = {
      url: location.href, title: clean(document.title, 200), headings: [], links: [], controls: [],
      fields: [], forms: [], tables: [], dialogs: [], messages: [], pagination: false,
    };

    const all = [...walk(root.documentElement)];

    // dialogs and forms first, so descendants can refer to them
    for (const el of all) {
      if (el.matches('dialog[open], [role="dialog"], [role="alertdialog"]') && visible(el)) {
        out.dialogs.push({ ref: mark(el), name: nameOf(el) || clean(el.querySelector('h1,h2,h3')?.textContent) });
      }
    }
    for (const el of all) {
      if (el.tagName === 'FORM' && visible(el)) {
        const heading = el.querySelector('legend, h1, h2, h3, h4');
        out.forms.push({
          ref: mark(el), name: clean(el.getAttribute('aria-label') || el.getAttribute('name') || heading?.textContent),
          method: (el.getAttribute('method') || 'get').toLowerCase(), region: region(el),
        });
      }
    }

    // tables: summarise structure; only the first data row's controls are listed individually
    const rowsToSkip = new Set();
    for (const el of all) {
      if (!(el.tagName === 'TABLE' || el.getAttribute('role') === 'grid' || el.getAttribute('role') === 'table')) continue;
      if (!visible(el)) continue;
      const headerCells = [...el.querySelectorAll('thead th, [role="columnheader"]')];
      const firstRow = el.querySelector('tr');
      const cells = headerCells.length ? headerCells : (firstRow ? [...firstRow.children] : []);
      const bodyRows = [...el.querySelectorAll('tbody tr, [role="row"]')].filter(r => !r.querySelector('th, [role="columnheader"]'));
      const actions = new Set();
      bodyRows.slice(0, 3).forEach(r => r.querySelectorAll('a[href], button, [role="button"], input[type="button"], input[type="submit"]')
        .forEach(c => actions.add(nameOf(c).replace(/\d+/g, '{n}'))));
      bodyRows.slice(1).forEach(r => rowsToSkip.add(r));
      out.tables.push({
        ref: mark(el), caption: clean(el.querySelector('caption')?.textContent || el.getAttribute('aria-label')),
        columns: cells.map(c => clean(c.textContent, 60)).filter(Boolean).slice(0, 20),
        rowCount: bodyRows.length, rowActions: [...actions].filter(Boolean).slice(0, 10),
        sortable: headerCells.some(c => c.hasAttribute('aria-sort') || c.querySelector('button, a')),
        region: region(el),
      });
    }
    const inSkippedRow = (el) => { const tr = el.closest('tr, [role="row"]'); return tr && rowsToSkip.has(tr); };
    const tableOf = (el) => el.closest('table, [role="grid"], [role="table"]')?.getAttribute(REF_ATTR) || null;
    const formOf = (el) => (el.form || el.closest('form'))?.getAttribute(REF_ATTR) || null;

    for (const el of all) {
      const tag = el.tagName.toLowerCase();
      const role = roleOf(el);

      if (role === 'heading' && visible(el)) {
        const level = Number(el.getAttribute('aria-level')) || Number(tag.slice(1)) || 2;
        out.headings.push({ level, text: clean(el.textContent), region: region(el) });
        continue;
      }

      if ((tag === 'a' || tag === 'area') && el.hasAttribute('href')) {
        // Links in every table row are kept for crawling; the distiller summarises them as row actions.
        let href;
        try { href = new URL(el.getAttribute('href'), document.baseURI).href; } catch (e) { continue; }
        out.links.push({
          ref: mark(el), href, text: nameOf(el), id: el.id || null, download: el.hasAttribute('download'),
          region: region(el), visible: visible(el), table: tableOf(el),
          locators: locators(el, 'link', nameOf(el)),
        });
        continue;
      }

      const isField = (tag === 'input' && !['button', 'submit', 'reset', 'image', 'hidden'].includes(el.type))
        || tag === 'select' || tag === 'textarea' || el.isContentEditable && el.getAttribute('role') === 'textbox';
      if (isField) {
        if (!visible(el) || inSkippedRow(el)) continue;
        const f = {
          ref: mark(el), tag, type: tag === 'input' ? (el.type || 'text').toLowerCase() : tag,
          role, label: nameOf(el), name: el.getAttribute('name') || null,
          placeholder: clean(el.getAttribute('placeholder')) || null,
          required: el.required || el.getAttribute('aria-required') === 'true',
          readonly: !!el.readOnly, disabled: !!el.disabled,
          min: el.getAttribute('min'), max: el.getAttribute('max'), step: el.getAttribute('step'),
          minLength: el.minLength > 0 ? el.minLength : null, maxLength: el.maxLength > 0 ? el.maxLength : null,
          pattern: el.getAttribute('pattern'), accept: el.getAttribute('accept'), multiple: !!el.multiple,
          autocomplete: el.getAttribute('autocomplete'), options: [], optionCount: 0,
          requiredMessage: null, describedBy: null, form: formOf(el), region: region(el),
          locators: locators(el, role, nameOf(el)),
        };
        if (tag === 'select') {
          const opts = [...el.options].map(o => clean(o.textContent, 60)).filter(Boolean);
          f.optionCount = opts.length; f.options = opts.slice(0, MAX_OPTIONS);
        }
        // Browser's own message for a required field that is empty right now (never reads the value).
        if (f.required && el.willValidate && el.validity && el.validity.valueMissing) f.requiredMessage = el.validationMessage;
        const described = el.getAttribute('aria-describedby');
        if (described) f.describedBy = clean(described.split(/\s+/).map(id => byId(el, id)?.textContent || '').join(' ')) || null;
        out.fields.push(f);
        continue;
      }

      const isControl = tag === 'button' || tag === 'summary'
        || (tag === 'input' && ['button', 'submit', 'reset', 'image'].includes(el.type))
        || ['button', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'switch', 'option'].includes(el.getAttribute('role') || '');
      if (isControl) {
        if (!visible(el) || inSkippedRow(el)) continue;
        const form = el.form || el.closest('form');
        const name = nameOf(el);
        out.controls.push({
          ref: mark(el), tag, role, name, type: el.getAttribute('type'), id: el.id || null,
          form: formOf(el), formMethod: form ? (form.getAttribute('method') || 'get').toLowerCase() : null,
          disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
          expanded: el.hasAttribute('aria-expanded') ? el.getAttribute('aria-expanded') === 'true' : null,
          selected: el.hasAttribute('aria-selected') ? el.getAttribute('aria-selected') === 'true' : null,
          hasPopup: el.getAttribute('aria-haspopup'), controlsId: el.getAttribute('aria-controls'),
          region: region(el), table: tableOf(el), locators: locators(el, role, name),
        });
        continue;
      }

      if (['alert', 'status'].includes(el.getAttribute('role')) && visible(el)) {
        const text = clean(el.textContent, 200);
        if (text) out.messages.push({ kind: el.getAttribute('role'), text });
      }
    }

    const numbered = out.links.filter(l => /^(\d+|next|previous|prev|›|»|last|first)$/i.test(l.text)).length;
    out.pagination = numbered >= 2
      || !!document.querySelector('nav[aria-label*="pagination" i], .pagination, [role="navigation"][aria-label*="page" i]');
    return out;
  };
})();
