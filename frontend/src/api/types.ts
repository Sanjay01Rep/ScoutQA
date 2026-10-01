// Mirrors the JSON shapes returned by src/scoutqa/webui/routes.py — keep these two in sync by hand,
// there's no shared schema generation (yet) between the Python backend and this frontend.

export interface ProjectInfo {
  project: string
  base_url: string
  auth_type: string
  roles: string[]
}

export interface LoginResult {
  role: string
  outcome: 'none' | 'reused' | 'fresh_login'
  session_saved: boolean
}

export interface JobStarted {
  job_id: string
  status: 'started'
}

export interface JobProgress {
  pages_done: number
  frontier_size: number
  current_url: string
}

export interface CrawlResult {
  run_id: string
  role: string
  stopped_reason: string
  stats: Record<string, number>
  states: Record<string, number>
}

export interface JobSnapshot {
  job_id: string
  kind: string
  status: 'running' | 'completed' | 'failed'
  progress: JobProgress
  started_at: string
  finished_at: string | null
  result?: CrawlResult
  error?: string
}

export interface MapSummary {
  role: string
  states: number
  modules: Record<string, number>
}

export interface MapText {
  role: string
  module?: string
  state_id?: string
  text: string
  approx_tokens: number
}

export interface GenerateResult {
  dry_run?: false
  total_cases: number
  rule_cases: number
  llm_cases: number
  by_module: Record<string, number>
  by_type: Record<string, number>
  by_priority: Record<string, number>
  needs_review: number
  llm_calls: number
  llm_cached_calls: number
  cases_path: string
}

export interface DryRunEstimate {
  dry_run: true
  by_module_tokens: Record<string, number>
  approx_input_tokens: number
  approx_cost_usd: number | null
}

export interface PendingCase {
  id: string
  title: string
  priority: string
  module: string
  needs_review: boolean
}

export interface ReviewList {
  pending_count: number
  pending: PendingCase[]
}

export type ReviewAction = { id: string; status: string } | { id: string; error: string }

export interface ReviewApplyResult {
  actions: ReviewAction[]
}

export interface ExportResult {
  path: string
  name: string
  format: string
  template: string
  cases: number
  rows: number
  needs_review: number
  custom_columns: string[]
}

export interface ModelsList {
  profiles: Record<string, { provider: string; model: string }>
  default_profile: string | null
  routing: Record<string, string>
}

export interface TestModelResult {
  stage: string
  profile: string
  provider: string
  model: string
  reply: string
  input_tokens: number
  output_tokens: number
  cost_usd: number | null
  cached: boolean
}

export interface ConfigureModelResult {
  valid: boolean
  profile_name: string
  provider: string
  model: string
  env_var_set: boolean | null
  yaml_snippet: string
  note: string
}

export interface TemplateColumn {
  column: string
  field: string
  why: string
}

export interface TemplatePreview {
  template: string
  layout: string
  columns: TemplateColumn[]
  custom_columns: string[]
  note: string
  yaml_snippet?: string
}

export interface UsageRow {
  stage: string
  provider: string
  model: string
  calls: number
  input_tokens: number
  output_tokens: number
  cached_input_tokens: number
  cost_usd: number | null
}

export interface UsageReport {
  rows: UsageRow[]
  total_input_tokens: number
  total_output_tokens: number
  total_cost_usd: number | null
  cache_entries: number
}

export interface ApiErrorBody {
  error: string
}

// ---------------------------------------------------------------- project setup (config file)

export interface AuthConfigView {
  type: string
  login_url: string | null
  username_env: string | null
  password_env: string | null
}

export interface ScopeConfigView {
  max_pages: number
  max_depth: number
}

export interface TemplateConfigView {
  path: string | null
}

export interface ConfigView {
  project: string
  base_url: string
  auth: AuthConfigView
  scope: ScopeConfigView
  template: TemplateConfigView
}

export interface GetConfigResult {
  exists: boolean
  path: string
  config: ConfigView | null
}

export interface SaveConfigResult {
  path: string
  project: string
  base_url: string
  note: string
}
