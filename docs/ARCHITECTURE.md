# ScoutQA — Architecture (Phase 1)

ScoutQA crawls a web application (logging in if needed), builds a compact model of its UI, and generates
test scenarios and manual test cases in a user-supplied template. Deterministic code does the heavy work;
LLMs only see small, structured, reference-based summaries.

> Status: **approved** (2026-09-24), revised with the **hybrid capture design** (§3.14): a Chrome/Edge
> extension captures pages in the user's own logged-in browser; the Python core does everything else.
> Phase 2 (Playwright script generation, change detection, Jira/TestRail sync) is out of scope; Phase 1
> only stores the data those features will need.

---

## 1. Design principles

1. **Zero tokens until generation.** Crawling, distilling, dedup, modelling, rule-based cases, template
   mapping and rendering are all code. No LLM in the crawl loop.
2. **The unit of crawl is a UI *state*, not a URL.** SPAs, modals, tabs and wizard steps share URLs.
3. **Safety is defense-in-depth**, not a keyword list: intent classifier + structural rules + network
   mutation guard + auto-dismissed confirm dialogs.
4. **Storage ≠ prompt.** SQLite keeps full fidelity (locators, attributes); a separate serializer renders a
   compact text DSL for the LLM. Locators and raw DOM never reach a model.
5. **LLM output is reference-based and verified.** Models cite page/element IDs; a validator rejects unknown
   refs, dedups, and separates *evidence* from *assumption*.
6. **The LLM fills a canonical schema, never the user's format.** Template mapping and rendering are code, so
   a template change is a free re-export.
7. **Everything content-addressed and cached.** Unchanged pages/modules cost zero tokens on re-runs.
8. **Library first.** `scoutqa` is a typed Python library + CLI; the MCP server is a thin wrapper.
9. **Secrets by reference only.** Config and MCP tools take env-var *names*, never values.
10. **Capture front-ends are thin.** Playwright, the extension's Record mode and its Crawl mode all run the
    *same* `extractor.js` and send the same summaries; scope, safety, model and generation live only in Python.
11. **Every observation carries a role.** The app model is keyed by (state, role), so permission differences
    between user profiles are data, not an afterthought.

---

## 2. Pipeline

```
                ┌──────────────────────────── CRAWL (0 tokens) ────────────────────────────┐
 scoutqa.yaml → │ Auth Manager ─► Browser (Playwright) ─► Explorer (state BFS) ─► Extractor │
  (project)     │      ▲                   ▲    Safety Guard (classifier + network guard)   │
                │ session store            └──── Scope (allowlist, URL patterns, budgets) │
                └───────────────────────────────────────────┬──────────────────────────────┘
                                                            ▼ RawSnapshot (local only)
                ┌──────────────────────────── DISTILL (0 tokens) ──────────────────────────┐
                │ Redactor ─► PageSpec builder ─► Fingerprints (structure / content)       │
                │           ─► Shared-layout extraction ─► Template-page & list collapsing │
                └───────────────────────────────────────────┬──────────────────────────────┘
                                                            ▼
                ┌────────────────── APP MODEL (SQLite, per project) ───────────────────────┐
                │ runs · states · elements · transitions · layouts · templates ·           │
                │ blocked_actions · modules/flows · scenarios · cases · llm_cache · usage  │
                └───────┬───────────────────────────────────────────────┬──────────────────┘
                        ▼                                               ▼
        ┌──── GENERATE ─────────────────────────────┐        ┌──── TEMPLATE ENGINE ────────┐
        │ Tier 0  Rule engine (0 tokens)            │        │ xlsx / md / json / csv      │
        │ Tier 1  LLM scenarios   (per module)      │◄───────│   → TemplateSpec + mapping  │
        │ Tier 2  LLM expansion   (3–5 scen/batch)  │ custom │   (synonyms, enums from     │
        │ Validator: schema · refs · dedup · repair │ fields │    Excel data validation)   │
        └───────┬───────────────────┬───────────────┘        └──────────────┬──────────────┘
                │ Prompt Serializer │ (compact DSL, stable cacheable prefix) │
                ▼                   ▼                                        ▼
        ┌──── MODEL ADAPTER ─────────────────────┐        ┌──── EXPORTERS ──────────────┐
        │ generate(request, OutputModel)         │        │ Excel (fills a copy of the  │
        │ middleware: cache · retry · budget ·   │        │ user's template) · CSV · MD │
        │ usage ledger · JSON repair             │        │ · JSON  + Coverage &        │
        │ providers: anthropic · openai(+compat) │        │ Limitations + Trace sheets  │
        │ · gemini · fake · (litellm optional)   │        └─────────────────────────────┘
        └────────────────────────────────────────┘
   Interfaces:  CLI (typer)  ·  MCP server (stdio, job-based)  ·  local service for the extension
                — all call scoutqa.pipeline

 Capture front-ends (all feed the same App Model, tagged by source + role):
   Playwright headless crawler (CI / scheduled)  ·  Extension Record mode  ·  Extension Crawl mode
```

Each stage persists its output and is independently re-runnable (`crawl`, `distill`, `generate`, `export`).

---

## 3. Components

### 3.1 Auth Manager (`crawl/auth.py`)
Strategies, selected by `auth.type`:

| Type | Use |
|---|---|
| `none` | Public apps |
| `form` | Username/password form. Selectors auto-detected (password field → preceding text/email field → submit) or given explicitly |
| `steps` | Scripted multi-step login (SSO: email → Next → password → "Stay signed in?"), each step `fill`/`click`/`wait_for` |
| `manual` | Headed browser; the human completes MFA/CAPTCHA/SSO; ScoutQA detects the success condition and saves the session |
| `basic` | HTTP basic auth via context credentials |
| `storage_state` | Import a session file exported elsewhere |

- **Session reuse:** Playwright storage state (+ optional sessionStorage capture, which SPAs often use for
  tokens and Playwright doesn't save). Validated before each crawl by loading `check_url` and testing the
  `success` condition (URL match / selector / cookie). Expired → auto re-login (if creds) or manual handoff.
- **Mid-crawl expiry:** redirect-to-login detection → re-auth once → resume.
- **MFA/CAPTCHA detection** (reCAPTCHA/hCaptcha/Turnstile frames, `autocomplete=one-time-code`, OTP text)
  → switch to manual handoff (never solved automatically).
- **Secrets:** `username_env`/`password_env` name env vars; optional OS keyring (Windows Credential
  Manager). Values are registered with a log-redaction filter at load time. Tracing/video is disabled
  during login (Playwright traces would capture typed passwords). Auth-only domains (e.g.
  `login.microsoftonline.com`) are allowed during login only.
- **Storage location:** session files and the app DB live in `~/.scoutqa/projects/<name>/`
  (`%USERPROFILE%\.scoutqa\…` on Windows, overridable with `SCOUTQA_HOME`) — outside the repo and outside
  OneDrive sync. `%LOCALAPPDATA%` is avoided because packaged (MSIX) hosts redirect writes there.

### 3.2 Scope (`crawl/scope.py`)
- URL normalisation: lowercase host, drop fragments (except hash-routes `#/…`), sort query params, strip
  tracking params (`utm_*`, `gclid`, …).
- Domain allowlist (exact / `*.example.com`), include/exclude globs. Default excludes: logout/sign-out,
  `mailto:`/`tel:`/`javascript:`, downloadable files (`.pdf .zip .xlsx …`).
- **URL-pattern clustering:** numeric/UUID/hash/date segments → `{id}`; sibling-cardinality detection for
  slugs. `max_instances_per_pattern` (default 3): once 3 instances share a structure fingerprint, the rest
  are recorded as "N instances discovered" but not visited. This saves crawl time, not just tokens.
- Budgets: `max_depth`, `max_states`, `max_actions_per_state`, `max_duration`, politeness delay.

### 3.3 Safety Guard (`crawl/safety.py`)
Four independent layers — any one of them stops a mutation:

1. **Intent classifier** on accessible name / text / aria-label / id / href: `auth_exit` (logout),
   `destructive` (delete, remove, deactivate, revoke, reset…), `transactional` (pay, buy, checkout, place
   order, transfer…), `submitting` (submit, save, send, publish, approve…), `navigational`, `disclosure`
   (tabs, accordions, menus, "view/details/more"), `unknown`. Keyword lists are configurable (i18n).
2. **Structural rules:** `type=submit`, buttons inside `method=post` forms, `form.requestSubmit` handlers.
3. **Network mutation guard** (`context.route`): in read-only mode, aborts every non-GET/HEAD/OPTIONS
   request except allowlisted patterns (e.g. search endpoints; GraphQL *queries* but not *mutations*).
   Disabled only during the login step.
4. **Browser guards:** JS `confirm()` → dismissed (cancels "Are you sure?" flows), downloads cancelled,
   popups recorded then closed, `beforeunload` dismissed.

Policy: read-only mode clicks only `navigational` + `disclosure` (+ `unknown` non-submit buttons, because
layer 3 backs them up). `allow_submit` and `allow_destructive` are separate, explicit opt-ins.
Every blocked click/request is stored in `blocked_actions` → it feeds the Limitations report *and* becomes a
hint for test generation ("Delete button exists on Users list → deletion scenario, not exercised").

### 3.4 Explorer (`crawl/explorer.py`) + waits (`crawl/waits.py`)
- BFS over **states**: `state_key = hash(normalised URL, structure fingerprint)`.
- Frontier items are either URLs (from links/router links) or `(url, action path)` — action depth ≤ 2 for
  safe disclosure actions (open modal, switch tab, expand menu). State restore = reload URL + replay path,
  with elements re-found by signature (role + name + ordinal).
- Records transitions `(state A) —[element]→ (state B)` with kind `navigate | open_dialog | tab | …`.
- **Settle strategy** (not `networkidle`): `load` → in-flight fetch/XHR counter quiet for N ms → DOM
  MutationObserver quiet for N ms → spinners gone (`aria-busy`, `role=progressbar`) → bounded incremental
  scroll for lazy loading; all under one timeout.
- iframes: allowlisted frames are distilled into the parent state; third-party frames (ads, payment
  widgets) are recorded by origin only. Open shadow roots are traversed.
- Images/media/fonts blocked by default for speed (configurable). Sequential by default for deterministic
  output; `concurrency: N` available.

### 3.5 Extractor + Distiller (`distill/`)
- **Extractor:** one injected JS pass per state → `RawSnapshot` (roles, accessible names, ARIA states,
  visibility, form constraints: `required`, `type`, `min/max/step`, `minlength/maxlength`, `pattern`,
  `accept`, options; `validationMessage` via the constraint-validation API; visible alerts/toasts; locator
  candidates: test-id, unique id, name, role+name). Raw snapshots stay local.
- **Redactor:** masks emails, phones, long digit runs, JWT/token-like strings and the logged-in user's name
  before anything is stored as a PageSpec (anything stored may later reach an LLM).
- **PageSpec** (Pydantic): url, url_pattern, title, headings, forms → fields, actions, links, tables
  (columns, row actions, sort/filter/pagination flags — never row data), dialogs, messages, frames.
  Element IDs are stable per state (`p12.e4`).
- **Two fingerprints:** `structure_hash` (roles/kinds/counts; text-insensitive → template grouping) and
  `content_hash` (labels, names, constraints → change detection for incremental runs).
- **Token savers:** shared header/nav/footer extracted once as a `Layout` and referenced by id; repeated
  sibling subtrees collapsed (`12 × product card [link, button "Add to cart"]`); lists capped (options,
  links, headings) with "+N more".

### 3.6 App Model (`appmodel/`)
SQLite (stdlib `sqlite3`) behind a typed repository; JSON export for inspection (`scoutqa map --json`).

```
runs(id, started, finished, config_hash, stats)
layouts(id, signature, spec)            templates(id, structure_hash, url_pattern, representative, count)
states(id, url, url_pattern, title, structure_hash, content_hash, layout_id, template_id, spec,
       first_run, last_run, status)     elements(id, state_id, kind, role, name, risk, locators)
transitions(from_state, element_id, to_state, kind, run_id)
blocked_actions(state_id, element_id, layer, reason, method, url_path)
modules(id, name, rule)  flows(id, path)  scenarios(...)  cases(...)  llm_cache(...)  usage(...)
```
- **Incremental re-crawl:** each run marks states `new | changed | unchanged | removed` by `content_hash`.
- **Modules** derived deterministically (URL prefix + nav grouping), overridable in config.
- **Flow candidates:** bounded simple paths in the transition graph from the post-login landing state that
  pass through forms — handed to the LLM as hints instead of asking it to discover flows.

### 3.7 Generator (`generate/`)
**Tier 0 — rule engine (0 tokens).** Rule packs, each rule with an id (`R-FIELD-REQUIRED`, …):
- Fields: required-empty, type-invalid (email/url/number/tel/date), length and numeric boundaries
  (min−1/min/max/max+1), pattern mismatch, select/checkbox required, file `accept`.
- Forms: empty submit, all-valid happy path (deterministic sample data), whitespace-only input.
- Navigation (layout links), tables (sort/pagination present), dialogs (open/close), auth pack
  (valid/invalid/empty login, logout) from the auth config.
- Expected results use the *observed* validation message when captured; otherwise generic wording flagged as
  an assumption.

**Tier 1 — scenarios (per module, cheap model).** Input: module DSL + flow candidates + titles already
covered by rules (so the LLM doesn't duplicate them). Output: 5–15 scenarios with type, priority, refs.
A separate small batch builds cross-module E2E scenarios from module summaries only.

**Tier 2 — expansion (3–5 scenarios per batch).** Input: only the pages those scenarios reference + the
canonical case schema + the template's *custom* columns. Output: detailed cases.

**Validator (after every LLM call):** Pydantic schema → ref check (unknown IDs removed and the case marked
`needs_review`) → dedup vs. existing cases (normalised title + step Jaccard) → one bounded repair retry
with the error list.

**Canonical TestCase:** `id` (stable: `TC-<MODULE>-###`, persisted mapping from case fingerprint), module,
title, type, priority, preconditions[], steps[{action, target_ref, data, expected}], expected_result,
test_data, tags, `source {origin: rule|llm, generator: rule_id | model+prompt_version, refs[], evidence[],
assumptions[]}`, review_status, `extra{custom_column: value}`.

**Incremental generation:** `inputs_hash = hash(module DSL, template-schema hash, prompt version, model)`;
matching hash → reuse stored cases. Re-running after a re-crawl only spends tokens on changed modules.

### 3.8 Prompt Serializer (`generate/serialize.py`)
Compact line DSL (≈3× fewer tokens than JSON), e.g.:
```
PAGE p7 "Checkout: Your Information" /checkout-step-one layout=L1
FORM f1 "checkout" submit=e4
  e1 text "First Name" *        e2 text "Last Name" *        e3 text "Zip/Postal Code" * maxlen=10
  e4 button "Continue" submit   e5 button "Cancel" -> p6
MSG "Error: First Name is required" (observed)
```
Prompt layout is ordered for provider prompt caching: **[system rules + output schema + template columns]**
(static, cache breakpoint) → **[app glossary: modules, layout]** (per project, cache breakpoint) →
**[batch content]** (variable). Prompts are versioned files; the version is part of the cache key.

### 3.9 Template Engine (`template/`)
- Loads Excel header row (plus **data-validation dropdowns → enums**, e.g. Priority ∈ {P1,P2,P3}), Markdown
  table header or per-case block skeleton, JSON Schema, or CSV header → `TemplateSpec`.
- **Mapping** columns → canonical fields via a synonym table ("Test Steps", "Procedure" → steps;
  "Expected Outcome" → expected_result; …), overridable with a mapping file.
- **Execution-time columns** (Status, Actual Result, Executed By, Date, Defect ID) are recognised and left
  blank — never sent to the LLM. Unknown columns become custom fields (header text + optional description)
  that the LLM fills, or stay blank if marked `fill: none`.
- Layouts: `case_per_row` or `step_per_row` (detected when a "Step #" column exists).
- Default sample template: ID, Module, Scenario, Preconditions, Steps, Expected Result, Priority.

### 3.10 Exporters (`export/`)
- **Excel:** writes into a *copy of the user's template* (keeps styling, widths, dropdowns); adds sheets
  **Scenarios**, **Coverage** (states/forms/elements covered vs. not), **Limitations** (unreached/skipped
  states by reason, blocked actions, never-submitted forms → server-side rules unknown, role-gated areas,
  third-party frames, "hidden business rules are not observable by crawling"), **Trace** (case → refs,
  origin, assumptions).
- CSV (UTF-8 BOM for Excel), Markdown, JSON (lossless canonical).

### 3.11 Model Adapter (`llm/`) ✅ implemented M7
```python
class ModelClient:                          # src/scoutqa/llm/base.py — one per provider, in llm/providers/
    async def complete(self, messages: list[Message], schema: dict, *, temperature: float,
                       max_output_tokens: int) -> RawCompletion: ...

class ModelRouter:                           # src/scoutqa/llm/router.py — provider-agnostic middleware
    async def generate(self, stage: str, messages: list[Message], output: type[T]) -> Generation[T]: ...
    def estimate(self, stage, messages, output) -> dict  # --dry-run, no call
```
A provider only turns `(messages, schema)` into text — caching, retries, JSON/schema repair, budget
enforcement and usage accounting all live once in `ModelRouter`, so behaviour is identical across providers.

- **Providers implemented:** `anthropic` (tool-forced structured output; a cacheable system message gets
  an ephemeral `cache_control` breakpoint), `openai` (Chat Completions, `response_format` strict JSON
  schema) — also reaches **any OpenAI-compatible endpoint** via `base_url`: Ollama, vLLM, LM Studio, Groq,
  DeepSeek, Together, OpenRouter, Mistral, xAI, Fireworks, Perplexity, Gemini's own OpenAI-compat route,
  `azure_openai` (dedicated: `AsyncAzureOpenAI`, `azure_endpoint` + deployment + `api_version`; reuses the
  `openai` adapter's request/response logic), `gemini` (google-genai SDK, `response_json_schema` — a plain
  JSON Schema, unlike the older OpenAPI-subset `response_schema`), `fake` (in-memory, every router test;
  also a real profile provider for a zero-key pipeline smoke check — see `scoutqa models --test`).
  No LiteLLM dependency, even optionally: this process holds API keys for several providers and browser
  session cookies; LiteLLM 1.82.7/1.82.8 were compromised on PyPI in March 2026 (credential-stealing
  `.pth`). Thin adapters over official SDKs (all lazily imported — installing ScoutQA never pulls in an
  unused provider's SDK) keep the surface small and give direct control over structured-output semantics.
- **Portable schemas** (`llm/schema.py`): a Pydantic output model → the strictest common JSON-Schema
  dialect (OpenAI strict): `$defs`/`$ref` inlined, every object `additionalProperties: false` with
  `required` listing *every* key (an optional field becomes nullable rather than omitted), noisy Pydantic
  keywords stripped. `render_for_prompt()` renders the same schema as readable pseudo-JSON, embedded in the
  prompt as a fallback for providers/models with partial schema support (`json_mode: object | none`) and
  reused verbatim in repair-retry messages.
- **Middleware** (`llm/router.py`): response cache keyed by provider+model+stage+messages+schema (SQLite
  `llm_cache`, docs above); retry with exponential backoff + jitter on *transient* failures only (rate
  limit/timeout/5xx — detected from the error text, since providers don't share an exception hierarchy);
  a separate JSON/schema **repair loop** (`llm.max_repair_attempts`) that replays the bad reply and asks
  the model to correct it; a concurrency semaphore (`llm.concurrency`); **budget caps**
  (`llm.budget_usd` / `llm.budget_tokens`, checked against the `llm_usage` ledger before every call);
  `router.estimate()` for a `--dry-run` estimate with no call. Every non-cached call is recorded to
  `llm_usage` (`llm/usage.py` prices a small table of current models, overridable via `llm.pricing`).
- **Routing:** `llm.profiles` (named `ModelProfile`s: provider, model, key, temperature, ...) +
  `llm.default_profile` / `llm.routing.<stage>` — any stage name works, so M8 introduces its own
  (`scenarios`, `expand`, ...) without a router change. `ProjectConfig.llm: ModelConfig`.
- **CLI:** `scoutqa models` lists configured profiles/routing; `scoutqa models --test [--stage X]` sends
  one trivial structured-output request to check a profile actually works, at any provider including
  `fake` (0 tokens, 0 setup); `scoutqa usage` shows the ledger (tokens, cost, cache size) per stage/model.

### 3.12 Interfaces
- **CLI** (`scoutqa`): `init`, `login`, `crawl`, `map`, `generate [--rules-only]`, `template`, `export`,
  `serve`, `extension`, `models [--test]`, `usage`.
- **MCP server** (official SDK, stdio): thin wrapper over `scoutqa.pipeline`.

| Tool | Notes |
|---|---|
| `configure_model` | Validates provider/model; confirms the key env var *exists*, never reads it back |
| `set_template` | Returns detected columns + mapping for confirmation |
| `login` *(added)* | Ensure/refresh session; `manual` opens a headed browser on the user's machine |
| `crawl_app` | Starts a background job → `run_id`; progress notifications |
| `get_run_status` *(added)* | Job status/progress (long crawls would exceed client tool timeouts) |
| `get_app_map` | Compact DSL, paged by level (`summary | module | page`) |
| `generate_scenarios` / `generate_test_cases` | Support `dry_run`; return counts + usage, not full content |
| `export` | Returns file path + summary |
| `get_usage` *(added)* | Token/cost ledger |

MCP tools never accept credentials as arguments — anything passed to a tool ends up in the client LLM's
context. Tool results are summaries + file paths, because the MCP client's tokens count too.

### 3.14 Hybrid capture: browser extension + local service
The extension **captures**; the Python core **decides and generates**.

```
┌──────── Chrome / Edge ───────────────────────┐          ┌──────── User's PC ───────────────────────┐
│ ScoutQA extension (MV3, plain JS, no build)  │  HTTP    │ scoutqa serve (127.0.0.1 only)           │
│  ├ Side panel: project, role, Record, Crawl, │◄────────►│  ├ pairing token + extension-origin check│
│  │   coverage gaps, Generate, Export         │ localhost│  ├ scope / budgets / safety (crawl/)     │
│  ├ Content script: extractor.js (shared),    │          │  ├ distiller + app model (SQLite)        │
│  │   API observer (MAIN world), value shapes │          │  ├ generation + export                   │
│  └ Service worker: drives the crawl tab,     │          │  └ API keys, templates, config           │
│     DNR read-only rules, dialog override     │          └──────────────────────────────────────────┘
└──────────────────────────────────────────────┘
```

- **Record mode** — the user browses normally (their own SSO/MFA session; ScoutQA never sees credentials or
  cookies). On each settled page/dialog the content script runs `extractor.js` and posts the redacted
  summary; the clicked element is posted as a transition (real flows). It also records, per form field,
  the **value shape** only (`Aaaa`, `9999-99-99`, `AA-99999`) — never the typed value, never password fields.
- **API observation (Record mode)** — fetch/XHR are wrapped in the page's MAIN world to record, per call:
  method, endpoint pattern (`POST /api/orders/{id}`), status, **server error messages** (4xx text) and the
  **JSON shape** (keys/types, no values). Server-side rules become evidence instead of assumptions.
- **Crawl mode** — the extension opens its own unfocused window and asks the local service for the next
  URL; the service runs the *same* `crawl/frontier.py` (scope, budgets, unsafe links, pattern sampling) as
  the Playwright explorer and chooses which in-page controls are safe to click (clicks it did not choose are
  refused). That tab gets `declarativeNetRequest` session rules (scoped by `tabIds`) blocking
  POST/PUT/PATCH/DELETE for every resource type including `main_frame` (form posts), except allowlisted
  patterns; a page-world guard answers `confirm()` with Cancel and refuses popups, keyed on a per-tab
  sessionStorage flag so the user's other tabs are untouched. Page-world scripts buffer reports until the
  content script is listening (dialogs fire while the HTML is still loading). Session loss (redirect to the
  sign-in page) stops the crawl as `session_lost`. Limits: iframes are not crawled in this mode; GraphQL
  POSTs are blocked (no request-body inspection in DNR).
- **Coverage view** — the side panel shows pages/forms/roles without cases and suggests what to record next.
- **Security** — the service binds to 127.0.0.1, requires the pairing token (stored hashed, bound to the
  extension's origin), refuses any web-page `Origin` and any `Host` other than 127.0.0.1/localhost (blocks
  CSRF and DNS rebinding). Raw extractor snapshots go only to 127.0.0.1 and are never stored — the service
  distils and redacts them with the crawler's code. Value shapes are validated server-side (only `A a 9`
  and punctuation are accepted, so a real value is refused); nothing typed on the sign-in page is kept.
  Permissions: host access requested per project (its own domains, its scheme), `sidePanel`, `storage`,
  `scripting`, `tabs`, `declarativeNetRequest` (Crawl mode) — no `debugger`, no `cookies`.
- **Packaging** — the extension is plain JavaScript (`// @ts-check`, no build step) inside the Python
  package (`src/scoutqa/extension/`), so `pip install` ships it and `scoutqa extension` prints the folder to
  load. `extractor.js` lives there once and is read by the Playwright crawler too.
- **Tests** — Playwright loads the unpacked extension against the fixture app; the same "no mutating request
  reached the server" invariant applies.

### 3.15 Roles and permission matrix
A project can define several login **profiles** (e.g. `admin`, `viewer`); each has its own credentials env
vars and saved session. Every state, element and transition is stored with its role. Comparing roles
yields a deterministic permission matrix ("viewer: no Delete on Items; no access to /admin/users") → access
control test cases with zero tokens (rule pack in M3).

### 3.16 Context pack and review loop
- **Context pack (optional, M8):** user stories, requirement docs, OpenAPI specs, existing test cases.
  Distilled once; only the parts relevant to a module go into its prompt. Fills requirement-ID columns,
  grounds expected results, and avoids duplicating existing cases.
- **Review loop:** reviewers accept / edit / reject cases (side panel or CLI) before export. Rejections are
  stored and excluded on regeneration; accepted edits are kept across re-runs.

### 3.13 Cross-cutting
- `config/`: Pydantic models; layering defaults → `scoutqa.yaml` → `SCOUTQA_*` env → CLI/MCP overrides.
- `logging`: console + JSONL per run; secret-redaction filter (registered values, `Authorization`, cookies,
  tokens); request bodies never logged.
- Typed exceptions (`AuthError`, `ScopeError`, `BudgetExceeded`, `ProviderError`); stable ordering
  everywhere so outputs are reproducible and diffable.

---

## 4. Known risks & mitigations

| Risk | Mitigation |
|---|---|
| Only reachable UI is seen; hidden business rules missed | Record mode (human-guided flows) + API observation of server errors; context pack; Limitations sheet; assumptions marked |
| Extension runs in the user's real session | Crawl tab gets DNR read-only rules; Record mode is user-driven; only redacted summaries leave the page |
| Hallucinated behaviour | Reference-based output, ref validation, evidence vs. assumption, `needs_review`, Trace sheet |
| Accidental mutations on a real app | 4-layer Safety Guard; fixture tests assert no mutating request ever reaches the server |
| MFA / CAPTCHA / SSO | `steps` + `manual` strategies, detection → handoff |
| SPAs, lazy loading, iframes, shadow DOM | State-based explorer, settle strategy, frame + shadow traversal |
| Token blow-up on big apps | Pattern budgets, template/layout/list collapsing, DSL, caching, budgets, dry-run |
| Session cookies leaking | Stored outside repo/OneDrive, gitignored, never in exports or MCP output |

---

## 5. Stack
Python 3.12 · Playwright (async, Chromium) · Pydantic v2 · stdlib sqlite3 · Typer · PyYAML · openpyxl ·
`mcp` SDK · optional extras: `anthropic`, `openai`, `google-genai`, `keyring`, `litellm` ·
dev: pytest, pytest-asyncio, ruff, mypy.

---

## 6. Folder structure

```
ScoutQA/
├── pyproject.toml            # src layout, extras: [anthropic, openai, gemini, all, dev]
├── README.md
├── scoutqa.example.yaml
├── docs/ARCHITECTURE.md
├── src/scoutqa/
│   ├── config/        models.py · loader.py · secrets.py
│   ├── log.py         workspace.py      errors.py
│   ├── crawl/         browser.py · auth.py · scope.py · safety.py · explorer.py · waits.py
│   ├── distill/       extractor.js · extract.py · spec.py · redact.py · fingerprint.py · layout.py
│   ├── appmodel/      db.py · schema.sql · repo.py · graph.py
│   ├── generate/      cases.py · rules/{fields,forms,navigation,tables,auth}.py
│   │                  serialize.py · prompts/ · scenarios.py · expand.py · validate.py · planner.py
│   ├── llm/           base.py · schema.py · router.py · cache.py · usage.py ·
│   │                  providers/{anthropic,openai,azure_openai,gemini,fake}.py
│   ├── template/      loader.py · mapping.py · default.py
│   ├── export/        excel.py · csv.py · markdown.py · jsonx.py · report.py
│   ├── service/       app.py · pairing.py  # local HTTP service for the extension (M5)
│   ├── pipeline.py    # library API used by CLI, MCP and the local service
│   ├── cli.py
│   └── mcp_server.py
├── extension/         # MV3 extension (TypeScript): manifest, side panel, content script, worker (M5–M6)
└── tests/
    ├── fixture_app/   # local web app: login, SPA routes, modal, iframe, lazy list, template pages,
    │                  # Delete/Pay buttons, logout, mutating API — and a request log for safety asserts
    ├── unit/
    └── integration/
```

---

## 7. Milestones (reordered for an early zero-token deliverable)

| # | Milestone | Done when |
|---|---|---|
| 1 ✅ | Skeleton, config, secrets/logging, **crawler + auth + session reuse + safety guard** (link-level crawl, SPA waits) | Fixture tests: login once, reuse session, scope/budgets respected, no mutating request or logout ever hits the server |
| 2 ✅ | **Shared `extractor.js` + distiller + app model** (fingerprints, layout/template/list collapsing, redaction, state-based exploration of safe controls, incremental re-crawl, **roles/profiles**, `scoutqa map`) | Golden PageSpecs; template pages collapse; re-crawl reports deltas; two roles stored side by side |
| 3 ✅ | **Canonical case model + rule engine** (incl. permission-matrix pack) | Golden rule outputs for fixture forms and roles |
| 4 ✅ | **Template engine + exporters + coverage/limitations report** | `crawl → generate --rules-only → export` gives a filled Excel with **0 tokens** |
| 5 ✅ | **Local service + extension Record mode** (pairing, side panel, API observation, value shapes, coverage gaps) | Extension loaded in Playwright records a fixture flow into the app model |
| 6 ✅ | **Extension Crawl mode** (service-driven frontier, DNR read-only rules) | Same safety invariants as M1, via the extension |
| 7 ✅ | **Model adapter + config + routing + cache + usage/budget** (Anthropic, OpenAI + any compatible endpoint, Azure OpenAI, Gemini, fake) | 48 new tests: schema/cache/router against the fake provider + real-SDK request/response shaping (network mocked); `scoutqa models --test` for a live check |
| 8 | **LLM scenarios + expansion** (serializer, batching, validation/repair, dedup, incremental, context pack, review loop) | Recorded-response tests; dry-run estimates |
| 9 | **MCP wrapper** (jobs, progress, compact outputs) | In-memory MCP client tests for every tool |

The deterministic pipeline (M1–M4) is proven end-to-end before any tokens are spent, and the extension
(M5–M6) plugs into a pipeline that already produces output.
