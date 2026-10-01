# ScoutQA — Step-by-Step Usage Guide

This walks through everything built so far (Milestones 1–6): the CLI crawler, rule-based test case
generation, template/Excel export, and the Chrome/Edge extension (Record mode + Crawl mode). No AI/API
keys are needed for any of this — Milestone 7+ (LLM generation) is not built yet.

---

## 1. One-time setup

### 1.1 Install

```powershell
py -3.12 -m venv $env:USERPROFILE\.scoutqa\venv
& $env:USERPROFILE\.scoutqa\venv\Scripts\Activate.ps1
pip install -e ".[dev]"
playwright install chromium
```

Run this from the project folder (where `pyproject.toml` is). The venv lives outside the project folder
on purpose — keep it there.

### 1.2 Check it worked

```powershell
scoutqa --help
```

You should see commands: `init`, `login`, `crawl`, `map`, `generate`, `template`, `export`, `serve`,
`extension`.

---

## 2. Point it at your app

### 2.1 Create a config file

```powershell
scoutqa init --project myapp --base-url https://myapp.example.com/
```

This writes `scoutqa.yaml` in the current folder. Open it and edit the `auth:` section:

```yaml
auth:
  type: form
  login_url: https://myapp.example.com/login
  username_env: MYAPP_USERNAME
  password_env: MYAPP_PASSWORD
```

For an app with no login, leave `auth.type: none`.

**Several roles (e.g. admin vs viewer)?** Use `auth.profiles` instead of `username_env`/`password_env`:

```yaml
auth:
  type: form
  login_url: https://myapp.example.com/login
  profiles:
    admin:  {username_env: MYAPP_ADMIN_USER,  password_env: MYAPP_ADMIN_PASS}
    viewer: {username_env: MYAPP_VIEWER_USER, password_env: MYAPP_VIEWER_PASS}
```

### 2.2 Set credentials as environment variables

Never put passwords in `scoutqa.yaml` — only the *names* of env vars go there.

```powershell
$env:MYAPP_USERNAME = "qa.user@example.com"
$env:MYAPP_PASSWORD = "..."
```

### 2.3 Review scope and safety settings

Still in `scoutqa.yaml`:

```yaml
scope:
  allowed_domains: []      # empty = just your base_url's host
  max_pages: 50
  max_depth: 3

safety:
  read_only: true          # keep this true unless you really want to allow writes
```

---

## 3. Path A — Automatic crawl (Playwright, headless)

Use this when the app has a normal username/password login (no SSO/MFA/CAPTCHA).

```powershell
scoutqa login              # logs in once, saves the session
scoutqa crawl               # crawls within scope, builds the app model
scoutqa map                 # prints a compact summary of what it found
```

- `login` only needs to be run once; `crawl` reuses the saved session and re-logs in only if it expired.
- Re-run `scoutqa crawl` any time — it's incremental (marks pages `new`/`changed`/`unchanged`/`removed`).
- Several roles: `scoutqa crawl --all-roles` (or `--role admin` for one at a time).

**Useful options:**
```powershell
scoutqa crawl --max-pages 20 --headed --verbose
```
`--headed` shows the browser window (handy for debugging); `--verbose` prints detailed logs.

---

## 4. Path B — Browser extension (your real Chrome/Edge session)

Use this for apps with SSO, MFA, CAPTCHA, or when you'd rather just click through the app yourself.

### 4.1 Start the local service

```powershell
scoutqa serve
```

This prints:
- a **pairing code** (e.g. `ABCD-EFGH`) — valid for 15 minutes, single use
- the **extension folder** path

Leave this window running.

### 4.2 Load the extension (one-time)

1. Open `chrome://extensions` (or `edge://extensions`)
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked**
4. Select the folder `scoutqa serve` printed (or run `scoutqa extension` to see it again)

Pin the ScoutQA icon to your toolbar if you like.

### 4.3 Pair

1. Click the ScoutQA toolbar icon — the side panel opens
2. Enter the port shown by `scoutqa serve` (default `8765`) and the pairing code
3. Click **Pair**

### 4.4a Record mode — you drive, it watches

1. Sign in to your app normally, in the same browser
2. In the side panel: pick a **Role**, click **Start recording**
3. Chrome will ask for permission to access your app's domain — allow it
4. Use the app: open pages, fill forms, submit, open dialogs/tabs
5. Click **Stop** when done

What it captures: page structure, which control led where, API call outcomes (method/status/field
names — never values), and the *shape* of what you typed (e.g. `Widget 7` → `Aaaaaa 9`). It never captures
passwords, cookies, or anything typed on the sign-in page.

### 4.4b Crawl mode — it explores on its own

1. Sign in to your app first (in a normal tab)
2. In the side panel, click **Crawl (read-only)**
3. A separate browser window opens and crawls automatically, using your session
4. Watch progress live in the side panel (pages visited, blocked requests, cancelled dialogs)
5. It stops on its own, or click **Stop crawl**

This is read-only by construction: the crawl tab blocks every POST/PUT/PATCH/DELETE at the browser level,
cancels confirmation dialogs, and never follows delete/pay/logout links.

### 4.5 Generate and export from the side panel

Once you've recorded or crawled something:
1. Click **Generate** — builds test cases (0 tokens, all local)
2. Pick a format (Excel/CSV/Markdown/JSON), click **Export**
3. The file downloads through your browser

(You can also just use the CLI for this — see step 5 below — the data is shared either way, stored in the
same local app model.)

---

## 5. Generate test cases and export (CLI)

Works the same whether the app model came from `scoutqa crawl`, Record mode, or Crawl mode.

```powershell
scoutqa generate --list      # generates cases, prints them all
```

Then export:

```powershell
scoutqa export -f xlsx                      # default template, into scoutqa-output/
scoutqa export -f xlsx -t my-template.xlsx   # your own Excel template
scoutqa export -f csv
scoutqa export -f md
scoutqa export -f json
```

### Using your own template

Preview how your template's columns will be filled, without writing anything:

```powershell
scoutqa template my-template.xlsx
```

This shows each column, what field it maps to, and why. Fix mismatches in `scoutqa.yaml`:

```yaml
template:
  path: my-template.xlsx
  columns:
    "Objective": title          # force a column to a specific field
    "Owner": blank               # leave a column empty
  defaults:
    "Environment": "QA"          # fill a column with a fixed value
```

Supported formats: `.xlsx`, `.csv`, `.md`, `.json`. Execution-only columns (Status, Actual Result,
Executed By, Defect ID, ...) are detected automatically and left blank.

---

## 5a. Model setup (for LLM-generated scenarios)

Plain `scoutqa generate` above is 100% rule-based and never calls a model. This section configures *which*
model(s) ScoutQA can call for the optional LLM stage (section 5b) — set it up and check it works first,
without it affecting the rule-based cases you already generate.

Add an `llm:` section to `scoutqa.yaml` (see `scoutqa.example.yaml` for every field):

```yaml
llm:
  profiles:
    claude: {provider: anthropic, model: claude-sonnet-5, api_key_env: ANTHROPIC_API_KEY}
    gpt:    {provider: openai, model: gpt-5.1, api_key_env: OPENAI_API_KEY}
    gemini: {provider: gemini, model: gemini-3-flash, api_key_env: GEMINI_API_KEY}
    local:  {provider: openai, model: llama3.1, base_url: "http://localhost:11434/v1"}  # Ollama, no key
    azure:  {provider: azure_openai, model: my-deployment, api_key_env: AZURE_OPENAI_KEY,
             azure_endpoint: "https://my-resource.openai.azure.com"}
  default_profile: claude    # used by any stage you don't route explicitly
  routing: {}                # e.g. {scenarios: gemini, expand: claude} — cheap model for scenarios,
                             # a stronger one for detailed cases
  budget_usd: 5.0            # optional: stop a run once estimated spend reaches this
  budget_tokens: null
```

Install only the SDK(s) you actually reference, and set the matching API key:

```powershell
pip install scoutqa[anthropic]   # or [openai] / [gemini] / [all]
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

Then check it works, and keep an eye on spend:

```powershell
scoutqa models                    # list configured profiles and which stage routes to which
scoutqa models --test             # one trivial request through the default profile
scoutqa models --test --stage expand --config scoutqa.yaml
scoutqa usage                     # tokens + estimated cost spent so far, per stage/model
```

Every response is cached by (provider, model, stage, prompt, schema) — an unchanged call costs 0 tokens
on a re-run. `provider: fake` needs no key or package and is a good first check that profiles/routing/CLI
all work before spending anything real.

---

## 5b. LLM-generated scenarios (optional, spends tokens)

Once a model profile works (section 5a), turn on the second generation stage: a model proposes business
scenarios your rule packs can't (edge cases, multi-step flows), then expands each into a detailed case.
Every step is checked against the app model — a step that invents an element, or an expected result nobody
actually observed, is flagged for review rather than silently trusted.

See the cost before spending anything:

```powershell
scoutqa generate --dry-run
```

This prints an approximate input-token/cost estimate per module from the app model alone — no network call,
nothing written. (It only estimates the scenario stage; expansion cost depends on how many scenarios come
back.)

Then generate for real:

```powershell
scoutqa generate --no-rules-only   # this run only, regardless of scoutqa.yaml
```

Or turn it on for every run by setting `generation.use_llm: true` in `scoutqa.yaml` (then plain
`scoutqa generate` includes the LLM stage; `--rules-only` still forces it off for one run if you need a
free run). Useful knobs, also in `scoutqa.yaml` under `generation:`:

```yaml
generation:
  use_llm: true
  scenarios_per_module: 10      # scenario ideas requested per module
  cross_module_scenarios: 5     # end-to-end scenarios spanning more than one module (0 to skip)
  cases_per_batch: 4            # scenarios expanded into detailed cases per LLM call
  context_pack:                 # optional grounding material, matched to modules by keyword (0 tokens)
    - requirements.md
    - existing-test-cases.json
```

Re-running `scoutqa generate` is incremental: a module whose app-model pages haven't changed won't get the
same scenario proposed twice (and an identical call is a free cache hit — see `scoutqa usage`).

**Reviewing LLM-proposed cases.** Every case with an assumption is flagged in `generate`'s summary and in
the exported report's Trace sheet. Work through them with:

```powershell
scoutqa review                              # list cases awaiting review
scoutqa review --approve TC-ITEMS-012       # mark reviewed (repeatable)
scoutqa review --reject TC-ITEMS-013        # drop it from exports
scoutqa review --unreview TC-ITEMS-012      # back to draft
```

A review decision is keyed to the case's content, not its row — regenerating never un-rejects a case or
loses an edit. `scoutqa export` always excludes rejected cases automatically.

---

## 6. What you get

The exported file contains:
- **Test Cases** — ID, module, title/scenario, preconditions, steps, expected result, priority (and
  anything else your template asks for)
- **Coverage** — pages, forms, fields found vs. covered by a case
- **Limitations** — what the crawl/recording could not see (skipped pages, blocked actions, unsubmitted
  forms, third-party frames), stated plainly
- **Trace** — every case's source page, elements, evidence, and any assumptions it makes

Cases with **assumptions** (nobody actually observed that behaviour) are flagged — review those first.
Cases built from **Record mode** evidence (a real server error message, a real successful submit) carry no
assumptions for that part.

---

## 7. Everyday commands, quick reference

| Command | What it does |
|---|---|
| `scoutqa init` | Write a starter `scoutqa.yaml` |
| `scoutqa login [--role R] [--force]` | Log in and save the session |
| `scoutqa crawl [--role R] [--all-roles]` | Crawl the app, update the app model |
| `scoutqa map [--role R]` | Print the compact app map |
| `scoutqa generate [--list]` | Generate rule-based test cases (0 tokens) |
| `scoutqa generate --no-rules-only` | Also run the LLM stage for this run |
| `scoutqa generate --dry-run` | Estimate the LLM stage's cost; nothing written |
| `scoutqa review [--approve\|--reject\|--unreview ID]` | List or decide on cases awaiting review |
| `scoutqa template [path]` | Preview a template's column mapping |
| `scoutqa export -f xlsx\|csv\|md\|json` | Export test cases |
| `scoutqa serve [--port N]` | Start the local service for the extension |
| `scoutqa extension` | Show where the extension folder is |
| `scoutqa models [--test] [--stage X]` | List model profiles/routing, or send one live test request |
| `scoutqa usage` | Show LLM tokens/cost spent so far |

Add `--verbose` to any command for detailed logs, `--headed` to `login`/`crawl` to watch the browser.

---

## 8. Where your data lives

Everything ScoutQA stores — sessions, the app database, crawl logs — lives in:

```
%USERPROFILE%\.scoutqa\projects\<project-name>\
```

Never in the project folder itself (it's gitignored anyway). Exports go to `.\scoutqa-output\` in whichever
folder you ran the command from.

To start over for a project: delete `%USERPROFILE%\.scoutqa\projects\<project-name>\` and re-run
`scoutqa login`/`crawl`.
