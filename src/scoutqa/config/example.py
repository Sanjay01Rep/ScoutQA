"""The commented `scoutqa.yaml` written by `scoutqa init` (also shipped as scoutqa.example.yaml)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_TEMPLATE = """\
# ScoutQA project configuration. Credentials are referenced by env-var NAME only.
project: {project}
base_url: {base_url}

scope:
  allowed_domains: []          # empty = only the base_url host; supports "*.example.com"
  include: []                  # URL globs; if set, a URL must match one
  exclude: []                  # URL globs never visited, e.g. "*/admin/*"
  start_urls: []               # extra entry points
  max_depth: 3
  max_pages: 50
  max_instances_per_pattern: 3 # /items/1, /items/2, /items/3 ... then stop
  max_duration_s: 600
  politeness_delay_ms: 0
  explore_actions: true        # click safe tabs / menus / dialog openers to find in-page states
  max_actions_per_state: 8

auth:
  type: none                   # none | form
  # login_url: {site_root}login
  # username_env: {env_prefix}_USERNAME
  # password_env: {env_prefix}_PASSWORD
  # profiles:                        # several roles instead of one user (crawl with --role / --all-roles)
  #   admin:  {{username_env: {env_prefix}_ADMIN_USER,  password_env: {env_prefix}_ADMIN_PASS}}
  #   viewer: {{username_env: {env_prefix}_VIEWER_USER, password_env: {env_prefix}_VIEWER_PASS}}
  # username_selector: "#email"      # optional; auto-detected when omitted
  # password_selector: "#password"
  # submit_selector: "button[type=submit]"
  # success:                         # optional; default = no visible password field after login
  #   url_regex: "/dashboard"
  #   selector: "[data-testid=user-menu]"
  # check_url: {site_root}dashboard   # protected page used to validate a saved session
  session_max_age_hours: 12

safety:
  read_only: true              # block every non-GET request (except during login)
  allow_mutation_patterns: []  # URL globs allowed to receive POST/PUT/PATCH/DELETE, e.g. "*/api/search*"
  allow_graphql_queries: true
  extra_unsafe_keywords: []    # e.g. ["loeschen", "supprimer"]
  allow_link_patterns: []      # follow these even if they sound unsafe, e.g. "*/archive"

generation:
  rule_packs: [fields, forms, actions, navigation, tables, dialogs, auth, access, smoke, api]
  modules: {{}}                # e.g. {{Checkout: ["/cart*", "/checkout*"]}}; default = first path segment
  use_llm: false               # also run LLM scenarios + expansion (needs llm.profiles below); overridden
                               # per run by `scoutqa generate --rules-only` / `--no-rules-only`
  scenarios_per_module: 10     # scenario ideas requested per module
  cross_module_scenarios: 5    # end-to-end scenarios spanning more than one module (0 to skip)
  cases_per_batch: 4           # scenarios expanded into detailed cases per LLM call
  context_pack: []             # paths (.md/.txt/.json) to requirements, user stories, an OpenAPI spec, or
                               # an existing test-case export — matched to modules by keyword, 0 tokens

template:
  path: null                   # your .xlsx / .csv / .md / .json template; null = ID, Module, Scenario,
                               # Preconditions, Steps, Expected Result, Priority
  sheet: null                  # Excel sheet with the header row (default: first sheet)
  header_row: null             # 1-based; default: auto-detect
  layout: auto                 # auto | case_per_row | step_per_row ("Step No" column => one row per step)
  columns: {{}}                # override mapping, e.g. {{"Objective": title, "Owner": blank}}
  defaults: {{}}               # fixed values, e.g. {{"Environment": "QA", "Release": "R12"}}

browser:
  headless: true
  block_resources: [image, media, font]
  navigation_timeout_ms: 30000
  settle:
    quiet_ms: 500
    timeout_ms: 10000
    lazy_scroll_steps: 5

# LLM setup (M7+): one entry per model you can call, then say which stage uses which.
# Only the provider(s) you actually reference need their package installed:
#   pip install scoutqa[anthropic]   pip install scoutqa[openai]   pip install scoutqa[gemini]
# 'openai' also reaches any OpenAI-compatible endpoint via base_url: Ollama, vLLM, LM Studio, Groq,
# DeepSeek, Together, OpenRouter, Mistral, xAI, ... (no api_key_env needed for a keyless local server).
llm:
  profiles: {{}}
  # profiles:
  #   claude:  {{provider: anthropic, model: claude-sonnet-5, api_key_env: ANTHROPIC_API_KEY}}
  #   gpt:     {{provider: openai, model: gpt-5.1, api_key_env: OPENAI_API_KEY}}
  #   gemini:  {{provider: gemini, model: gemini-3-flash, api_key_env: GEMINI_API_KEY}}
  #   local:   {{provider: openai, model: llama3.1, base_url: "http://localhost:11434/v1"}}  # Ollama
  #   azure:   {{provider: azure_openai, model: my-gpt-deployment, api_key_env: AZURE_OPENAI_KEY,
  #             azure_endpoint: "https://my-resource.openai.azure.com"}}
  default_profile: null        # profile used by any stage not listed in routing
  routing: {{}}                 # e.g. {{scenarios: gemini, expand: claude}} — cheap model for scenarios,
                               # a stronger one for detailed cases
  cache: true                  # reuse an identical previous call (0 tokens)
  max_retries: 3               # on a transient failure: rate limit, timeout, 5xx
  max_repair_attempts: 2       # extra tries after invalid JSON / schema mismatch
  concurrency: 4
  budget_usd: null             # stop a run once estimated spend reaches this
  budget_tokens: null          # stop a run once input+output tokens reach this
  pricing: {{}}                 # override/add $ per 1M tokens: {{"openai:gpt-5.1": [1.25, 0.125, 10.0]}}
"""


def example_yaml(project: str = "myapp", base_url: str = "https://myapp.example.com/") -> str:
    parts = urlsplit(base_url)
    site_root = f"{parts.scheme}://{parts.netloc}/" if parts.scheme and parts.netloc else base_url
    env_prefix = re.sub(r"[^A-Za-z0-9]+", "_", project).strip("_").upper() or "APP"
    return _TEMPLATE.format(project=project, base_url=base_url, site_root=site_root, env_prefix=env_prefix)
