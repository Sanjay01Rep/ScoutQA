"""A small local web app used as the crawl target in tests.

It deliberately contains everything the crawler must handle or must NOT touch, and records every request
so tests can assert safety invariants ("no mutating request other than the login POST ever arrived").

Traps (must never be requested):  /logout  /items/delete-all  /checkout/pay  /files/manual.pdf
Auto-fired on /settings load:     confirm() dialog, POST /api/track beacon (must be blocked)
Async content:                    /reports (fetch after delay + lazy section), /app/* (SPA router)
In-page states (M2):              /items/{id}: History tab (reveals /items/{id}/audit), "Add note" dialog
Roles (M2):                       qa-user = admin (Admin page, Delete buttons), qa-viewer = viewer
Change detection (M2):            FixtureState.settings_variant adds a Phone field to /settings
"""

from __future__ import annotations

import html
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

USERNAME = "qa-user"
PASSWORD = "fixture-Pa55word!"  # dummy credentials for the local fixture only
VIEWER_USERNAME = "qa-viewer"
VIEWER_PASSWORD = "fixture-Vi3wer!"
USERS = {USERNAME: (PASSWORD, "admin"), VIEWER_USERNAME: (VIEWER_PASSWORD, "viewer")}
PII_EMAIL = "qa.user@example.com"
PII_PHONE = "+1 555 123 4567"

TRAPS = ("/logout", "/items/delete-all", "/checkout/pay", "/files/manual.pdf")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


@dataclass
class FixtureState:
    requests: list[tuple[str, str]] = field(default_factory=list)
    sessions: dict[str, str] = field(default_factory=dict)  # sid -> role
    settings_variant: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, method: str, path: str) -> None:
        with self.lock:
            self.requests.append((method, path))

    def mutations(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.requests if m not in SAFE_METHODS]

    def login_posts(self) -> int:
        return sum(1 for m, p in self.requests if m == "POST" and p == "/login")

    def paths(self) -> set[str]:
        return {p for _, p in self.requests}

    def expire_sessions(self) -> None:
        with self.lock:
            self.sessions.clear()

    def reset(self) -> None:
        with self.lock:
            self.requests.clear()
            self.sessions.clear()
            self.settings_variant = False


def _page(title: str, body: str, script: str = "", role: str = "admin") -> str:
    admin = '<a href="/admin/users">Admin</a>' if role == "admin" else ""
    nav = f"""
    <header><nav aria-label="Main">
      <a href="/dashboard">Dashboard</a> <a href="/items">Items</a> <a href="/reports">Reports</a>
      <a href="/settings">Settings</a> <a href="/app/orders">Orders</a> {admin} <a href="/logout">Log out</a>
    </nav></header>"""
    return f"""<!doctype html><html><head><title>{html.escape(title)}</title></head>
<body>{nav}<main><h1>{html.escape(title)}</h1>{body}</main>
<footer><a href="/help">Help</a></footer><script>{script}</script></body></html>"""


LOGIN_PAGE = """<!doctype html><html><head><title>Sign in</title></head><body><main>
<h1>Sign in</h1>{error}
<form method="post" action="/login{next}">
  <label>Username <input name="username" autocomplete="username" required></label>
  <label>Password <input name="password" type="password" required></label>
  <button type="submit">Sign in</button>
</form><a href="/help">Help</a></main></body></html>"""

SPA_SHELL = """<!doctype html><html><head><title>Orders App</title></head><body>
<nav><a href="/app/orders" data-link>Orders</a> <a href="/app/customers" data-link>Customers</a>
<a href="/dashboard">Back to dashboard</a></nav>
<main id="view" aria-busy="true">Loading…</main>
<script>
const view = document.getElementById('view');
async function render() {
  const path = location.pathname;
  view.setAttribute('aria-busy', 'true');
  view.textContent = 'Loading…';
  const m = path.match(/^\\/app\\/(orders|customers)(?:\\/(\\d+))?$/);
  if (!m) { view.innerHTML = '<h1>Not found</h1>'; view.removeAttribute('aria-busy'); return; }
  const [, kind, id] = m;
  const data = await (await fetch('/api/' + kind)).json();
  if (id) {
    view.innerHTML = `<h1>${kind} #${id}</h1><a href="/app/${kind}" data-link>All ${kind}</a>`;
  } else {
    view.innerHTML = `<h1>${kind}</h1><ul>` +
      data.map(x => `<li><a href="/app/${kind}/${x.id}" data-link>${x.name}</a></li>`).join('') + '</ul>';
  }
  view.removeAttribute('aria-busy');
}
document.addEventListener('click', e => {
  const a = e.target.closest('a[data-link]');
  if (a) { e.preventDefault(); history.pushState({}, '', a.href); render(); }
});
window.addEventListener('popstate', render);
render();
</script></body></html>"""

ITEM_FORM = """
<form method="post" action="/items">
  <label>Name <input name="name" required maxlength="50"></label>
  <label>Quantity <input name="qty" type="number" min="1" max="99"></label>
  <label>Owner email <input name="owner" type="email" required></label>
  <label>Status <select name="status"><option>Active</option><option>Paused</option></select></label>
  <button type="submit">Save</button>
</form><a href="/items">Back</a>"""

# Record mode (M5): the create form submits JSON; the server answers 422 with a message or 201.
NEW_ITEM_SCRIPT = """
document.querySelector('form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  const res = await fetch('/api/items', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                         body: JSON.stringify(data)});
  const out = document.getElementById('result');
  if (res.ok) { location.href = '/items'; return; }
  const body = await res.json();
  out.setAttribute('role', 'alert');
  out.textContent = body.errors[0].message;
});
"""

ITEM_DETAIL_SCRIPT = """
document.querySelectorAll('[role=tab]').forEach(tab => tab.addEventListener('click', () => {
  document.querySelectorAll('[role=tab]').forEach(t => t.setAttribute('aria-selected', String(t === tab)));
  document.querySelectorAll('[role=tabpanel]').forEach(p => { p.hidden = p.id !== tab.getAttribute('aria-controls'); });
}));
const dlg = document.getElementById('note-dialog');
document.getElementById('open-note').addEventListener('click', () => dlg.showModal());
document.getElementById('close-note').addEventListener('click', () => dlg.close());
"""


def _items_table(role: str) -> str:
    rows = "".join(
        f'<tr><td><a href="/items/{i}">Item {i}</a></td><td>{i * 2}</td><td>Active</td>'
        f'<td>{"<button type=button>Delete</button>" if role == "admin" else ""}</td></tr>'
        for i in range(1, 13)
    )
    return f"""<a href="/items/new">New item</a>
    <table><caption>All items</caption>
      <thead><tr><th aria-sort="none"><button type="button">Name</button></th><th>Qty</th><th>Status</th>
      <th>Actions</th></tr></thead><tbody>{rows}</tbody></table>
    <nav aria-label="Pagination"><a href="/items?page=1">1</a> <a href="/items?page=2">2</a>
      <a href="/items?page=2">Next</a></nav>"""


def _item_detail(item: str, role: str) -> str:
    delete = (f'<form method="post" action="/items/{item}/delete"><button>Delete</button></form>'
              if role == "admin" else "")
    return f"""<p>Item {item}</p>
    <div role="tablist">
      <button role="tab" aria-selected="true" aria-controls="p-details" id="t-details">Details</button>
      <button role="tab" aria-selected="false" aria-controls="p-history" id="t-history">History</button>
    </div>
    <section id="p-details" role="tabpanel"><a href="/items/{item}/edit">Edit</a></section>
    <section id="p-history" role="tabpanel" hidden><ul><li>Created</li></ul>
      <a href="/items/{item}/audit">Full audit log</a></section>
    <button type="button" id="open-note" aria-haspopup="dialog">Add note</button>
    <dialog id="note-dialog" aria-label="Add note"><h2>Add note</h2>
      <form method="post" action="/items/{item}/notes">
        <label>Note <textarea name="note" required maxlength="500"></textarea></label>
        <button type="submit">Save note</button> <button type="button" id="close-note">Cancel</button>
      </form></dialog>
    {delete}"""


def make_handler(state: FixtureState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # silence stderr
            pass

        # -------------------------------------------------------------- plumbing
        def _send(self, status: int, body: str | bytes = "", ctype: str = "text/html; charset=utf-8",
                  headers: dict[str, str] | None = None) -> None:
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
            self._send(303, "", headers={"Location": location, **(headers or {})})

        def _role(self) -> str | None:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            sid = cookie["sid"].value if "sid" in cookie else None
            return state.sessions.get(sid) if sid else None

        def _body(self) -> str:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length).decode() if length else ""

        # -------------------------------------------------------------- verbs
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            state.record("GET", path)
            self._route_get(path)

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_POST(self) -> None:
            parts = urlsplit(self.path)
            state.record("POST", parts.path)
            body = self._body()
            if parts.path == "/login":
                form = parse_qs(body)
                nxt = parse_qs(parts.query).get("next", ["/dashboard"])[0]
                user = (form.get("username") or [""])[0]
                if user in USERS and form.get("password") == [USERS[user][0]]:
                    sid = secrets.token_hex(16)
                    state.sessions[sid] = USERS[user][1]
                    self._redirect(nxt if nxt.startswith("/") else "/dashboard",
                                   {"Set-Cookie": f"sid={sid}; Path=/; HttpOnly; SameSite=Lax"})
                else:
                    err = '<div role="alert">Invalid username or password</div>'
                    self._send(200, LOGIN_PAGE.format(error=err, next=""))
                return
            if parts.path == "/api/items" and self._role() is not None:
                try:
                    item = json.loads(body or "{}")
                except ValueError:
                    item = {}
                if str(item.get("name", "")).strip().lower() == "duplicate":
                    self._send(422, json.dumps({"errors": [{"field": "name", "message": "Name already exists"}]}),
                               "application/json")
                else:
                    self._send(201, json.dumps({"id": 13, **{k: str(v) for k, v in item.items()}}), "application/json")
                return
            self._send(204)

        def do_PUT(self) -> None:
            self._mutation()

        def do_PATCH(self) -> None:
            self._mutation()

        def do_DELETE(self) -> None:
            self._mutation()

        def _mutation(self) -> None:
            state.record(self.command, urlsplit(self.path).path)
            self._body()
            self._send(204)

        # -------------------------------------------------------------- pages
        def _route_get(self, path: str) -> None:
            if path == "/login":
                query = urlsplit(self.path).query
                nxt = f"?{query}" if query else ""
                self._send(200, LOGIN_PAGE.format(error="", next=nxt))
                return
            if path == "/help":
                self._send(200, "<!doctype html><title>Help</title><h1>Help</h1><a href='/login'>Sign in</a>")
                return
            if path == "/logout":
                self._redirect("/login", {"Set-Cookie": "sid=; Path=/; Max-Age=0"})
                return
            role = self._role()
            if role is None:
                self._redirect(f"/login?next={path}")
                return
            self._protected(path, role)

        def _protected(self, path: str, role: str) -> None:
            def page(title: str, body: str, script: str = "") -> None:
                self._send(200, _page(title, body, script, role))

            if path == "/":
                self._redirect("/dashboard")
            elif path == "/dashboard":
                body = f"""
                <p>Signed in as {USERNAME if role == 'admin' else VIEWER_USERNAME} ({PII_EMAIL}), phone {PII_PHONE}</p>
                <ul>
                  <li><a href="/items/delete-all">Delete all items</a></li>
                  <li><a href="/checkout/pay">Pay now</a></li>
                  <li><a href="https://example.org/docs">External docs</a></li>
                  <li><a href="mailto:support@example.com">Email support</a></li>
                  <li><a href="/files/manual.pdf">Manual (PDF)</a></li>
                  <li><a href="/dashboard?utm_source=newsletter">Dashboard (tracked)</a></li>
                  <li><a href="/items/new">Create item</a></li>
                </ul>
                <div id="host"></div>"""
                script = """
                const root = document.getElementById('host').attachShadow({mode: 'open'});
                root.innerHTML = '<a href="/reports/shadow">Shadow report</a>';"""
                page("Dashboard", body, script)
            elif path == "/items":
                page("Items", _items_table(role))
            elif path == "/items/new":
                page("Edit item", ITEM_FORM + '<div id="result"></div>', NEW_ITEM_SCRIPT)
            elif path.startswith("/items/") and path.endswith("/edit"):
                page("Edit item", ITEM_FORM)
            elif path.startswith("/items/") and path.endswith("/audit"):
                page("Audit log", "<p>No entries.</p>")
            elif path.startswith("/items/") and path.removeprefix("/items/").isdigit():
                item = path.removeprefix("/items/")
                page(f"Item {item}", _item_detail(item, role), ITEM_DETAIL_SCRIPT)
            elif path == "/admin/users":
                if role != "admin":
                    self._send(403, _page("Forbidden", "<p>You do not have access.</p>", role=role))
                    return
                page("Users", "<table><thead><tr><th>User</th><th>Role</th></tr></thead>"
                              "<tbody><tr><td>a</td><td>admin</td></tr></tbody></table>")
            elif path == "/reports":
                body = """
                <iframe src="/frame/summary" title="Summary"></iframe>
                <div id="async"></div>
                <div style="height: 3000px"></div>
                <div id="lazy">…</div>"""
                script = """
                setTimeout(async () => {
                  await fetch('/api/reports');
                  document.getElementById('async').innerHTML = '<a href="/reports/q1">Q1 report</a>';
                }, 300);
                new IntersectionObserver((entries, obs) => {
                  if (entries.some(e => e.isIntersecting)) {
                    document.getElementById('lazy').innerHTML = '<a href="/reports/annual">Annual report</a>';
                    obs.disconnect();
                  }
                }).observe(document.getElementById('lazy'));"""
                page("Reports", body, script)
            elif path.startswith("/reports/"):
                page(f"Report {path.rsplit('/', 1)[-1]}", "<p>Report</p>")
            elif path == "/frame/summary":
                self._send(200, '<!doctype html><title>Summary</title>'
                                '<a href="/reports/frame-only" target="_top">Frame-only report</a>')
            elif path == "/settings":
                phone = ('<label>Phone <input type="tel" name="phone" pattern="[0-9]{10}"></label>'
                         if state.settings_variant else "")
                body = f"""
                <form method="post" action="/settings"><label>Email <input type="email" name="email"></label>
                {phone}<button type="submit">Save settings</button></form>
                <button id="deleteAccount" onclick="fetch('/api/account', {{method: 'DELETE'}})">Delete account</button>"""
                script = """
                confirm('Discard unsaved changes?');
                fetch('/api/track', {method: 'POST', body: JSON.stringify({event: 'view'})});"""
                page("Settings", body, script)
            elif path == "/app" or path.startswith("/app/"):
                self._send(200, SPA_SHELL)
            elif path.startswith("/api/"):
                time.sleep(0.2)
                kind = path.removeprefix("/api/")
                data = [{"id": i, "name": f"{kind} {i}"} for i in range(1, 6)]
                self._send(200, json.dumps(data), "application/json")
            elif path in TRAPS:
                page("Trap", "<p>This must never be requested.</p>")
            else:
                self._send(404, _page("Not found", "", role=role))

    return Handler


class FixtureServer:
    def __init__(self) -> None:
        self.state = FixtureState()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host!s}:{port}/"

    def start(self) -> FixtureServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


if __name__ == "__main__":
    server = FixtureServer().start()
    print(f"Fixture app on {server.base_url}  (users: {USERNAME}=admin, {VIEWER_USERNAME}=viewer)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
