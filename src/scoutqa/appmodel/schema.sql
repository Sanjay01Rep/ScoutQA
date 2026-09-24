-- ScoutQA app model, schema version 1.
-- Every observation is keyed by role (login profile) so permission differences are plain data.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    role           TEXT NOT NULL,
    source         TEXT NOT NULL,          -- playwright | extension-record | extension-crawl
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    stopped_reason TEXT,
    stats          TEXT                    -- JSON
);

CREATE TABLE IF NOT EXISTS layouts (
    id   TEXT PRIMARY KEY,
    spec TEXT NOT NULL                     -- LayoutSpec JSON
);

CREATE TABLE IF NOT EXISTS states (
    id             TEXT PRIMARY KEY,       -- hash(role, url, variant): stable across runs
    role           TEXT NOT NULL,
    url            TEXT NOT NULL,
    variant        TEXT NOT NULL DEFAULT '',  -- '' = the URL itself; else the in-page action that led here
    parent_state   TEXT,                   -- for variants: the base state
    url_pattern    TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    structure_hash TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    layout_id      TEXT,
    spec           TEXT NOT NULL,          -- PageSpec JSON (redacted, locator-free)
    depth          INTEGER NOT NULL DEFAULT 0,
    source         TEXT NOT NULL,
    first_run      TEXT NOT NULL,
    last_run       TEXT NOT NULL,
    status         TEXT NOT NULL           -- new | changed | unchanged | removed
);
CREATE INDEX IF NOT EXISTS states_role_pattern ON states (role, url_pattern, structure_hash);

CREATE TABLE IF NOT EXISTS elements (
    state_id  TEXT NOT NULL,
    ref       TEXT NOT NULL,
    kind      TEXT NOT NULL,
    role      TEXT NOT NULL,
    name      TEXT NOT NULL,
    risk      TEXT,
    signature TEXT NOT NULL,
    locators  TEXT NOT NULL,               -- JSON; never sent to an LLM
    PRIMARY KEY (state_id, ref)
);

CREATE TABLE IF NOT EXISTS transitions (
    role        TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    element_ref TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,             -- link | navigate | open_dialog | tab | expand
    to_url      TEXT NOT NULL,
    to_state    TEXT,                      -- resolved when the target was captured
    label       TEXT NOT NULL DEFAULT '',
    last_run    TEXT NOT NULL,
    PRIMARY KEY (role, from_state, element_ref, kind, to_url)
);

CREATE TABLE IF NOT EXISTS run_events (
    run_id   TEXT NOT NULL,
    role     TEXT NOT NULL,
    type     TEXT NOT NULL,                -- blocked | skipped | dialog | error
    reason   TEXT NOT NULL DEFAULT '',
    url      TEXT NOT NULL DEFAULT '',
    page_url TEXT NOT NULL DEFAULT '',
    method   TEXT,
    text     TEXT,
    trigger  TEXT                          -- in-page action that caused a blocked request
);
CREATE INDEX IF NOT EXISTS run_events_run ON run_events (run_id, type);

-- Stable case IDs: a case's deterministic key keeps its ID (TC-ITEMS-007) across regenerations.
CREATE TABLE IF NOT EXISTS case_ids (
    key    TEXT PRIMARY KEY,
    id     TEXT NOT NULL UNIQUE,
    module TEXT NOT NULL
);

-- Record mode (browser extension): API calls observed while a person used the app.
-- Shapes only (keys and types, never values); error messages of failed calls are kept, redacted.
CREATE TABLE IF NOT EXISTS api_calls (
    role           TEXT NOT NULL,
    page_pattern   TEXT NOT NULL,
    method         TEXT NOT NULL,
    endpoint       TEXT NOT NULL,          -- URL pattern, e.g. /api/items/{id}
    status         INTEGER NOT NULL,
    count          INTEGER NOT NULL DEFAULT 1,
    messages       TEXT NOT NULL DEFAULT '[]',
    request_shape  TEXT,
    response_shape TEXT,
    trigger        TEXT,                   -- label of the click that preceded the call
    last_run       TEXT NOT NULL,
    PRIMARY KEY (role, page_pattern, method, endpoint, status)
);

-- Record mode: anonymised shape of values typed into fields ('Aa-9999'), never the values themselves.
CREATE TABLE IF NOT EXISTS field_shapes (
    role        TEXT NOT NULL,
    url_pattern TEXT NOT NULL,
    field_label TEXT NOT NULL,
    field_kind  TEXT NOT NULL,
    shape       TEXT NOT NULL,
    length      INTEGER NOT NULL,
    count       INTEGER NOT NULL DEFAULT 1,
    last_run    TEXT NOT NULL,
    PRIMARY KEY (role, url_pattern, field_label, shape)
);

CREATE TABLE IF NOT EXISTS cases (
    key        TEXT PRIMARY KEY,
    id         TEXT NOT NULL,
    module     TEXT NOT NULL,
    origin     TEXT NOT NULL,              -- rule | llm
    spec       TEXT NOT NULL,              -- TestCase JSON
    updated_at TEXT NOT NULL
);

-- LLM response cache (M7): keyed by a hash of provider + model + messages + output schema, so an
-- unchanged prompt costs 0 tokens on a re-run. Never stores prompts/keys in plain sight beyond what the
-- provider itself received; values are the raw JSON text the model returned.
CREATE TABLE IF NOT EXISTS llm_cache (
    key        TEXT PRIMARY KEY,
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    stage      TEXT NOT NULL,
    text       TEXT NOT NULL,              -- raw model output (validated JSON text)
    usage      TEXT NOT NULL,              -- JSON: {input_tokens, output_tokens, cached_input_tokens}
    created_at TEXT NOT NULL
);

-- LLM usage ledger (M7): one row per call that actually reached a provider (cache hits are not billed
-- and are not recorded here). Backs `scoutqa usage` and the run-level budget check.
CREATE TABLE IF NOT EXISTS llm_usage (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT,
    stage              TEXT NOT NULL,
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL,
    latency_s          REAL NOT NULL,
    attempts           INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS llm_usage_stage ON llm_usage (stage, created_at);
