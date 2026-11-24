// GENERATED FILE -- DO NOT EDIT BY HAND.
//
// Generated from swarm-api's OpenAPI document (`create_app().openapi()`, the
// document the service serves at /openapi.json) by scripts/gen_api_types.py.
// To change a type, change the API's model and regenerate, from the
// repository root:
//
//   uv run python scripts/gen_api_types.py
//
// An offline drift test fails while this file differs from what that
// command writes.

/** One subscription account, as the broker serves it and swarm-api rebuilds it. */
export interface Account {
  account_id: string
  owner_tenant: string
  label: string
  display_name?: string | null
  provider: string
  state: string
  reason: string
  lend_to: string[]
  assigned: number
  windows: { [key: string]: AccountWindow }
  observed_at: string | null
  stale: boolean
  unreadable_by: string[]
  /**
   * The tenants the pool is skipping this account for NOW (the verdict, with the
   * server's clock); `unreadable_by` is only the record, with no ages.
   */
  unreadable_now: string[]
  last_assigned_at: string | null
  vendor?: string | null
  kind?: string | null
  tier?: string | null
  /** Per borrowing tenant: its caps. */
  lends?: { [key: string]: unknown } | null
  per_workspace_max?: { [key: string]: unknown } | null
  monthly_spend_cap_usd?: number | null
  month_spend_usd?: number | null
  lend_spend_usd?: { [key: string]: unknown } | null
  exhausted_at?: string | null
  voice_dedicated?: boolean | null
  model_classes?: string[] | null
  organisation?: unknown
  duplicate_of?: string | null
  duplicate_reason?: string | null
  duplicate_matched_on?: unknown
  possible_duplicate?: unknown
  max_agents?: number | null
  pools?: string[] | null
  voice_it_people?: unknown[] | null
  voice_it_holder?: unknown
  can_take_new_agent?: boolean | null
  blocked_by?: unknown[] | null
  next_eligible_at?: string | null
  latest_hold_deadline?: string | null
  measured?: boolean | null
  last_refresh_at?: string | null
  token_expires_at?: string | null
  last_refresh_reason?: string | null
  /** Health events by kind, last 24 h. */
  health_24h?: { [key: string]: number } | null
  health_status?: string | null
}

/**
 * Register a Claude subscription or a ChatGPT plan into the CALLER's pool.
 *
 * A rotating OAuth pair, and nothing else: see `ACCOUNT_PROVIDERS` above.
 * There is no `api_key` field here and no kind selector beyond `provider`,
 * because every member of the pool is a pair the broker refreshes.
 *
 * THE OWNER IS THE TENANT ON THE VERIFIED TOKEN, always. `owner_tenant` below
 * is read for one purpose only -- to be compared against it -- and its value
 * is never what the account is filed under, so there is no ordering of
 * validation in which a body field could put a live credential into somebody
 * else's pool.
 *
 * It is accepted at all because the broker's own route takes that field (this
 * API mirrors the broker's contract) and because the refusal can then explain
 * itself: a caller naming another tenant is told the rule, rather than getting
 * a bare `extra_forbidden` from `extra="forbid"` and guessing.
 */
export interface AccountCreate {
  owner_tenant?: string | null
  label: string
  provider?: string
  lend_to?: string[]
  credential: string
}

/** Ask whether the person approved it, by the state start answered. */
export interface AccountDeviceSignInPoll {
  state: string
}

/**
 * Begin adding a ChatGPT plan with a device code. No provider: only a plan
 * signs in this way. No owner: the caller's workspace is the owner.
 */
export interface AccountDeviceSignInStart {
  label: string
  lend_to?: string[]
}

/**
 * Who may borrow an account: `lends`, each borrower with its cap, or today's
 * bare `lend_to` list, which the broker still reads for a plan (a borrower
 * already lent keeps its cap; a new one is carried over, marked `cap not
 * set`). Neither: lent to nobody, as `{}` always meant. Declared here, beside
 * the one route that takes it, like `AccountModelClasses`.
 */
export interface AccountLending {
  lend_to?: string[] | null
  lends?: { [key: string]: LendCap } | null
}

/**
 * How many agents an account may hold at once (archived issue 2222), 1 to
 * MAX_AGENTS_LIMIT.
 *
 * Declared here, beside the one route that takes it, like
 * `AccountModelClasses`. Required, so an empty body is not read as the
 * default. `StrictInt`, so `true`, `"6"` and `6.5` are refused as not a number
 * rather than coerced into one; the range is `_checked_max_agents`.
 */
export interface AccountMaxAgents {
  max_agents: number
}

/**
 * The model classes an account serves (owner decision R5). Empty: every class.
 *
 * Declared here rather than in `schemas.py`, beside the one route that takes
 * it. The names are not checked here: the broker owns the account record and
 * answers an unknown class with a 422 naming the vocabulary, as it does an
 * unknown state.
 */
export interface AccountModelClasses {
  model_classes?: string[]
}

/**
 * An account's Name (archived issue 1913), or null to clear it so the label
 * shows.
 *
 * Declared here, beside the one route that takes it, like
 * `AccountModelClasses`. Required, so an empty body is not read as "clear".
 * The bound only stops an unbounded body: what a Name may be is the broker's
 * `validate_display_name`, and its 422 reaches the caller in its own words.
 */
export interface AccountName {
  display_name: string | null
}

/**
 * Each workspace's cap on the agents it holds on an account (Q6), the owner's
 * and each borrower's. REQUIRED, so an empty body is refused rather than read
 * as "no caps"; `{}` clears every cap. 51: the owner and the broker's 50
 * lends. Each number is taken as sent and refused by `_max_agents_words` in
 * the broker's words, not coerced.
 */
export interface AccountPerWorkspaceMax {
  per_workspace_max: { [key: string]: unknown }
}

/**
 * Which pools an account is in (the Voice it pool, owner 2026-11-02), and the
 * account's label typed back. What a pool name may be, and whether the label
 * matches, is the broker's (`PUT /v1/accounts/{id}/pools`): its 400 reaches
 * the caller in its own words. The bounds only stop an unbounded body.
 */
export interface AccountPools {
  pools: string[]
  confirm_label: string
}

/**
 * Finish it, with what the callback page showed.
 *
 * No `provider` field: the broker's exchange takes none. The PENDING sign-in
 * the broker keyed by `state` records which provider started it, so the finish
 * cannot disagree with the start.
 */
export interface AccountSignInFinish {
  state: string
  code: string
}

/**
 * Begin adding an account by signing in to its provider in a browser.
 *
 * No credential here, which is the whole point: the person signs in with
 * Anthropic or OpenAI and we never see anything but the code the provider
 * hands back afterwards.
 *
 * `provider` defaults to `anthropic`, so a Claude sign-in sends nothing new
 * and behaves exactly as it did before ChatGPT plans existed. It is forwarded
 * to the broker unchanged; the broker picks the authorize page and the
 * redirect from it and echoes both in its answer.
 */
export interface AccountSignInStart {
  label: string
  provider?: 'anthropic' | 'openai'
  lend_to?: string[]
}

/**
 * An API key's monthly spend cap in US dollars, or null for none (§4.6).
 *
 * Declared here, beside the one route that takes it. Required, so an empty
 * body is not read as "clear". What a cap may be (above 0, the ceiling) and
 * that a plan has none are the broker's: its 422 reaches the caller.
 */
export interface AccountSpendCap {
  monthly_spend_cap_usd: number | null
}

export interface AccountStateChange {
  state: string
  reason?: string
}

export interface AccountWindow {
  utilization: number
  resets_at: string
  reset: boolean
}

/** `GET /v1/accounts`: every account this tenant may run on. */
export interface AccountsPage {
  accounts: Account[]
  tenant_id: string | null
  unreadable_documents: string[]
  /**
   * How many account documents the broker could not read; the ids are named only
   * to their owner.
   */
  unreadable_document_count: number
  refresh_sweep?: RefreshSweep | null
}

export interface ActionsLinkRequest {
  name: string
  environment?: string | null
}

export interface ActionsSecretDelete {
  confirm_name: string
  environment?: string | null
}

export interface ActionsSecretWrite {
  value: string
  environment?: string | null
  confirm_environment?: string | null
  link_token?: string | null
}

/**
 * `PUT /v1/admin/quota/{provider}/{tenant_id}/adaptive-target` (QUOTA-RESET).
 *
 * The broker's own body, minus `requested_by`: that is the admin's verified
 * email, taken from the token, never from the request.
 */
export interface AdaptiveTargetRequest {
  target: number
  reason: string
  clear_cooldown?: boolean
}

export interface AddRounds {
  rounds?: number
  note?: string | null
}

/**
 * One repository a step reads beside its own (docs/design/ multi-repository-
 * steps.md §4.1, archived issue 1434): a NAME the tenant registered, never a
 * URL, a slot or a commit. swarm-api resolves the rest -- the registration,
 * whose slot clones it, the commit its ref names now, its size -- and stores
 * that, signed (`extrarepos`); nothing here reaches the task as sent
 * (invariant 10).
 */
export interface AdditionalRepositoryCreate {
  repository: string
  ref?: string | null
  name?: string | null
}

/**
 * One pull request a workflow waits for before its steps start (SPEED-D3).
 *
 * A NUMBER, never a repository: the pull request is the workflow's own
 * repository's (`prgate.resolve_after`), so a spec cannot point the tenant's
 * token at another one.
 */
export interface AfterPullRequest {
  pr: number
}

export interface Answer {
  text: string
}

/**
 * `POST /v1/tasks/{id}/answers`: an option label, free text, or both.
 *
 * `via` and `session` say where it was answered (ANSWER-SYNC); neither changes
 * who may answer.
 */
export interface AnswerBody {
  question_index: number
  choice?: string | null
  text?: string | null
  via?: 'console' | 'claude-code' | 'github-comment' | 'voice-it' | null
  session?: string | null
}

export interface ApplyBody {
  version_id: string
  diff_sha256: string
  confirm_name?: string | null
}

export interface Approval {
  frame: string
  session_id?: string | null
}

export interface AskBody {
  code: string
  message?: string | null
}

/** `GET /v1/tasks/{task_id}/attempts`. */
export interface AttemptPage {
  task_id: string
  read_at: string
  attempts: AttemptRow[]
  /** The subscription account each attempt ran on, keyed by attempt id. */
  accounts_by_attempt: { [key: string]: unknown }
}

/** `attempt_to_api` in codec.py: one attempt, with its five spend fields. */
export interface AttemptRow {
  attempt_id: string
  task_id: string
  tenant_id: string
  generation: number
  lease_id: string
  backend: string
  execution_name: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  exit_code: number | null
  error: string | null
  error_redaction_count?: number | null
  peak_rss_bytes: number | null
  peak_disk_bytes: number | null
  oom_near_miss: boolean
  checkpoints: string[]
  input_tokens: number | null
  output_tokens: number | null
  cache_read_input_tokens: number | null
  cache_creation_input_tokens: number | null
  cost_usd: number | null
  cpu_seconds?: number | null
  peak_cpu_cores?: number | null
  mean_cpu_cores?: number | null
  cpu_limit_cores?: number | null
  cpu_measured_at?: string | null
  cpu_limit_source?: string | null
  cpu_reading_age_seconds?: number | null
  model?: string | null
  size_version?: number | null
  sized_cpu_request?: number | null
  sized_cpu_limit?: number | null
  sized_memory_gib?: number | null
  sized_disk_gib?: number | null
  sized_live_split?: unknown
  account_id?: string | null
  credential_source?: string | null
  credential_tier?: string | null
}

export interface AuthBody {
  mode: 'none' | 'dispatch' | 'workspace_secret'
  header?: string | null
  scheme?: 'Bearer' | '' | null
}

export interface AuthorizeBody {
  surface?: 'console' | 'plugin'
}

export interface BindingBody {
  provider: string
}

export interface BriefWrite {
  text: string
}

/** A bucket's state; when `unread`, every count on it is null. */
export type BucketState = 'sealed' | 'open' | 'unread'

/**
 * `agent_budget_ceiling` (archived issue 2459): the most any workspace default
 * or step budget may name, per unit. A unit left out has no ceiling, so `{}`
 * clears it.
 */
export interface BudgetCeilingRequest {
  usd?: number | null
  tokens?: number | null
}

/** `PUT /v1/workspaces/{tenant_id}/budget`: the default, or null to clear it. */
export interface BudgetSettingsPut {
  budget: { [key: string]: unknown } | null
}

export interface BypassWrite {
  value: string
  environment?: string | null
  link_token?: string | null
}

/** Why a task was cancelled (`swarm_api.outcomes.CANCEL_CAUSES`). */
export type CancelCauseKey = 'requested' | 'after_failure' | 'after_cancel' | 'workflow_sweep' | 'child_cascade' | 'other'

export interface CancelCauseLabel {
  key: CancelCauseKey
  label: string
}

/** Cancels by cause; none of them is a failure. */
export interface CancelSplit {
  total: number
  requested: number
  after_failure: number
  /**
   * A cascade cancel that followed a cancel somebody asked for, at its parent or
   * further up the chain -- not a failure (archived issue 185, decision 2).
   */
  after_cancel: number
  workflow_sweep: number
  /**
   * Contract request 41: a child task cancelled because its parent was
   * cancelled, ended, or out-waited its await. OPTIONAL: an API before
   * DERIVE_VERSION 6 does not send it, and absent is not zero.
   */
  child_cascade?: number | null
  other: number
}

/**
 * `GET /v1/capacity`: the caller's pools and what each runner profile can
 * admit.
 */
export interface Capacity {
  pools: Pool[]
  runner_profiles: { [key: string]: RunnerProfile }
  tenant_id?: string | null
  pools_complete?: boolean | null
  blocked_reason_groups?: { [key: string]: string[] } | null
  generated_at: string
}

export interface CeilingRequest {
  max_active: number
}

export interface CodeBody {
  code: string
}

/**
 * PUT /v1/workspaces/{id}/code-intelligence. Every field optional; a field
 * left out is unchanged, `inherit` clears, and a body that names nothing is
 * refused.
 */
export interface CodeIntelligencePut {
  jev?: boolean | null
  context?: 'on' | 'off' | 'inherit' | null
  context_ab?: 'ab' | 'on' | 'off' | 'inherit' | null
  graph_delivery?: 'staged' | 'service' | 'inherit' | null
}

export interface ContractReviewPut {
  enabled: boolean
}

export interface Conversion {
  code: string
  state: string
}

export interface CostByOutcome {
  succeeded: CostCell
  failed: CostCell
  dead_lettered: CostCell
  cancelled: CostCell
}

/** Reported cost over some attempts. */
export interface CostCell {
  /** null when no attempt reported a cost: an absence, never $0.00. */
  sum_usd: number | null
  attempts: number
  reporting: number
}

/**
 * What one relaxed ceiling would buy a runner profile
 * (`ProfileAdmission.counterfactual`).
 */
export interface Counterfactual {
  pool: string
  action: string
  headroom_after: number | null
  basis_after: string
  delta: number | null
  next_binding: string[]
}

/** TENANT-DAYS ON THE UTC CALENDAR, the rollup's own unit. */
export interface CoverageDays {
  total: number
  sealed: number
  live: number
  unread: number
}

export interface CredentialWrite {
  username: string
  password: string
  link_token: string
  environment?: string | null
}

export interface DeclaredCost {
  /** null when no attempt reported a cost: an absence, never $0.00. */
  sum_usd: number | null
  attempts: number
  reporting: number
  profiles: string[]
}

export interface DefaultRole {
  reason?: string | null
  via?: 'console' | 'cli'
  role: string
}

export interface DefaultsBody {
  defaults: string[]
}

export interface DeleteProject {
  confirm?: string | null
}

export interface DenyBody {
  reason?: string | null
}

export interface DenyRequest {
  reason: string
}

export interface DismissBody {
  dismissed?: boolean
}

/** `GET /v1/admin/dispatch`: the platform dispatch switch. */
export interface DispatchControl {
  dispatch_paused: boolean
  updated_at: string | null
  updated_by: string | null
  reason: string | null
  /** running, paused or unknown (U26). */
  dispatch_state?: string | null
  /** present or missing (U26). */
  control_document?: string | null
}

export interface DrainRequest {
  drain?: boolean
  reason?: string | null
}

export interface EditNote {
  text?: string | null
  state?: 'open' | 'resolved' | null
  session_id?: string | null
}

export interface EditPreferences {
  changes: { [key: string]: unknown }
  revision?: number | null
}

export interface EnableOrgBody {
  owner: string
}

export interface EndLoop {
  outcome: 'stop'
}

/**
 * `/end`'s optional body: the person's own cause (sessions.PERSON_END_CAUSES).
 * None, or no body, is `ended_by_person`; any other cause is 422.
 */
export interface EndSession {
  cause?: 'ended_by_person' | 'tab_closed'
}

/**
 * What the callback page posts: GitHub's `state` and `code`, or the `error`
 * GitHub sent instead of a code.
 */
export interface ExchangeBody {
  state: string
  code?: string | null
  error?: string | null
}

export interface ExitCount {
  exit_code: number | null
  label: string
  n: number
}

export interface FailingStep {
  step_id: string
  n: number
}

/** Why an ended task failed (`swarm_api.outcomes.FAILURE_CLASSES`). */
export type FailureClassKey = 'runner_error' | 'timeout' | 'lost_worker' | 'could_not_start' | 'inputs_unavailable' | 'outputs_missing' | 'dispatch_failed' | 'spec_signature_invalid' | 'verdict_refused' | 'verdict_failed' | 'merge_refused' | 'merge_failed' | 'publish_refused' | 'budget_exceeded' | 'fix_loop_exhausted' | 'other' | 'no_reason' | 'intended'

export interface FailureClassLabel {
  key: FailureClassKey
  label: string
}

export interface FirstFailed {
  /** The task's own, which the route reads as it is and does not promise. */
  step_id: string | null
  task_id: string
  failure_class: FailureClassKey
  ended_at: string
}

export interface FocusBody {
  visual_id: string
  anchor?: { [key: string]: unknown } | null
  caption?: string | null
}

/**
 * A slot, by name. `extra="forbid"`: a value in any field is a 422, and the
 * validation handler never echoes what was sent.
 */
export interface GitTokenSlot {
  scope: 'tenant' | 'repository' | 'user'
  repo_id?: string | null
  repository?: string | null
  repo_ids?: string[] | null
}

export interface GrantWrite {
  enabled: boolean
  slot?: string | null
  hosts?: string[] | null
}

/** The Timeline's group picker (`swarm_api.outcomes.GROUPS`). */
export type GroupBy = 'runner_profile' | 'submitted_by' | 'tenant_id' | 'model' | 'model_class'

export interface GroupProbe {
  group: string
  member: string
}

export interface GroupRow {
  /**
   * Grouped by person, `''` is the tasks with no submitter recorded: a row, but
   * not a name.
   */
  key: string
  /**
   * Null for the `model` group: arrivals are counted per task, before an attempt
   * has run on any model, so no model can be credited with them.
   */
  submitted: number | null
  ended: number
  succeeded: number
  failed: number
  dead_lettered: number
  cancelled: CancelSplit
  rate: Rate | null
  cost: CostCell
  declared_cost: boolean
  /**
   * Aligned 1:1 with buckets; a null entry is an unread bucket. The whole field
   * is null past 120 buckets.
   */
  series: (SeriesPoint | null)[] | null
}

export interface GroupsProbeRequest {
  groups?: GroupProbe[]
}

export interface HTTPValidationError {
  detail?: ValidationError[]
}

/**
 * `PUT /v1/admin/quota/{provider}/{tenant_id}/hard-max` (POOL-LIMITS-QUOTA).
 *
 * `reason` is the point of the change's audit entries: swarm-api's
 * `admin_audit` one, and the broker's `quota_audit` one, which it is forwarded
 * to. No `requested_by`: who asked is the admin's verified email, taken from
 * the token, and a body that names one is refused (`extra="forbid"`).
 */
export interface HardMaxRequest {
  hard_max: number
  reason: string
}

export interface HealthReport {
  run_at: string
  checks?: { [key: string]: unknown }[]
  findings?: { [key: string]: unknown }[]
}

/**
 * `POST /v1/admin/history:purge` (purge.py). A dry run unless `dry_run` is
 * false AND `confirm` is the word `purge`; `states` is checked against
 * `purge.PURGEABLE_STATES`, so nothing live can be asked for.
 */
export interface HistoryPurgeRequest {
  states: string[]
  before?: string | null
  tenant_id?: string | null
  exclude_ids?: string[]
  dry_run?: boolean
  confirm?: string | null
  limit?: number | null
  page_token?: string | null
}

/**
 * `POST .../impact`: exactly one change, and a depth. extra="forbid": a
 * caller's `paths`, `command`, `image` or `tests` is refused, not ignored.
 */
export interface ImpactRequest {
  pull_request?: number | null
  commit?: string | null
  base?: string | null
  head?: string | null
  depth?: number | null
}

/**
 * `POST .../index`. extra="forbid": a caller's `image`, `command`, `prompt`,
 * `runner_profile` or `sha` is refused, not ignored (invariant 10).
 */
export interface IndexRunRequest {
  kind?: string
}

/**
 * The schedule and trigger of repo-index.md §3.3. The index POINTER fields
 * (`current_sha`, `head_sha`, ...) are written by index runs, never a caller.
 */
export interface IndexSettings {
  interval_hours?: number | 'off' | null
  on_change?: 'poll' | 'webhook' | 'off' | null
  min_change_interval_minutes?: number | null
  full_every_days?: number | null
  keep_versions?: number | null
  measure_latency_hours?: number | 'off' | null
  paused?: boolean | null
}

export interface InstallBody {
  source: SourceBody
  overlay?: { [key: string]: unknown } | null
}

export interface InviteCreate {
  email: string
}

/**
 * `jev_token_rates` (JEV-USAGE-PAGE): Jev's price in USD per million tokens,
 * per direction. A direction left out has no rate, so `{}` clears it.
 */
export interface JevTokenRatesRequest {
  input?: number | null
  output?: number | null
}

export interface JudgmentBackendWrite {
  judgment_backend: string
}

export interface LatencyOutcome {
  n: number
  wait: Stat | null
  run: Stat | null
  total: Stat | null
}

export interface LatencyRow {
  runner_profile: string
  /** null when the row's tasks do not share one timeout_seconds. */
  timeout_s: number | null
  succeeded: LatencyOutcome
  /** Includes dead-lettered. */
  failed: LatencyOutcome
}

/**
 * One row of `GET /v1/leases`: a live lease's heartbeat, for a tenant's
 * member.
 */
export interface LeaseHeartbeat {
  task_id: string
  lease_id: string
  attempt_id: string
  generation: number
  task_state: TaskState
  dispatch_state: string
  created_at: string
  dispatch_deadline: string
  expires_at: string
  heartbeat_at: string | null
  silent_seconds: number | null
  silent: boolean
  expired: boolean
  dispatch_overdue: boolean
}

/** `GET /v1/leases`. */
export interface LeaseHeartbeatPage {
  tenant_id: string
  read_at: string
  thresholds: LeaseThresholds
  heartbeats: LeaseHeartbeat[]
}

/** `GET /v1/admin/leases`. */
export interface LeasePage {
  leases: LeaseRow[]
  next_page_token?: string | null
  thresholds: LeaseThresholds
  evaluated_at: string
  active_only: boolean
  tenant_id: string | null
  units_held: number
  units_held_by_pool?: { [key: string]: number } | null
  active_beyond_window: number
  truncated: boolean
  examined: number
}

/**
 * `lease_to_api` in codec.py. The lease's own state is `dispatch_state`, never
 * `state`: nothing writes STARTING or RUNNING to a lease, so it is not the
 * task's state.
 */
export interface LeaseRow {
  lease_id: string
  task_id: string
  attempt_id: string
  tenant_id: string
  generation: number
  pools: string[]
  units: number
  pool_units?: { [key: string]: number } | null
  dispatch_state: string
  created_at: string
  dispatch_deadline: string
  expires_at: string
  heartbeat_at: string | null
  released_at: string | null
  release_reason: string | null
  released: boolean
  expired: boolean
  dispatch_overdue: boolean
  silent_seconds: number
  heartbeat_ever: boolean
  last_error: string | null
}

/**
 * The reconciler's own thresholds, served with the leases so no client
 * restates them.
 */
export interface LeaseThresholds {
  heartbeat_grace_seconds: number
  lease_timeout_seconds: number
}

/**
 * REQUIRED, `{}` at least, as application/json (epic 21): no body, or any body
 * a cross-site form can send (urlencoded, multipart, text/plain), is a 422
 * before anything is read, so another site cannot make a signed-in person
 * leave on their front-door cookie alone -- a page elsewhere cannot send
 * application/json without a CORS preflight swarm-api never grants.
 */
export interface LeaveBody {
  reason?: string | null
}

/** The bucket a ledger read is cut into (`swarm_api.outcomes.BUCKETS`). */
export type LedgerBucketSize = 'hour' | 'day' | 'week' | 'month'

export interface LegBody {
  environment?: string | null
}

/**
 * One lend's cap (docs/design/llm-providers.md §4.6, Q7): `max_agents` always,
 * `monthly_spend_cap_usd` on a key only. `max_agents` is taken as sent, so
 * `true`, `"2"` or `2.5` is refused by `_checked_lends` in the broker's words
 * ("is a whole number") rather than coerced or refused in pydantic's; the
 * plan/key rule on the spend cap is the broker's.
 */
export interface LendCap {
  max_agents: unknown
  monthly_spend_cap_usd?: number | null
}

export interface LimitRequest {
  limit: number
}

export interface LimitsBody {
  max_request_bytes?: number | null
  max_response_bytes?: number | null
  calls_per_minute?: number | null
  call_timeout_seconds?: number | null
}

export interface LinkCreate {
  kind: 'pool-account' | 'team-git' | 'addon-secret' | 'voice-api-key' | 'api-key'
  label: string
  owner: string
  provider?: 'anthropic-api' | 'openai-api' | null
  workspace?: string | null
  share_with_voice_it?: boolean
  replace?: boolean
}

export interface LinkRedeem {
  token: string
  value?: string | null
}

export interface LiveActionView {
  at: string
  kind: string
  method?: string | null
  url?: string | null
  length?: number | null
  event?: string | null
  cause?: string | null
  outcome?: string | null
  by?: string | null
  you?: boolean | null
  preset?: string | null
  orientation?: string | null
  touch?: boolean | null
}

/** `GET /v1/tasks/{task_id}/live/actions`: one attempt's timeline. */
export interface LiveActionsView {
  attempt_id: string | null
  actions: LiveActionView[]
  truncated: boolean
}

/**
 * WHICH BROWSER TOOL, AND THE EGRESS (BSTATUS-API; part of archived issue
 * 1030).
 *
 * `tool_selected` is the task's own `input.browser_tool` (contract request 77;
 * `auto` when it named none), served from submission on. `tools_available` is
 * what the runner registered for it on the current attempt, empty until the
 * gate has reported it; `tool_last_used` the tool whose tagged connection last
 * drove the browser, null until one has; `egress` null until the gate's first
 * report. Never invented.
 */
export interface LiveBrowserTool {
  tool_selected: 'auto' | 'playwright' | 'dev-browser' | 'jev'
  tools_available: ('playwright' | 'dev-browser' | 'jev')[]
  tool_last_used: LiveToolUse | null
  jev: LiveJev
  egress: LiveEgress | null
}

/**
 * The person in control (§4.2). `renew_by` is when control lapses unless
 * renewed; the console does not read it.
 */
export interface LiveController {
  email: string
  since: string | null
  renew_by?: string | null
}

/** The device the gate has applied (§6.1). */
export interface LiveDevice {
  preset: string
  orientation: 'portrait' | 'landscape'
  touch: boolean
}

/**
 * The live browser's egress as the gate last reported it. `country` is this
 * deployment's exit country while residential, else null; `last_error` one of
 * the gate's fixed sentences, never a vendor's.
 */
export interface LiveEgress {
  mode: 'residential' | 'direct'
  country: string | null
  state: 'ok' | 'failing' | 'off'
  last_error: string | null
}

/**
 * `Task.human_wait` as a viewer sees it (§4.1 step 3): no request id, no
 * generation.
 */
export interface LiveHumanWait {
  reason: string | null
  expect: string | null
  requested_at: string | null
  deadline: string | null
}

/**
 * Whether jev is registered on this attempt, why not, and whose TypeSafe key
 * it runs on (`user`, `tenant`; null when it is not on).
 *
 * `no_route`: the workspace judges with an internal backend and the tailnet
 * egress to the decision API is off (JEV-OFF, archived issue 1345). Before the
 * runner has reported, a task that chose jev or auto is served the reason its
 * workspace's backend cannot run (`judgment.unavailable_code`).
 */
export interface LiveJev {
  enabled: boolean
  reason: 'no_key' | 'not_installed' | 'not_selected' | 'no_route' | null
  key_scope: 'user' | 'tenant' | null
}

/** The top frame's origin as the GATE read it over CDP (§5.7). */
export interface LiveOrigin {
  origin: string
  host: string
  display_host?: string | null
  registrable_domain: string
  scheme: string
  ip_literal?: boolean | null
  cert_error?: boolean | null
  path?: string | null
}

/** One of the gate's fixed device presets (§6.1). */
export interface LivePreset {
  name: string
  width: number
  height: number
  scale: number
  mobile: boolean
}

/**
 * `GET /v1/tasks/{task_id}/live/session`, and the answer of every control
 * route. `live_browser` is the task's profile's catalogue property
 * (`RunnerProfile.live_browser`, contract request 71), true for a live-browser
 * task before, during and after its session; `session` is null until the
 * gate's tunnel has opened one for the task's current attempt.
 */
export interface LiveRead {
  live_browser: boolean
  session: LiveSessionView | null
  browser?: LiveBrowserTool | null
}

export interface LiveSessionView {
  session_id: string
  state: 'open' | 'closed'
  closed_reason: string | null
  origin: LiveOrigin | null
  human_wait: LiveHumanWait | null
  controller: LiveController | null
  open_to_members: boolean
  device: LiveDevice | null
  presets: LivePreset[]
  you: LiveYou
  agent_connected_at?: string | null
  agent_connections?: number | null
  first_frame_at?: string | null
}

/** `POST /v1/tasks/{task_id}/live/ticket` (§5.2 step 3). */
export interface LiveTicketView {
  ticket: string
  session_id: string
  expires_in: number
  socket_path: string
}

/** The tool whose tagged CDP connection sent the last command, and when. */
export interface LiveToolUse {
  name: 'playwright' | 'dev-browser' | 'jev'
  at: string
}

/** What THIS caller may do (§5.1). The server decides; the console draws. */
export interface LiveYou {
  email: string
  can_control: boolean
  can_preempt: boolean
  can_open: boolean
  can_switch_device: boolean
}

export interface LoanDeclineRequest {
  reason?: string | null
}

export interface LoanRequest {
  account_id: string
  lend: boolean
}

export interface ManifestStart {
  name?: string | null
}

/**
 * One per-run MCP credential (docs/design/registered-mcp-servers.md §4.3).
 *
 * NOT a `StrictModel`: that strips whitespace from every string, and a
 * credential is not a string to tidy. Both fields are `Any` so that their
 * shape is refused by `mcp_credentials.check_credential` with its own code,
 * `mcp_credential_shape`, whose message names the rule and never the value,
 * rather than by a pydantic type error. The value is a local of the submission
 * and goes to exactly one Secret Manager `add_version` (`SubmissionService`);
 * it is in no document, no response and no log.
 */
export interface McpCredentialCreate {
  value: unknown
  expires_in: unknown
}

/**
 * `{"mcp_credentials": {<server>: {"value", "expires_in"}}}`: the same per-
 * server object the workflow-create body takes, checked the same way
 * (`mcp_credentials.check_credential`), and never dumped or repr'd.
 */
export interface McpCredentialRestore {
  mcp_credentials?: { [key: string]: McpCredentialCreate } | null
}

/** `GET /v1/tenants/me`: the caller and the workspace they are in. */
export interface Me {
  tenant: unknown
  principal: unknown
  environment: string
  environment_declared: boolean
  /** The caller's role in the selected workspace, {name, source}. */
  role?: unknown
  /**
   * caller_mcp/await_input/service_answer, each true, false or "unknown"
   * (FEATURES-READ).
   */
  features?: unknown
}

export interface MemberRemoval {
  email: string
  reason?: string | null
}

/**
 * The pull request a merge-only workflow merges, when no workflow opened it.
 *
 * Owner decision 2026-10-07 (archived issue 352, MS0 question 2). A NUMBER and
 * the HEAD SHA the caller means, never a branch or a repository: the
 * repository is the tenant's registered one (`continuation.resolve_merge_pr`),
 * and the sha is what the merge step pins -- it merges only at that head, with
 * the required checks green there.
 */
export interface MergePullRequest {
  number: number
  head_sha: string
}

/**
 * PUT /v1/workspaces/{id}/merge-settings. Every field optional; a field left
 * out is unchanged, and a body that names nothing is refused.
 */
export interface MergeSettingsPut {
  mode?: 'direct' | 'train' | null
  train_size?: number | null
  max_train_age_minutes?: number | null
  gather_min?: number | null
  gather_wait_minutes?: number | null
  required_start_minutes?: number | null
  base_red_wait_minutes?: number | null
  repositories?: { [key: string]: 'inherit' | 'direct' | 'train' } | null
  repository_train?: { [key: string]: RepositoryTrainPut } | null
}

/**
 * One model's entry in `model_prices` (archived issue 1481). `updated_by` is
 * not the caller's to say: swarm-api stamps the admin who made the change.
 */
export interface ModelPriceRequest {
  input: number
  output: number
  cache_read: number
  cache_write: number
  currency?: 'USD'
  source?: string
  as_of: string
  tiers?: ModelPriceTierRequest[] | null
}

/** One tier of a model priced by prompt length (`modelprices.tiers_problem`). */
export interface ModelPriceTierRequest {
  up_to_prompt_tokens: number | null
  input: number
  output: number
  cache_read: number
  cache_write: number
}

export interface NeededRetry {
  k: number
  of: number
}

export interface NewWorkspaceRequest {
  kind: 'personal' | 'group' | 'service'
  principal: string
  display_name?: string | null
  limits?: RequestLimits | null
  first_admin?: string | null
}

export interface NotFinal {
  attempts: number
  by_exit: ExitCount[]
}

export interface NoteBody {
  text: string
  via: 'voice' | 'typed' | 'tap'
  anchor?: { [key: string]: unknown } | null
  part?: string | null
}

export interface NotesWork {
  note_ids: string[]
  kind: 'follow_up' | 'issue' | 'lane_brief'
  title?: string | null
  session_id?: string | null
}

export interface NotificationPreferences {
  muted: string[]
  reminder: string
}

/** One bucket. When `state` is unread, every count below is null. */
export interface OutcomeBucket {
  start: string
  end: string
  state: BucketState
  /** end > generated_at: the current period, drawn as "so far". */
  in_progress: boolean
  unread_reason: UnreadReason | null
  /** By created_at: the ONLY submission-time field on a bucket. */
  submitted: number | null
  ended: number | null
  succeeded: number | null
  failed: number | null
  dead_lettered: number | null
  cancelled: CancelSplit | null
  rate: Rate | null
  failure_classes: { [key: string]: number } | null
  cost: CostCell | null
}

export interface OutcomeCoverage {
  days: CoverageDays
  unread: UnreadDay[]
  derived_now: number
  built_through: string | null
  reopened: number
  /** null when the count query failed: not read, never 0. */
  terminal_without_completed_at: number | null
  wait_excluded: number
  seal_grace_s: number
}

export interface OutcomeGroups {
  by: GroupBy
  rows_total: number
  rows: GroupRow[]
}

export interface OutcomeLatency {
  percentile_method: string
  by_profile: LatencyRow[]
  /**
   * The same number as `coverage.wait_excluded`, carried here so the latency
   * card need not read `coverage`.
   */
  wait_excluded: number
}

export interface OutcomePrevious {
  since: string
  until: string
  complete: boolean
  succeeded: number
  failed: number
  dead_lettered: number
  cancelled_total: number
  rate: Rate | null
}

export interface OutcomeRetries {
  tries: RetryTries[]
  needed_retry: NeededRetry
  rescued: number
  failed_after_retry: number
  not_final: NotFinal
  admissions_without_attempt_doc: number
}

export interface OutcomeTotals {
  complete: boolean
  buckets: number
  buckets_read: number
  submitted: number
  ended: number
  succeeded: number
  failed: number
  dead_lettered: number
  cancelled: CancelSplit
  rate: Rate | null
  failure_classes: { [key: string]: number }
  cost: TotalsCost
}

/**
 * `GET /v1/outcomes`: what the work in a span ended as, bucketed by when it
 * ended.
 *
 * `coverage` and `previous` are required here because a whole read sends them;
 * the console's `OutcomesHead` is the payload a `section` read may send
 * without them. `ended_rows` (`rows=`) and `usage` (`usage=`) are sent only
 * when asked for.
 */
export interface Outcomes {
  scope: TenantScope | PlatformScope
  tz: string
  requested: OutcomesRequested
  since: string
  until: string
  bucket: LedgerBucketSize
  bucket_chosen_by: 'server' | 'caller'
  basis: OutcomesBasis
  filters: OutcomesFilters
  vocab: OutcomesVocab
  buckets: OutcomeBucket[]
  totals: OutcomeTotals
  retries: OutcomeRetries
  latency: OutcomeLatency
  groups: OutcomeGroups
  workflows_failed: WorkflowsFailed
  coverage: OutcomeCoverage
  previous: OutcomePrevious | null
  reads: number
  cached: boolean
  generated_at: string
  /** The tasks behind one figure, with `rows=`. */
  ended_rows?: unknown
  /** The agents' usage, with `usage=`. */
  usage?: unknown
}

/** WHICH TIMESTAMP EACH SERIES IS PLACED BY, as the route says it. */
export interface OutcomesBasis {
  outcomes: string
  submitted: string
}

export interface OutcomesFilters {
  profile: string[]
  submitted_by: string[]
  kind: string
  tenant: string[]
  exclude_tenant: string[]
  group: GroupBy
}

export interface OutcomesRequested {
  span: string | null
  since: string | null
  until: string | null
  bucket: string
}

export interface OutcomesVocab {
  failure_classes: FailureClassLabel[]
  cancel_causes: CancelCauseLabel[]
  classifier_version: number
}

/**
 * D5: a personal access token for one owner. `owner` and `token` and nothing
 * else -- a tenant, a user or a scope in the body is a 422 that does not echo
 * the value (the app's validation handler serves the field name and the rule,
 * never the input).
 */
export interface OwnerTokenBody {
  owner: string
  token: string
}

export interface PauseRequest {
  reason?: string | null
}

export interface PerSucceededTask {
  n: number
  of: number
  p50_usd: number | null
  p95_usd: number | null
  max_usd: number | null
  values_usd: number[] | null
}

export interface PersonClear {
  reason?: string | null
  via?: 'console' | 'cli'
  email: string
}

export interface PersonRole {
  reason?: string | null
  via?: 'console' | 'cli'
  email: string
  role: string
}

export interface PickedTestRepository {
  repository: string
}

export interface PlanApprove {
  plan_digest: string
}

export interface PlanEdit {
  plan_digest?: string | null
  plan: { [key: string]: unknown }
}

export interface PlanReject {
  plan_digest?: string | null
  reason?: string | null
}

/**
 * `plan_review_caps` in `PUT /v1/admin/settings`: the caps named replace
 * theirs, the others keep their figure. Ranges: `CAPS_RANGES`.
 */
export interface PlanReviewCapsRequest {
  replans?: number | null
  diagram_asks?: number | null
  chat_turns?: number | null
  review_usd?: number | null
}

/**
 * `PUT /v1/workspaces/{tenant_id}/plan-settings`. A field left out is
 * unchanged; `inherit` clears the workspace's choice so the platform's
 * applies. The caps are the platform's: a `caps` key is refused by name.
 */
export interface PlanSettingsPut {
  diagrams?: 'on' | 'off' | 'inherit' | null
  chat_model_class?: string | null
}

export interface PlatformScope {
  kind: 'platform'
  tenants: string[]
  excluded: string[]
  tenants_complete: boolean
}

/**
 * `PUT /v1/admin/settings`: the settings to change; one omitted is left as it
 * is.
 */
export interface PlatformSettingsRequest {
  merge_by_default?: boolean | null
  max_additional_repositories?: number | null
  additional_repositories_budget_gib?: number | null
  model_prices?: { [key: string]: unknown } | null
  agent_budget_ceiling?: BudgetCeilingRequest | null
  jev_token_rates?: JevTokenRatesRequest | null
  plan_diagrams_default?: boolean | null
  plan_chat_model_class?: string | null
  plan_review_caps?: PlanReviewCapsRequest | null
}

export interface PlatformSwitchesPut {
  version: number
  work_contracts?: 'off' | 'on' | null
  contract_review?: 'off' | 'report' | 'enforce' | null
  plugins?: 'off' | 'on' | null
}

export interface PluginsPut {
  allowed: string[]
}

export interface Pointer {
  visual_id: string
  anchor?: { [key: string]: unknown } | null
}

/** `pool_to_api` in codec.py: one capacity pool's ceiling and use. */
export interface Pool {
  name: string
  hard_limit: number | null
  adaptive_target: number | null
  quota_derived_limit: number | null
  effective_limit: number | null
  active: number
  available: number | null
  enabled: boolean
  updated_at: string
  /** What lowered `effective_limit` below `hard_limit`, one entry per cause. */
  lowered_by?: unknown[] | null
}

/**
 * The whole preference set; every field but `time_zone` is required, so a save
 * replaces rather than merges and a missing field is a 422, not a silent
 * reset. A missing `time_zone` keeps the stored one (below).
 */
export interface PreferencesWrite {
  display_name: string | null
  default_workspace: string | null
  notifications: NotificationPreferences
  revision: number
  time_zone?: string | null
}

/** How many more agents of one runner profile fit now, and what binds it. */
export interface ProfileAdmission {
  units: number
  headroom: number | null
  basis: string
  blockers: ProfileBlocker[]
  binding: string[]
  counterfactual: Counterfactual[]
  complete: boolean
  unread: string[]
  uncapped: string[]
}

/**
 * One pool that refuses a runner profile's next agent
 * (`ProfileAdmission.blockers`).
 */
export interface ProfileBlocker {
  pool: string
  reason: string
  limit: number | null
  active: number
  group: string | null
}

export interface ProviderEnableRequest {
  enabled: boolean
  reason?: string | null
}

/** One provider of `GET /v1/providers`. */
export interface ProviderEntry {
  provider: string
  credential_registered: boolean
  credential_source?: string | null
  runnable_profiles?: string[] | null
  runner_profiles: string[]
  quota: QuotaState | null
}

export interface ProviderKeyWrite {
  api_key: string
  link_token?: string | null
}

/** `GET /v1/providers`. */
export interface ProvidersPage {
  tenant_id: string
  providers: ProviderEntry[]
  account_pool?: string | null
  generated_at: string
}

/** One setting or both; a body with neither is refused 422. */
export interface QaSettingsPut {
  qa_flows_per_step?: number | null
  writing_flows?: boolean | null
}

export interface QualityGatesPut {
  jev: boolean | 'on_request'
}

/** `GET /v1/admin/quota`. */
export interface QuotaPage {
  tenant_id: string | null
  quota: QuotaState[]
  truncated: boolean
  generated_at: string
}

/** `quota_to_api` in codec.py: one provider's quota reading for one tenant. */
export interface QuotaState {
  provider: string
  tenant_id: string
  state: string
  updated_at: string
  configured_hard_max: number
  adaptive_target: number | null
  quota_derived_limit: number | null
  requests_remaining: number | null
  tokens_remaining: number | null
  reset_at: string | null
  cooldown_until: string | null
  last_429_at: string | null
  retry_after_seconds: number | null
  success_count: number
  rate_limit_count: number
  effective_limit: number
  /** The pool this reading lowers, if any. */
  feeds_pool?: Pool | null
}

/** k succeeded of n decided, with its Wilson 95 % interval. Null when n = 0. */
export interface Rate {
  k: number
  n: number
  p: number
  lo: number
  hi: number
}

export interface ReadRequest {
  ids: string[]
}

export interface RebuildBody {
  base: { [key: string]: string }
}

/**
 * One segment, in both of the console's shapes: Recordings.tsx reads `name`,
 * `key`, `bytes`, `created_at`; Theater.tsx reads `seq`, `object`,
 * `started_at`.
 */
export interface RecordingSegmentView {
  attempt_id: string
  seq: number
  name: string
  key: string
  object: string
  bytes: number | null
  created_at: string | null
  started_at: string | null
}

/** `GET /v1/tasks/{task_id}/live/recordings`, oldest first. */
export interface RecordingsView {
  tenant_id: string
  task_id: string
  recordings: RecordingSegmentView[]
  segments: RecordingSegmentView[]
  truncated: boolean
}

/**
 * Whether the broker's refresh sweep is still running; `readable: false` is
 * unknown.
 */
export interface RefreshSweep {
  last_completed_at: string | null
  last_outcome: string | null
  readable: boolean
}

export interface RenameProject {
  title: string
}

export interface RenameRequest {
  display_name: string
}

/**
 * `/reopen`'s bodies (THE PAUSE): swarm-voice's `{tier}` or `{state, cause?}`
 * through the grant, the person's `{offer_sdp, cause}`.
 */
export interface Reopen {
  tier?: string | null
  state?: string | null
  cause?: string | null
  offer_sdp?: string | null
}

export interface RepositoryCreate {
  repository: string
  forge?: 'github'
  default_branch?: string | null
  allowed_profiles?: string[] | null
  index?: IndexSettings | null
  platform?: boolean | null
  hard_stop_paths?: string[] | null
  merge_policy?: 'off' | 'on_merge_verdict' | null
  workflow_files?: boolean | null
}

/**
 * What repo-index.md §6.1 lets a PATCH change: schedule, trigger, the index
 * history kept (`index.keep_versions`), `allowed_profiles`, `default_branch`,
 * `paused`; and the hard stops' `platform` (a platform admin's only) and
 * `hard_stop_paths` (docs/schedules.md §4.4); `merge_policy` (a platform
 * admin's only); and `context` (the workspace owner's or a platform admin's,
 * CTXQ-SET); and `agents_may_file_issues` (the workspace admin's, AU-0 REG-
 * FLAG); and `workflow_files` (the workspace admin's, archived issue 2029).
 * Never the repository, the tenant or anything an index run writes.
 */
export interface RepositoryPatch {
  default_branch?: string | null
  allowed_profiles?: string[] | null
  paused?: boolean | null
  index?: IndexSettings | null
  platform?: boolean | null
  hard_stop_paths?: string[] | null
  merge_policy?: 'off' | 'on_merge_verdict' | 'inherit' | null
  readable_by_tasks?: boolean | null
  context?: 'on' | 'off' | 'inherit' | null
  agents_may_file_issues?: boolean | null
  workflow_files?: boolean | null
}

/**
 * What `swarm-github-hooks` forwards of a verified push: the repository's name
 * and nothing else (docs/design/graph-service.md §6.2 step 4).
 *
 * extra="forbid": a payload's `after` sha, its `ref` or a tenant is refused,
 * never ignored, so nothing from the payload but the name can be read, trusted
 * or stored. The head is read from GitHub with each workspace's own credential
 * (`repoindex.RepoIndex.push`).
 */
export interface RepositoryPush {
  forge: 'github'
  full_name: string
}

/** One registration's train settings, each bounded as the workspace's. */
export interface RepositoryTrainPut {
  gather_min?: number | null
  gather_wait_minutes?: number | null
  required_start_minutes?: number | null
  base_red_wait_minutes?: number | null
}

export interface RequestLimits {
  max_active?: number | null
  capacity_units?: number | null
  quota_pods?: number | null
  quota_cpu?: number | null
}

/** A resource class's sizing, as `/v1/runtimes` joins it to each runtime. */
export interface ResourceClassSpec {
  name: string
  cpu: number
  cpu_request?: number | null
  cpu_limit?: number | null
  memory_gib: number
  disk_gib: number
  units: number
  size_version?: number | null
}

export interface RetryTries {
  attempts: string
  tasks: number
  succeeded: number
  failed: number
  dead_lettered: number
  cancelled: number
}

export interface RouteBody {
  route_id: string
}

export interface RunBody {
  screens?: string[] | null
  file?: 'issues' | 'none'
  browser_tool?: 'playwright' | 'jev' | null
  environments?: string[] | null
  environment?: string | null
  flows_per_step?: number | null
}

/**
 * Plan an issue, and run the plan once it is approved.
 *
 * No profile, image, command or resource field, as everywhere else (invariant
 * 10): the planner and every step are `claude-code`, chosen by `issueruns`,
 * never by the caller.
 */
export interface RunCreate {
  issue: string
  plan_approval?: 'required' | 'auto'
  auto_merge?: boolean | null
  fix_rounds?: number
  security_review?: boolean
  claude_session?: string | null
}

export interface RunEnd {
  reason: string
}

export interface RunnerKeyLinkCreate {
  provider: string
}

export interface RunnerKeyWrite {
  provider: string
  api_key: string
  link_token?: string | null
}

/**
 * One entry of `GET /v1/capacity`'s `runner_profiles`, keyed by the profile's
 * name.
 */
export interface RunnerProfile {
  resource_class: string
  backend: string
  provider: string | null
  units: number
  pools: string[]
  /** Contract request 129: the units this profile takes from each of its pools. */
  pool_units?: { [key: string]: number } | null
  available?: boolean | null
  disabled_reason?: string | null
  admission?: ProfileAdmission | null
  /** What the profile's input must carry. */
  input_contract?: unknown
}

/** `POST /v1/admin/runner-sizes:preview`: the PUT's classes; nothing written. */
export interface RunnerSizesPreviewRequest {
  classes?: { [key: string]: { [key: string]: unknown } }
  base_version?: number | null
}

/**
 * `PUT /v1/admin/runner-sizes`. A class or field the body does not name keeps
 * its value. `base_version` is the version the admin edited: another admin's
 * save in between is a 409, never a silent overwrite.
 */
export interface RunnerSizesRequest {
  base_version: number
  reason: string
  classes: { [key: string]: { [key: string]: unknown } }
  confirm_below_recent_peak?: boolean
}

/**
 * `POST /v1/admin/runner-sizes/restore`: a NEW version with `version`'s
 * classes (0: the code defaults). The pointer is never rewound.
 */
export interface RunnerSizesRestoreRequest {
  base_version: number
  version: number
  reason: string
  confirm_below_recent_peak?: boolean
}

/** One runner profile as `/v1/runtimes` publishes it, keyed by its name. */
export interface Runtime {
  name: string
  available: boolean
  disabled_reason: string
  image: string
  node_version?: string | null
  backend: string
  resolved_backend: string
  provider: string | null
  secrets: string[]
  secrets_any_of: boolean
  timeout_seconds: number
  resource_class: string
  model_classes?: string[] | null
  default_model_class?: string | null
  models?: { [key: string]: string } | null
  resources: ResourceClassSpec
  agent_memory_gib?: number | null
  display_memory_gib?: number | null
  pod_memory_gib?: number | null
}

/** `GET /v1/runtimes`. */
export interface RuntimesPage {
  runtimes: { [key: string]: Runtime }
}

export interface SaveLogin {
  name: string
  expires_in_days?: number
}

export interface SearchRequest {
  q: string
  limit?: number
  sha?: string | null
}

export interface SecretCreate {
  name: string
  provider: string
  kind: string
  value?: string | null
  fields?: { [key: string]: unknown } | null
  link_token?: string | null
}

export interface SecretDelete {
  name: string
}

export interface SecretValue {
  value?: string | null
  fields?: { [key: string]: unknown } | null
  link_token?: string | null
}

export interface SecretWrite {
  value: string
  link_token: string
}

export interface Seen {
  seq: number
}

/**
 * `POST .../tests:select`: changed paths, as data. Never resolved on a
 * filesystem.
 */
export interface SelectRequest {
  paths: string[]
}

export interface SeriesPoint {
  k: number
  n: number
}

export interface ServerCreate {
  name: string
  url: string
  transport?: 'streamable-http'
  description?: string
  auth?: AuthBody
  allowed_profiles?: string[]
  allowed_tools?: string[]
  limits?: LimitsBody | null
  enabled?: boolean
}

export interface ServerPatch {
  url?: string | null
  description?: string | null
  auth?: AuthBody | null
  allowed_profiles?: string[] | null
  allowed_tools?: string[] | null
  limits?: LimitsBody | null
  enabled?: boolean | null
}

export interface SignInClient {
  client_id: string
  client_secret: string
}

export interface SourceBody {
  kind: string
  repository: string
  ref: string
  subdir?: string
  plugin?: string | null
}

export interface StartSession {
  offer_sdp?: string | null
  project_id?: string | null
  session_id?: string | null
}

/** A latency distribution, in seconds. */
export interface Stat {
  n: number
  p50_s: number
  p95_s: number
  max_s: number
  /**
   * Only when n < 5: the values themselves, because a percentile of four is a
   * guess.
   */
  values_s: number[] | null
}

/**
 * `GET /v1/stats`. `platform_tasks_by_state` is ABSENT, not empty, for a non-
 * admin: an absent key is not a count of nothing.
 */
export interface Stats {
  tenant_id: string
  tasks_by_state: { [key: string]: number }
  dispatch_paused: boolean
  limits: { [key: string]: number }
  generated_at: string
  /** Served only to an admin who asks for `scope=platform`; absent otherwise. */
  platform_tasks_by_state?: { [key: string]: number } | null
  /** running, paused or unknown (U26). */
  dispatch_state?: string | null
  /** present or missing (U26). */
  control_document?: string | null
}

/**
 * `budget` on a task or a workflow step: `{usd: N}` or `{tokens: N}` (archived
 * issue 2459).
 *
 * It may only LOWER the attempt's limit: `budget.refuse_step_above` refuses
 * one above the workspace default or the platform ceiling, naming both. A
 * limit, never a resource (invariant 10), as `timeout_seconds` is.
 */
export interface StepBudget {
  usd?: number | null
  tokens?: number | null
}

/**
 * One plugin a step loads, by name in `plugins` (invariant 10: a name, never a
 * path, a command, a URL or a version).
 *
 * A built-in (docs/design/agent-plugins.md, "Per-step selection") names a mode
 * and skills, checked against the catalogue, the step's runner profile and the
 * workspace's signed setting by `plugins.check_step`. A managed install
 * (docs/design/managed-plugins.md section 7), named by its install id, names
 * neither: it may carry `overrides`, values for the non-secret fields its
 * pinned version declares (section 6.4), and `plugins.merge_step_plugins`
 * refuses a mode or skills on it and overrides on a built-in.
 */
export interface StepPluginCreate {
  mode?: string | null
  skills?: string[] | null
  overrides?: { [key: string]: string | boolean | number | (string | boolean | number)[] } | null
}

/**
 * `when` on a workflow step: run this step's agent only on these verdicts
 * (archived issue 264).
 *
 * The verdict is read from the file this step stages from `step` through
 * `input_from`, a JSON object whose `verdict` is one of
 * `validation.REVIEW_VERDICTS`. When the verdict is not in `verdict_in` the
 * step still runs, without an agent, and still publishes: it is the step that
 * opens the pull request. The values are checked by
 * `validation.validate_step_routing`, which names the accepted ones.
 *
 * The gate may also read further reviews (`also`): their verdict files, staged
 * through `input_from` under names of their own, are read the same way, and
 * the agent runs when ANY of the verdicts read is in `verdict_in` -- the
 * security review beside an issue run's first review, whose fix runs when
 * either says NOT_YET. Empty, the gate is the one review's, as it always was.
 */
export interface StepVerdictGate {
  step: string
  verdict_in: string[]
  also?: string[]
}

export interface StrandedSweepRequest {
  redrive?: boolean
}

/**
 * A registration, or a change of one. Each field is checked by
 * `swarm_api.qatargets` and `swarm_api.qaspec`, so a refusal is their own
 * sentence rather than a validation list. No credential: that is the PUT's.
 */
export interface TargetBody {
  name?: unknown
  base_url?: unknown
  repository?: unknown
  sign_in?: unknown
  screens?: unknown
  discover?: unknown
  flows?: unknown
  widths?: unknown
  themes?: unknown
  exclusions?: unknown
  environment?: unknown
  read_posts?: unknown
  design_docs?: unknown
  file?: unknown
  rate?: unknown
  clock?: unknown
  challenge_bypass?: unknown
  flows_browser_tool?: unknown
  entry_query?: unknown
  extra_origins?: unknown
  environments?: unknown
  authorized_by?: unknown
}

/**
 * `task_to_api` in codec.py, plus what the task routes add beside it
 * (`submitted_by_name`, the attempt totals, `contract_acceptance`).
 */
export interface Task {
  id: string
  tenant_id: string
  state: TaskState
  runner_profile: string
  resource_class: string
  provider: string | null
  priority: number
  created_at: string
  updated_at: string
  started_at: string | null
  completed_at: string | null
  submitted_by: string | null
  /** The submitter's display name (`people.add_names`); null when none is set. */
  submitted_by_name?: string | null
  attempt_count: number
  max_attempts: number
  /**
   * Contract request 123: consecutive dispatches the cluster refused (Job quota
   * or an admission policy). Each parks PLATFORM_REFUSED without spending an
   * attempt; a dispatch that succeeds resets it to 0.
   */
  platform_refusals?: number
  park_reason: string | null
  blocked_by: unknown[] | null
  workflow_id: string | null
  step_id: string | null
  depends_on: string[] | null
  /**
   * Contract request 14 (docs/design/child-tasks.md §6.3). The task whose agent
   * submitted this one, and that parent's attempt; set by swarm-api from the
   * submitting attempt, null for everything else. OPTIONAL: an API older than
   * the change does not send them. Not `depends_on`: a parent can cancel its
   * children and awaits them, a dependency does neither.
   */
  parent_task_id?: string | null
  parent_attempt_id?: string | null
  /**
   * archived issue 780 OB7: the slot the task's GitHub token is read from, by
   * name.
   */
  forge_credential?: string | null
  /** `read` or `write`; null when the task names no GitHub credential. */
  forge_access?: 'read' | 'write' | null
  /**
   * "the submitter's GitHub credential", "tenant token, service submission",
   * "owner's GitHub connection, service submission" (INDEX-CRED), or null.
   */
  forge_credential_source?: string | null
  /**
   * Contract request 95 (docs/design/multi-repository-steps.md §4.1): the
   * repositories this task reads beside its own, each pinned to a commit. Null
   * when it reads none. OPTIONAL: an older API does not send it.
   */
  additional_repositories?: unknown[] | null
  cancel_requested: boolean
  repository_url: string | null
  model: string | null
  /**
   * The model class the task runs (contract request 104, archived issue 1435),
   * RESOLVED: the stored class, or its profile's default when none was stored,
   * which `model_class_defaulted` then says. Null on a profile that runs no
   * Claude model. OPTIONAL: an older API does not send it.
   */
  model_class?: string | null
  model_class_defaulted?: boolean
  timeout_seconds: number | null
  /**
   * Contract request 131: the `timeout_seconds` the spec asked for when the API
   * stored a lower one (the profile's cap, or a child's parent's), else null. A
   * record only: the limit is `timeout_seconds`. OPTIONAL: an older API does not
   * send it.
   */
  timeout_seconds_asked?: number | null
  /**
   * Contract request 133: the task's resolved turn cap, which the worker passes
   * to the agent CLI, or null for none. OPTIONAL: an older API does not send it.
   */
  max_turns?: number | null
  /** When a PARKED task becomes eligible again. Null unless it is parked. */
  next_eligible_at: string | null
  /**
   * MASKED, like `input` (owner decision, 2026-09-26: masked everywhere). The
   * caller's keys come through the API's masker; the platform's own (`dispatch`,
   * `input_from`, `expected_outputs`, which submission refuses from callers) as
   * stored. `metadata_redaction_count` is how many masks it took.
   */
  metadata: { [key: string]: unknown } | null
  /**
   * A single task's own description (archived issue 2688): plain text of at most
   * 600 characters, masked as its metadata is. Null for a workflow's step, which
   * inherits nothing; OPTIONAL, as an API older than the field sends none.
   */
  description?: string | null
  repository_ref: string | null
  /**
   * Absent from a `view=summary` listing row (`codec.SUMMARY_DROPPED_KEYS`). THE
   * MASKED INPUT. `GET /v1/tasks/{id}` serves the input the API masked at read
   * time, never the input as submitted -- even to the tenant that submitted it
   * (owner decision, 2026-09-26). Screens still draw the `/input` copy, which
   * carries the prompt and the rest as blocks.
   */
  input?: unknown
  /**
   * How many credential-shaped strings the API masked in `input` and in
   * `metadata`. OPTIONAL: an API older than the change sends neither, and serves
   * both unmasked, so absent is not zero.
   */
  input_redaction_count?: number | null
  metadata_redaction_count?: number | null
  /**
   * MASKED too, since the archived pull request 229 review: the agent's stderr
   * tail and its own summary text, by the input's masker, string by string (an
   * artifact's `name` and `uri` as stored). Each count is how many masks that
   * took; optional for the reason the two above are.
   */
  last_error: string | null
  last_error_redaction_count?: number | null
  /**
   * The agent's own summary, masked string by string. Absent from a
   * `view=summary` listing row (`codec.SUMMARY_DROPPED_KEYS`).
   */
  result_summary?: { [key: string]: unknown } | null
  result_summary_redaction_count?: number | null
  /**
   * Userinfo that could carry a credential is masked whole; submission refuses
   * it now.
   */
  repository_url_redaction_count?: number | null
  latest_checkpoint: string | null
  /**
   * Contract request 70 (LB-C), docs/design/live-browser.md §4.1: set while a
   * RUNNING browser task holds its pod for a person, null otherwise, and cleared
   * when the hold parks. OPTIONAL: an API older than LB-L12 does not send it.
   * Read through `handoffOf` (Handoff.tsx), never raw.
   */
  human_wait?: unknown
  /**
   * F1-POD: why the current attempt's GKE pod is not starting, as the reconciler
   * last read it (`reconciler.startproblem`); null when nothing is recorded.
   * Kept on a cancelled task, which is how `elapsed()` tells "started, did not
   * reach running" from "never ran". OPTIONAL: an older API does not send it.
   * Drawn by `PodStartProblem`.
   */
  start_problem?: unknown
  /**
   * TASK-ANSWERS (part of archived issue 1206): the owner's answers to the
   * agent's questions, in question order, lifted by the API out of
   * `metadata.owner_answers`; `[]` when none is answered. OPTIONAL: an older API
   * does not send it. The card reads them from `GET /questions` instead, beside
   * each question.
   */
  owner_answers?: unknown[]
  /**
   * Contract request 23 (archived pull request 217). Written by every terminal
   * writer beside `completed_at`. Null on SUCCEEDED and on a task that ended
   * before this field existed. One of `END_CAUSES` from this build's contract;
   * typed as a string because a newer API may send a value this build does not
   * know.
   */
  end_cause?: string | null
  /**
   * archived issue 362. Which pool refuses this READY task NOW, read live on the
   * GET by `swarm_api/waiting.py` from the task's own pools. Null for every
   * other state; OPTIONAL because an older API does not send it. `blocked_by` is
   * the scheduler's record from its LAST pass; this is the current reading.
   */
  waiting_for?: unknown
  /**
   * archived issue 1782 (WAIT-API). Why a PARKED, READY or QUEUED task waits,
   * built by `swarm_api/waiting_detail.py` from what the platform recorded --
   * never read from GitHub or a provider. `GET /v1/tasks/{id}` serves it in
   * full; a listing row serves `WaitingRow`. Null for every other state;
   * OPTIONAL because an older API does not send it.
   */
  waiting?: unknown
  /**
   * archived issue 379. The subscription account the LATEST attempt runs on,
   * derived by `swarm_api/task_accounts.py` from this task's own account events.
   * OPTIONAL and nullable: an older API does not send it, and a route that did
   * not read it sends null. Read it through `accountText`, never raw.
   */
  account?: unknown
  /**
   * archived issue 179. The worker's last beat, from the task's current LEASE
   * (the worker never writes it to the task), read by `swarm_api/heartbeats.py`
   * on `GET /v1/tasks` and `GET /v1/tasks/{id}` for LEASED, DISPATCHED, STARTING
   * and RUNNING. Null when the lease has NEVER beaten -- never the lease's
   * creation time -- and null for every other state. OPTIONAL because an older
   * API does not send it. Read it through `silentWorkerLine` (Agents.tsx), and
   * only when `heartbeat` is `'read'`.
   */
  heartbeat_at?: string | null
  /** The reconciler's grace, resolved server-side; null where `heartbeat` is. */
  heartbeat_grace_seconds?: number | null
  /**
   * What the reading is: `'read'` (the lease was read), `'not read'` (the read
   * failed -- a null `heartbeat_at` then means unknown, never silent), `'no
   * lease'`, or null for a task that holds no lease.
   */
  heartbeat?: string | null
  /**
   * WHETHER A BROWSER RUNTIME USED ITS BROWSER (BROWSER-USE-FLAG, archived issue
   * 2322), the worker's verdict lifted out of `result_summary` by
   * `codec.browser_use_to_api`, on the list rows too. True or false only on a
   * browser runtime that ended with its counts read; NULL IS NOT FALSE: it is a
   * runtime with no browser, an agent not ended, one that predates the record,
   * or counts the worker could not read (`browser_use.unknown_because`). Read it
   * through `browserUseOf` (BrowserUse.tsx). OPTIONAL: an older API does not
   * send it.
   */
  browser_used?: boolean | null
  /** Its counts and seconds; null when the worker recorded none. */
  browser_use?: unknown
  /**
   * EVERY ATTEMPT'S COST AND TIME (lane review P1, 2026-10-05), from
   * `swarm_api/attempt_totals.py` on `GET /v1/tasks/{id}` and the workflow read.
   * `result_summary` and `started_at` describe only the LAST attempt; these sum
   * all of them. OPTIONAL because an older API and the list routes do not send
   * them. `cost_usd_total` null is NOT MEASURED, never $0; `cost_incomplete`
   * true makes it a floor ("at least"). `attempts_read: 'failed'` means the
   * attempts were not read and every figure is null.
   */
  attempts?: number | null
  attempts_with_cost?: number | null
  cost_usd_total?: number | null
  cost_incomplete?: boolean | null
  duration_s_total?: number | null
  duration_incomplete?: boolean | null
  first_started_at?: string | null
  last_attempt_cost_usd?: number | null
  last_attempt_duration_s?: number | null
  attempts_read?: 'ok' | 'failed'
  /**
   * THE FENCING GENERATION. `models.py:177`, now served by `task_to_api`. It
   * increments on admission (`admission.py:186`) AND on a reconciler fence
   * (`reconciler/store.py:220-227`), so `current_generation > attempt_count` is
   * exactly "a stale worker was fenced out" -- the only cheap signal that
   * CONTRACT invariant 5 has fired. `AttemptRow.generation` is a different and
   * narrower thing: the generation ONE attempt was minted with. NOT OPTIONAL and
   * 0 IS AN ANSWER: the API sends it on every task, and zero means never
   * admitted. Read `> attempt_count` rather than truthiness.
   */
  current_generation: number
  /**
   * The lease holding this task's capacity, or null if it holds none. Null on a
   * QUEUED, PARKED, READY or terminal task -- those cost nothing (invariant 1).
   * It is the join key onto `LeaseRow`, and comparing that row's `generation`
   * against `current_generation` above is how a stalled task is diagnosed: a
   * task at generation 2 whose named lease is still at generation 1 and
   * unreleased is holding a slot for work that will never run. That is the
   * twenty-minute DISPATCHED stall on task_b5dc2568713a40158851, which no screen
   * could explain because neither number reached the browser.
   */
  current_lease_id: string | null
  /**
   * `codec.dispatch_of`, lifted out of `metadata.dispatch` by the API. OPTIONAL
   * ON PURPOSE, and it is the one field in this interface whose absence means
   * something. `task_to_api` always sends it -- filling the API's defaults for a
   * task submitted before the feature existed -- so a missing key here is not an
   * old TASK, it is an older API than this bundle. Those are different and
   * `dispatchOf` below keeps them different: an old task reads `collect`, an old
   * API reads "not reported".
   */
  dispatch?: unknown
  /**
   * The step's `contract_acceptance` (jev-contract.md sections 5.4 and 8.2),
   * served on the detail only and only where Jev judged the step's work
   * contract: read by `ContractJev.contractAcceptance`, drawn as Jev's.
   */
  contract_acceptance?: unknown
  /**
   * The console link, for surfaces OUTSIDE the console (MCP, sc, plugin, PR
   * bodies); inside it the address bar is the link.
   */
  links?: TaskLinks | null
  /**
   * The Claude Code session that submitted it (SC-SESSION-SCOPE), lifted out of
   * `metadata`; read by the sc plugin, the console does not scope by it.
   */
  claude_session?: unknown
  /**
   * Contract request 120: the attempt's resolved per-agent budget, `{usd?,
   * tokens?}`, or null for none.
   */
  budget?: unknown
}

export interface TaskBatchCreate {
  tasks: TaskCreate[]
  idempotency_key?: string | null
}

export interface TaskCreate {
  runner_profile: string
  idempotency_key?: string | null
  input?: { [key: string]: unknown }
  priority?: number
  metadata?: { [key: string]: unknown }
  repository_url?: string | null
  repository_ref?: string | null
  timeout_seconds?: number | null
  max_turns?: number | null
  budget?: StepBudget | null
  max_attempts?: number | null
  model?: string | null
  model_class?: string | null
  plugins?: { [key: string]: StepPluginCreate } | null
  plugins_off?: string[] | null
  strategy?: string
  carrier?: string
  dispatch?: { [key: string]: unknown } | null
  mcp_credentials?: { [key: string]: McpCredentialCreate } | null
  additional_repositories?: AdditionalRepositoryCreate[] | null
  publish_to_pr?: number | string | null
}

/** `GET /v1/tasks/{task_id}`: the one task, under `task`. */
export interface TaskEnvelope {
  task: Task
}

/** `event_to_api` in codec.py: one entry of a task's event log. */
export interface TaskEvent {
  event_id: string
  task_id: string
  type: string
  at: string
  attempt_id: string | null
  lease_id: string | null
  generation: number | null
  detail: { [key: string]: unknown } | null
  detail_redaction_count?: number | null
}

/** `GET /v1/tasks/{task_id}/events`. */
export interface TaskEventPage {
  task_id: string
  events: TaskEvent[]
  next_page_token: string | null
}

/** `links` on a task: its console page, for surfaces OUTSIDE the console. */
export interface TaskLinks {
  /**
   * This agent's page on the deployment's console, the same in every state; null
   * when the deployment has no console (`agent_console_url`).
   */
  console: string | null
}

/** `GET /v1/tasks`: one page of tasks. The paging key is `next_page_token`. */
export interface TaskPage {
  tasks: Task[]
  /**
   * Pass back as `page_token` for the next page; null on the last. This API has
   * never sent `next_cursor`.
   */
  next_page_token?: string | null
  tenant_id?: string | null
}

export type TaskState = 'SUBMITTED' | 'QUEUED' | 'PARKED' | 'READY' | 'LEASED' | 'DISPATCHED' | 'STARTING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED' | 'DEAD_LETTERED'

export interface TeamCreate {
  group: string
  display_name?: string | null
  first_admin?: string | null
  invites?: string[]
}

/** `tenant_to_api` in codec.py. */
export interface Tenant {
  tenant_id: string
  kind: string
  principal: string
  display_name: string | null
  created_at: string
  max_active: number
  capacity_units: number
  monthly_budget_usd: number | null
  enabled: boolean
  credentials: string[]
  service_account: string | null
  gcs_prefix: string | null
  namespace: string | null
  /** The workspace record's kind; null for a Terraform tenant (WK-ADMIN-LIST). */
  workspace_kind?: string | null
  /** The workspace record's w- id; null for a Terraform tenant (WK-ADMIN-LIST). */
  workspace_id?: string | null
}

/**
 * `PUT /v1/admin/tenants/{id}/findings-epic` (archived issue 638).
 *
 * The issue number, in the repository each run works on, that a review's minor
 * findings are filed on as comments; null stops the filing. Required, so an
 * empty body is a 422 rather than a silent clear. Strict, so `"638"` and
 * `true` are refused rather than coerced to an issue nobody chose.
 */
export interface TenantFindingsEpicRequest {
  findings_epic: number | null
}

export interface TenantLimitsRequest {
  max_active?: number | null
  capacity_units?: number | null
  enabled?: boolean | null
  monthly_budget_usd?: number | null
}

/** `GET /v1/admin/tenants`. */
export interface TenantPage {
  tenants: Tenant[]
  /**
   * Whether the workspace records could not be read, so `workspace_kind` and
   * `workspace_id` read null for every row.
   */
  workspaces_unread: boolean
}

export interface TenantScope {
  kind: 'tenant'
  tenant_id: string
}

export interface TerritoryRequest {
  files: string[]
  depth?: number
  pull_requests?: boolean
  lanes?: boolean
  exclude_runs?: string[]
  exclude_pull_requests?: number[]
  sha?: string | null
}

export interface TextTurn {
  text: string
}

export interface ToWork {
  note_ids: string[]
  kind: 'follow_up' | 'issue' | 'lane_brief'
  title?: string | null
}

export interface TotalsCost {
  /** null when no attempt reported a cost: an absence, never $0.00. */
  sum_usd: number | null
  attempts: number
  reporting: number
  by_outcome: CostByOutcome
  per_succeeded_task: PerSucceededTask
  retries: CostCell
  declared: DeclaredCost
}

export interface UndoPreferences {
  revision?: number | null
}

export interface UnreadDay {
  tenant_id: string
  day: string
  reason: UnreadReason
}

/** Why a bucket or a tenant-day was not read. */
export type UnreadReason = 'derive_budget' | 'read_failed' | 'too_large'

export interface UpdateBody {
  ref?: string | null
  repository?: string | null
  upload?: UploadBody | null
  overlay?: { [key: string]: unknown } | null
}

export interface UploadBody {
  filename: string
  size: number
  subdir?: string
  overlay?: { [key: string]: unknown } | null
}

export interface UseInWrite {
  workspaces: string[]
}

export interface ValidationError {
  loc: (string | number)[]
  msg: string
  type: string
  input?: unknown
  ctx?: { [key: string]: unknown }
}

export interface ValuesBody {
  version_id: string
  revision: number
  values: { [key: string]: unknown }
}

export interface VerifyBody {
  checks?: ('clone' | 'push' | 'pull_request' | 'push_test')[] | null
}

/** Optionally, the one repository to probe. `extra="forbid"`, as above. */
export interface VerifyScope {
  repository?: string | null
  repo_id?: string | null
}

export interface VisualBody {
  visual_id?: string | null
  kind: 'mockup' | 'diagram' | 'spec' | 'code' | 'log'
  title: string
  body?: string | null
  parts?: { [key: string]: unknown }[] | null
  source?: { [key: string]: unknown } | null
}

/** The warm takes no field: no machine, zone, image or engine (invariant 10). */
export type WarmSpeech = { [key: string]: never }

/**
 * `/work`'s body (voice/workfeed.py): swarm-voice's `{delegation:
 * {delegation_id, state}}` or the follower's `{roster: [rows]}`, each checked
 * field by field in `voice.grant.record_work`.
 */
export interface WorkReport {
  delegation?: { [key: string]: unknown } | null
  roster?: unknown[] | null
  budget?: true | null
}

/**
 * `GET /v1/workflows/{workflow_id}`: the workflow, its steps and its derived
 * state.
 */
export interface Workflow {
  workflow_id: string
  state: string
  tenant_id: string
  stored_state: string
  state_source: string
  rollup?: unknown
  drift?: unknown
  created_at: string
  updated_at: string
  submitted_by: string | null
  submitted_by_name?: string | null
  priority: number
  on_step_failure: string
  cancel_requested: boolean
  steps: unknown[]
  pull_request?: unknown
  /**
   * The Claude Code session that submitted it (SC-SESSION-SCOPE); read by the sc
   * plugin.
   */
  claude_session?: unknown
  /**
   * The console link, for surfaces OUTSIDE the console (MCP, sc, plugin, PR
   * bodies).
   */
  links?: unknown
  /** The workflow's repository, masked as a task's. */
  repository_url?: string | null
  title?: string | null
  /** The submitter's plain-text description. */
  description?: string | null
  /** Who wrote the description. */
  description_source?: string | null
  /** {purpose, goals, output}: the description in three parts. */
  about?: unknown
  /** {title, url?} of the feature it builds. */
  feature?: unknown
  /** schedule, quality_check, issue_run, claude_code or console. */
  started_from?: string | null
}

export interface WorkflowCreate {
  steps: WorkflowStepCreate[]
  idempotency_key?: string | null
  priority?: number
  on_step_failure?: 'fail_workflow' | 'continue'
  metadata?: { [key: string]: unknown }
  fix_rounds?: number | null
  strategy?: string
  carrier?: string
  dispatch?: { [key: string]: unknown } | null
  repository_url?: string | null
  repository_ref?: string | null
  continues_task?: string | null
  merge_pr?: MergePullRequest | null
  publish_to_pr?: number | string | null
  after?: AfterPullRequest[] | null
  mcp_credentials?: { [key: string]: McpCredentialCreate } | null
  additional_repositories?: AdditionalRepositoryCreate[] | null
}

/**
 * `GET /v1/workflows/{workflow_id}`: the workflow, its step tasks and its
 * dispatch.
 */
export interface WorkflowEnvelope {
  workflow: Workflow
  tasks: Task[]
  dispatch: unknown
}

export interface WorkflowFailedRow {
  workflow_id: string
  tenant_id: string
  /**
   * `''` as well as null when no submitter was recorded: both are an absence,
   * never a name.
   */
  submitted_by: string | null
  first_failed: FirstFailed | null
  cascade_cancelled: number
  last_ended_at: string
  state: string
  steps: WorkflowSteps | null
}

export interface WorkflowStepCreate {
  step_id: string
  runner_profile: string
  input?: { [key: string]: unknown }
  depends_on?: string[]
  resource_class?: string | null
  model_class?: string | null
  plugins?: { [key: string]: StepPluginCreate } | null
  plugins_off?: string[] | null
  input_from?: { [key: string]: string }
  timeout_seconds?: number | null
  max_turns?: number | null
  budget?: StepBudget | null
  when?: StepVerdictGate | null
  builds_on?: string | null
  repository_ref?: string | null
  allow_empty_diff?: boolean
  metadata?: { [key: string]: unknown }
  pr_role?: string | null
  merges?: { [key: string]: string } | null
  await_input?: unknown
  fix_loop?: unknown
  additional_repositories?: AdditionalRepositoryCreate[] | null
}

export interface WorkflowSteps {
  total: number
  succeeded: number
  failed: number
  dead_lettered: number
  cancelled: number
  open: number
  unreadable: number
}

export interface WorkflowsFailed {
  applicable: boolean
  /**
   * null when `applicable` is false (kind=standalone): nothing was counted, so
   * no count is a zero.
   */
  with_ended_steps: number | null
  with_failed_steps: number | null
  rows_total: number | null
  rows: WorkflowFailedRow[]
  failing_steps: FailingStep[]
}

export interface WorkspaceRequest {
  via?: 'console' | 'plugin' | 'api'
}

export interface WriteCard {
  card: { [key: string]: unknown }
}

export interface WriteNote {
  note?: NoteBody | null
  to_work?: ToWork | null
  session_id?: string | null
}

export interface WriteVisual {
  visual?: VisualBody | null
  focus?: FocusBody | null
  approve?: swarm_api__routes__voice_visuals__ApproveBody | null
  approved?: true | null
}

export interface swarm_api__routes__access__GrantBody {
  repository: string
  mode: 'read' | 'write'
}

/**
 * REQUIRED, `{}` at least, as application/json, as `LeaveBody` is: a form on
 * another site cannot approve on an admin's front-door cookie.
 */
export interface swarm_api__routes__join_requests__ApproveBody {
  role?: string | null
}

export interface swarm_api__routes__join_requests__LinkBody {
  on: boolean
  rotate?: boolean
}

export interface swarm_api__routes__provider_keys__LinkRequest {
  scope: 'user' | 'tenant'
}

export interface swarm_api__routes__qa_identity__GrantBody {
  reason?: unknown
}

export interface swarm_api__routes__qatargets__ApproveBody {
  host: string
  extra_origin?: string | null
  idp_token_path?: string | null
  extra_origins?: string[] | null
  environment?: string | null
}

export interface swarm_api__routes__qatargets__LinkBody {
  environment?: string | null
  field?: 'credential' | 'challenge_bypass'
}

export interface swarm_api__routes__voice_visuals__ApproveBody {
  visual_id: string
  frame: string
}

export interface swarm_api__routes__workspace_secrets__LinkRequest {
  action: 'add' | 'rotate'
  name?: string | null
}
