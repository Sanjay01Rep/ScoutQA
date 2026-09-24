# ScoutQA

Crawl a web application (logging in if needed) and generate test scenarios and manual test cases in your
own template. Deterministic code does the crawling, parsing and rule-based cases; LLMs only see small,
structured summaries. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

> Phase 1 progress: ✅ M1 crawler, login, session reuse, safety guard · ✅ M2 distiller, app model
> (SQLite), in-page states, roles · ✅ M3 rule-based test cases · ✅ M4 template + export
> (crawl → generate → Excel with 0 tokens) · ✅ M5 local service + browser extension (Record mode)
> · ✅ M6 extension Crawl mode · ✅ M7 model adapter (Anthropic, OpenAI + any compatible endpoint,
> Azure OpenAI, Gemini) · next: M8 LLM scenario/test-case generation.

## Model setup (M7)

Provider-agnostic: one interface, several providers, your choice of which model does which stage of
generation (M8 will add `scenarios`/`expand`; any stage name already works). Nothing here calls a model
until you ask it to — Milestones 1–6 are all 0 tokens regardless of what's configured here.

```yaml
# scoutqa.yaml
llm:
  profiles:
    claude: {provider: anthropic, model: claude-sonnet-5, api_key_env: ANTHROPIC_API_KEY}
    gpt:    {provider: openai, model: gpt-5.1, api_key_env: OPENAI_API_KEY}
    local:  {provider: openai, model: llama3.1, base_url: "http://localhost:11434/v1"}  # Ollama, keyless
  default_profile: claude
  budget_usd: 5.0        # optional: stop a run once estimated spend reaches this
```

```powershell
pip install scoutqa[anthropic]   # only the provider(s) you actually use need their SDK installed
$env:ANTHROPIC_API_KEY = "..."
scoutqa models                   # list configured profiles and routing
scoutqa models --test            # send one trivial request to check a profile actually works
scoutqa usage                    # tokens and estimated cost spent so far, per stage/model
```

`provider: openai` with a `base_url` reaches **any** OpenAI-compatible endpoint: Ollama, vLLM, LM Studio,
Groq, DeepSeek, Together, OpenRouter, Mistral, xAI, and more — not just OpenAI itself. `azure_openai` is a
separate provider for Azure's own auth (`azure_endpoint` + deployment name). `provider: fake` needs no key
or package at all — useful to check the rest of the pipeline (caching, budget, `scoutqa usage`) works
before spending real tokens. Every response is cached (identical call = 0 tokens on a re-run) and every
real call is logged to the usage ledger, whether it succeeds or not.

## Record mode (browser extension)

Use the app in **your own browser** — SSO, MFA and CAPTCHA just work, and ScoutQA never sees credentials.

```powershell
scoutqa serve          # prints a pairing code and the extension folder
```

1. Chrome/Edge → `chrome://extensions` → Developer mode → **Load unpacked** → the folder printed above
   (`scoutqa extension` shows it too).
2. Click the ScoutQA toolbar icon to open the side panel, enter the pairing code, **Start recording**
   (Chrome asks for access to your app's domain only).
3. Use the app: sign in, open pages, fill forms, submit, open dialogs. Then **Stop**, **Generate**, **Export**.

What is captured: page structure (same extractor as the crawler), which control led where, API calls
(method, endpoint, status, request/response *field names and types*, server error messages), and the
*format* of typed values (`Widget 7` → `Aaaaaa 9`). Never captured: field values, passwords, anything typed
on the sign-in page, cookies, screenshots. Everything goes only to `127.0.0.1`.

Recorded server errors become negative cases with the app's real message; a recorded successful submit
turns the form's happy-path case from "assumed" into "observed".

## Crawl mode (browser extension)

Sign in to the app in your browser, then click **Crawl (read-only)** in the side panel. ScoutQA explores the
app in a separate window with your session — ideal for SSO/MFA apps the headless crawler cannot log into.
The service decides every step with the same scope, budget and safety rules as `scoutqa crawl`; the
extension only drives the tab. Read-only is enforced by the browser itself: the crawl tab gets
`declarativeNetRequest` rules that block POST/PUT/PATCH/DELETE (including form submissions), `confirm()` is
answered "Cancel", popups are refused, and delete / pay / logout links are never followed. Your other tabs
are not affected.

Limits: content inside iframes is not crawled in this mode (use `scoutqa crawl`), and GraphQL queries sent
as POST are blocked too (the browser cannot inspect request bodies); allow them with
`safety.allow_mutation_patterns` if needed.

## Setup (Windows, PowerShell)

The virtualenv lives outside the OneDrive-synced project folder:

```powershell
py -3.12 -m venv $env:USERPROFILE\.scoutqa\venv
& $env:USERPROFILE\.scoutqa\venv\Scripts\Activate.ps1
pip install -e ".[dev]"
playwright install chromium
```

## Usage

```powershell
scoutqa init --project myapp --base-url https://myapp.example.com/   # writes scoutqa.yaml
# edit scoutqa.yaml (auth section), then provide credentials via env vars:
$env:MYAPP_USERNAME = "qa.user@example.com"
$env:MYAPP_PASSWORD = "..."
scoutqa login          # logs in once, saves the session
scoutqa crawl          # reuses the session; re-logs in only if it expired
scoutqa map            # compact app map: layouts, pages, forms, tables, dialogs, in-page states
scoutqa generate --list  # rule-based test cases (0 tokens) -> cases.json in the project folder
scoutqa template team-template.xlsx   # preview how your template's columns will be filled
scoutqa export -f xlsx -t team-template.xlsx   # -> scoutqa-output/<project>-testcases-<time>.xlsx
```

**Templates:** `.xlsx`, `.csv`, `.md` or `.json` (list of columns or JSON Schema). Columns are matched by
name ("TC ID", "Test Scenario", "Pre-Conditions", "Test Steps", "Expected Result(s)", "Severity", …);
execution columns (Status, Actual Result, Executed By, Defect ID, …) stay blank. With an Excel template
the output is a copy of *your* workbook — title rows, styling, dropdowns (Priority `P1/P2/P3` etc.) and
other sheets are kept. A "Step No" column switches to one row per step. Unknown columns are left blank;
fill them with `template.defaults` or map them with `template.columns`.

Every export adds **Coverage** (pages, fields covered, cases per page), **Limitations** (what crawling
could not see: unvisited pages by reason, blocked actions, unsubmitted forms, third-party frames) and
**Trace** (each case's rule, page, elements, evidence and assumptions) sheets.

**Rule packs** (`generation.rule_packs`): `fields` (required, length, range, format, pattern, options),
`forms` (valid submit, empty submit), `actions` (destructive/transactional actions ScoutQA never ran),
`tables` (sort, pagination), `dialogs` (dialogs, tabs), `navigation`, `auth`, `access` (role differences),
`smoke`. Every case keeps a stable ID (`TC-ITEMS-007`), its source state, the *evidence* the crawler
observed, and any *assumptions* — cases with assumptions are flagged for review.

**Several roles:** define `auth.profiles` (e.g. `admin`, `viewer`) in `scoutqa.yaml`, then
`scoutqa crawl --all-roles`. Each role has its own saved session and its own states in the app model,
so permission differences can be compared.

**Re-crawls are incremental:** every state is marked `new`, `changed`, `unchanged` or `removed`
(removed only after a complete crawl) against the previous run of the same role.

Credentials are only ever referenced by env-var **name** in `scoutqa.yaml`. Sessions and crawl results
are stored in `%USERPROFILE%\.scoutqa\projects\<project>\` (override with `SCOUTQA_HOME`), never in
this folder.

## Safety

Read-only by default. Four independent layers stop the crawler from changing data:

1. Link/button intent classifier (logout, delete, pay, submit … are never followed).
2. Structural rules (submit buttons, POST forms).
3. Network guard: every non-GET/HEAD/OPTIONS request is aborted except `safety.allow_mutation_patterns`
   (login is the only exception, and only while logging in).
4. Browser guards: `confirm()` dialogs dismissed, downloads cancelled, popups closed.

Everything blocked is recorded in the crawl result.

## Tests

```powershell
pytest                 # unit + browser tests against a local fixture app
ruff check . ; mypy
```
