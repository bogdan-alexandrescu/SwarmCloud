// Shapes returned by swarm-api. Field names mirror
// apps/swarm-api/swarm_api/codec.py exactly -- if one changes there, this is
// the copy that has to follow, and there is no check that notices. Keep the
// comment references so the next person can find the source.

/** `pool_to_api` in codec.py. */
export interface Pool {
  name: string
  hard_limit: number
  /** AIMD target. null when nothing has ever lowered it. */
  adaptive_target: number | null
  /** Ceiling derived from provider quota. null when unknown. */
  quota_derived_limit: number | null
  /** min(hard_limit, adaptive_target, quota_derived_limit), floored at 0. */
  effective_limit: number
  /**
   * WEIGHTED UNITS IN USE, NOT AGENTS. Admission increments by the resource
   * class's `units` -- standard 1, browser 2, large 4 -- so `active: 8` may be
   * two large agents or eight standard ones. Never label this "agents".
   */
  active: number
  available: number
  enabled: boolean
  updated_at: string
}

/** `service.capacity()`. */
export interface Capacity {
  pools: Pool[]
  runner_profiles: Record<string, RunnerProfile>
  generated_at: string
}

export interface RunnerProfile {
  resource_class: string
  backend: string
  provider: string | null
  units: number
  pools: string[]
}

/** The kind of pool, parsed from its name. `global` has no prefix. */
export type PoolKind = 'global' | 'tenant' | 'provider' | 'resource' | 'runner' | 'backend'

export function poolKind(name: string): PoolKind {
  if (name === 'global') return 'global'
  const prefix = name.split(':')[0]
  switch (prefix) {
    case 'tenant':
      return 'tenant'
    case 'provider':
      return 'provider'
    case 'resource':
      return 'resource'
    case 'runner':
      return 'runner'
    case 'backend':
      return 'backend'
    default:
      return 'global'
  }
}

/** `provider:anthropic:tenant:u-bogdan` -> `anthropic · u-bogdan`. */
export function poolLabel(name: string): string {
  if (name === 'global') return 'global'
  const parts = name.split(':')
  if (parts.length <= 2) return parts[1] ?? name
  // provider:X:tenant:Y
  return `${parts[1]} · ${parts[3] ?? parts[2]}`
}

/**
 * How many more agents of one runner profile could be admitted right now.
 *
 * The conjunction made arithmetic: a task must clear EVERY pool in its list at
 * the same moment, so headroom is the minimum across them -- never a sum --
 * and it is divided by the profile's weight because admission increments each
 * pool by `units`, not by one.
 *
 * TRAP D, and it is the most plausible misreading on the whole screen: the
 * `pools` list on a runner profile is the CALLING TENANT'S list, including for
 * an admin, because service.capacity() calls pool_names_for(tenant_id=
 * ctx.tenant_id) unconditionally. So this number answers "how many more could
 * I submit", never "how much capacity does the platform have". Every caller of
 * this function must render the tenant beside the number.
 *
 * A pool named in the profile but absent from the response is unconfigured,
 * which means unlimited by construction -- the global and tenant pools always
 * exist, so a missing one is a narrow named pool that was never capped. It is
 * skipped rather than treated as zero.
 */
export function headroomFor(
  profile: RunnerProfile,
  poolsByName: ReadonlyMap<string, Pool>,
): { agents: number; binding: string | null; missing: string[] } {
  let agents = Infinity
  let binding: string | null = null
  const missing: string[] = []

  for (const name of profile.pools) {
    const pool = poolsByName.get(name)
    if (!pool) {
      missing.push(name)
      continue
    }
    // A paused pool admits nothing at all, whatever its headroom says.
    if (pool.enabled === false) return { agents: 0, binding: name, missing }
    const units = profile.units > 0 ? profile.units : 1
    const fits = Math.floor(Math.max(0, pool.available) / units)
    if (fits < agents) {
      agents = fits
      binding = name
    }
  }

  return { agents: Number.isFinite(agents) ? agents : 0, binding, missing }
}

/**
 * Whether a pool's number is about the whole platform or only this tenant.
 *
 * Trap E: /v1/stats counts are tenant-scoped while the `global` pool is
 * platform-wide (service.py:276 vs :296-310). Putting "4 agents" from one
 * beside "7 units" from the other is a scope error dressed as a comparison.
 * So every number declares its scope and may only sit beside a number of the
 * same scope.
 */
export function poolScope(name: string): 'platform' | 'tenant' {
  const kind = poolKind(name)
  if (kind === 'tenant') return 'tenant'
  // provider:X:tenant:Y is the per-tenant slice of a provider.
  if (name.includes(':tenant:')) return 'tenant'
  return 'platform'
}

/**
 * The order pools are grouped in, matching `pool_names_for`
 * (models.py:82-107). The grouping teaches the CONJUNCTION -- a task must
 * clear every pool in its list at the same moment -- rather than hiding it.
 */
export const POOL_FAMILY_ORDER: readonly PoolKind[] = [
  'global', 'tenant', 'resource', 'runner', 'backend', 'provider',
]

/**
 * `active` above `effective_limit`. Not merely "full": it means the pool is
 * carrying more than its ceiling allows, which admission cannot produce and
 * which therefore indicates drift -- a limit lowered under running work, or a
 * counter that was never released. `make pool-check` exists to find this.
 */
export function overCeiling(pool: Pool): boolean {
  return pool.active > pool.effective_limit
}

/**
 * Which configured value is BINDING on this pool right now, in words. This is
 * the "set by" column: knowing a pool is capped at 6 is useless without
 * knowing whether that is the operator's number, AIMD backing off, or the
 * provider's quota.
 */
export function setBy(pool: Pool): { term: string; detail: string } {
  const { hard_limit, adaptive_target, quota_derived_limit, effective_limit } = pool
  if (quota_derived_limit !== null && quota_derived_limit === effective_limit && quota_derived_limit < hard_limit) {
    return { term: 'provider quota', detail: `Provider quota caps this at ${quota_derived_limit}; configured is ${hard_limit}.` }
  }
  if (adaptive_target !== null && adaptive_target === effective_limit && adaptive_target < hard_limit) {
    return { term: 'AIMD back-off', detail: `AIMD lowered this to ${adaptive_target} after provider errors; configured is ${hard_limit}.` }
  }
  return { term: 'configured', detail: `The configured hard limit of ${hard_limit} is what applies.` }
}

/**
 * Why a pool's ceiling is lower than its configured limit, or null if it is not.
 * Worth surfacing: a pool sitting at `effective_limit < hard_limit` is being
 * held down by something, and which something is the whole diagnosis.
 */
export function limitedBy(pool: Pool): 'adaptive' | 'quota' | null {
  if (pool.effective_limit >= pool.hard_limit) return null
  const adaptive = pool.adaptive_target
  const quota = pool.quota_derived_limit
  if (quota !== null && quota === pool.effective_limit) return 'quota'
  if (adaptive !== null && adaptive === pool.effective_limit) return 'adaptive'
  return null
}

// --------------------------------------------------------------------------
// Tasks and workflows
// --------------------------------------------------------------------------

/** `task_to_api` in codec.py. Trimmed to what the UI actually renders. */
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
  attempt_count: number
  max_attempts: number
  park_reason: ParkReason | string | null
  blocked_by: BlockedEntry[] | null
  workflow_id: string | null
  step_id: string | null
  depends_on: string[] | null
  cancel_requested: boolean
  repository_url: string | null

  // The rest of what task_to_api actually sends. This file declared 21 of its
  // 31 keys, and the ten below are exactly the ones the remaining screens are
  // built on -- last_error for the trouble board, result_summary for run
  // output, latest_checkpoint for the attempt timeline, next_eligible_at for
  // parked work.
  model: string | null
  timeout_seconds: number | null
  /** When a PARKED task becomes eligible again. Null unless it is parked. */
  next_eligible_at: string | null
  metadata: Record<string, unknown> | null
  repository_ref: string | null
  input: unknown
  last_error: string | null
  result_summary: Record<string, unknown> | null
  latest_checkpoint: string | null
}

/**
 * NOT on the task: `current_generation`.
 *
 * models.py carries it and it is meaningful -- it increments on admission AND
 * on a reconciler fence, so `current_generation > attempt_count` is exactly
 * "a stale worker was fenced out". task_to_api does not send it, so no screen
 * can show it and no screen should imply it. Attempt.generation is available
 * per attempt, which is a different and narrower thing.
 */

/** `_event_to_api`, routes/tasks.py. */
export interface TaskEvent {
  event_id: string
  task_id: string
  type: string
  at: string
  attempt_id: string | null
  lease_id: string | null
  generation: number | null
  detail: Record<string, unknown> | null
}

/**
 * What the worker puts in `task.result_summary` (lifecycle.py:946-960, written
 * through control.py:663-664).
 *
 * WHY THIS AND NOT `GET /v1/tasks/{id}/artifacts`: that route reads a
 * Firestore `artifacts` subcollection (store.py:513) that NOTHING IN THIS
 * REPOSITORY WRITES. `ARTIFACTS = "artifacts"` is declared once in swarm-api's
 * store and read once, and grep across apps/ finds no writer. The endpoint
 * therefore returns an empty list for every task forever. A panel built on it
 * would render "no artifacts" for a run that produced twenty.
 *
 * The artifacts are real -- they are uploaded to the tenant's own GCS prefix
 * and recorded here, by reference. Nothing is inlined and no download URL is
 * minted: the reader uses their own credentials against GCS, which keeps the
 * tenant boundary in one place.
 */
export interface ResultSummary {
  artifacts?: ArtifactRef[]
  artifact_bytes?: number
  /** `{stdout: "gs://...", stderr: "gs://..."}`. A stream that failed to upload is absent. */
  logs?: Record<string, string>
  /** Present only when something was dropped for exceeding max_artifact_bytes. */
  artifacts_skipped?: string[]
  checkpoint?: { checkpoint_id?: string; [k: string]: unknown }
  /**
   * What the agent did to the repository, and what happened to it.
   * `lifecycle.py::_harvest_git`. Absent when the task cloned nothing.
   *
   * THE FIELD THAT MATTERS MOST HERE IS `publish_reason`, and it is present on
   * success as well as failure. "No pull request" has at least six distinct
   * causes -- the token cannot push, the attempt parked, the agent changed
   * nothing, the forge is unknown, the push was rejected, the PR call failed --
   * and they need completely different responses. A panel that rendered a
   * missing PR as a single "none" would be the same failure the rest of this
   * UI is built to avoid.
   */
  git?: GitSummary
  /** Token and cost numbers live at `runner.usage`, untyped. See the note below. */
  runner?: { usage?: Record<string, number | string[]>; [k: string]: unknown }
  [k: string]: unknown
}

export interface GitCommit {
  sha: string
  subject: string
  author: string
  committed_at: string
  files_changed: number
  insertions: number
  deletions: number
  /** git prints `-` for both counts on a binary change; those are counted here
   *  instead of folded into a 0/0 line count that would read as "changed
   *  nothing". */
  binary_files: number
}

export interface GitSummary {
  /** The commit the clone landed on. Null when a resumed attempt lost its marker. */
  base?: string | null
  head?: string | null
  commits?: GitCommit[]
  commit_count?: number
  insertions?: number
  deletions?: number
  /** Changed-but-uncommitted paths. The COMMON case: most agents never commit. */
  dirty?: string[]
  dirty_count?: number
  dirty_truncated?: boolean
  /** Artifact name of the patch, matched against `artifacts[]` to get its URI. */
  patch?: string | null
  patch_bytes?: number
  /** True when a patch was produced and DISCARDED for exceeding the cap. A
   *  truncated patch applies cleanly and silently drops the rest of the
   *  change, so none is written. Not the same as "no patch". */
  patch_omitted?: boolean
  patch_note?: string
  note?: string
  error?: string
  repository?: string
  default_branch?: string | null
  can_push?: boolean
  branch?: string
  pushed_head?: string
  /** True when the WORKER made one of the commits, because the agent left work
   *  uncommitted and it would otherwise never have reached the branch. */
  auto_committed?: boolean
  published?: boolean
  publish_reason?: string
  pull_request?: { number: number; url: string; state: string; created: boolean }
}

export interface ArtifactRef {
  name: string
  bytes: number
  uri: string
}

/**
 * Reads `result_summary.runner.usage` if it is there.
 *
 * Three things must stay true of anything rendered from this:
 *  - only claude-code and codex produce it; mock, generic and browser report
 *    nothing, and NOTHING IS NOT ZERO. Render an em dash, never $0.00.
 *  - result_summary is written only by finish(), so a RUNNING agent has no
 *    figure and a PARKED one never will for the attempt it lost -- that path
 *    puts the summary in the event detail and in blocked_by instead.
 *  - it is an untyped dict, so nothing can query or index it. A "top spenders"
 *    figure is a client-side sum over one page, not an aggregate.
 */
export function usageOf(task: Task): Record<string, number | string[]> | null {
  const runner = (task.result_summary as ResultSummary | null)?.runner
  const usage = runner?.usage
  return usage && typeof usage === 'object' ? usage : null
}

/** `GET /v1/tenants/me`, routes/tenants.py:24-38. */
export interface Me {
  tenant: Tenant
  principal: {
    email: string
    domain: string
    groups: string[]
    is_admin: boolean
  }
}

/**
 * A row-bounded window of tasks, plus what span those rows turned out to
 * cover.
 *
 * THE ONE DESIGN RULE for activity: bound by ROWS, label by the SPAN those
 * rows actually covered. The platform can serve "the most recent N tasks for
 * this tenant" cheaply and exactly; it cannot serve "everything in August" at
 * all -- list_tasks applies exactly one inequality, decoded from the page
 * token. So the control says "Last 500 tasks" and the header reports
 * "14 Sep 09:12 -> 19 Sep 08:44 (4d 23h)". If the tenant is busy that span is
 * six hours; if quiet, three months. Both are true. "Last 7 days" would be a
 * lie the moment the window truncates.
 */
export interface TaskWindow {
  tasks: Task[]
  /** True when a next_page_token remained, so older tasks exist beyond this. */
  moreExist: boolean
  /** How many pages were actually fetched, for the provenance line. */
  pages: number
  /** Oldest and newest created_at in the window, or null when empty. */
  from: string | null
  to: string | null
}

export type Bucket = 'hour' | 'day' | 'week' | 'month'

/**
 * Floor a timestamp to a bucket boundary IN THE VIEWER'S LOCAL ZONE.
 *
 * A day boundary silently in UTC moves seven hours of a Pacific engineer's
 * work into the wrong day, which is the kind of wrong that never gets
 * reported because it looks plausible.
 */
export function bucketStart(iso: string, bucket: Bucket): number | null {
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return null
  const d = new Date(t)
  d.setMinutes(0, 0, 0)
  if (bucket === 'hour') return d.getTime()
  d.setHours(0)
  if (bucket === 'day') return d.getTime()
  if (bucket === 'week') {
    // ISO-8601: weeks start Monday.
    const dow = (d.getDay() + 6) % 7
    d.setDate(d.getDate() - dow)
    return d.getTime()
  }
  d.setDate(1)
  return d.getTime()
}

/**
 * Usage is only meaningful when PRESENT AND NON-EMPTY. `?? 0` here would
 * render "this engineer spent nothing", which is worse than omitting the
 * number -- a zero reads as a fact.
 */
export function hasUsage(task: Task): boolean {
  const u = usageOf(task)
  return u !== null && Object.keys(u).length > 0
}

/**
 * `lease_to_api`. One row of "who is holding capacity right now".
 *
 * NOTE `dispatch_state`, not `state`. Nothing ever writes STARTING or RUNNING
 * to a lease -- the worker advances the TASK through those and touches the
 * lease only to heartbeat -- so a column labelled "state" would show
 * DISPATCHED for an agent that has been running for an hour.
 */
export interface LeaseRow {
  lease_id: string
  task_id: string
  attempt_id: string
  tenant_id: string
  generation: number
  pools: string[]
  units: number
  dispatch_state: 'LEASED' | 'DISPATCHED' | string
  created_at: string
  dispatch_deadline: string
  expires_at: string
  heartbeat_at: string | null
  released_at: string | null
  release_reason: string | null
  released: boolean
  expired: boolean
  dispatch_overdue: boolean
  /** Computed server-side. Falls back to created_at when never beaten. */
  silent_seconds: number
  heartbeat_ever: boolean
  /** Denormalised from the task, so this is not an N+1. */
  last_error: string | null
}

/**
 * THE THRESHOLDS ARRIVE WITH THE DATA and the UI must colour from them.
 *
 * 90 and 120 are the reconciler's, and the grace is not even a constant --
 * it derives from the heartbeat interval. A `const GRACE = 90` here would be
 * the restatement drift check-contract-parity.sh exists to catch, one layer
 * further out, and it would colour a row amber at a threshold the reconciler
 * does not act on.
 */
export interface LeasePage {
  leases: LeaseRow[]
  thresholds: { heartbeat_grace_seconds: number; lease_timeout_seconds: number }
  evaluated_at: string
  active_only: boolean
  tenant_id: string | null
  /** Weighted units, not agents. */
  units_held: number
}

/** How a lease row reads, given the thresholds the API just sent. */
export type Liveliness = 'alive' | 'silent' | 'presumed-dead'

export function leaseLiveliness(
  row: LeaseRow,
  thresholds: LeasePage['thresholds'],
): { kind: Liveliness; copy: string } {
  // Past expires_at is a SECOND, INDEPENDENT signal, not a later stage of the
  // first: the lease TTL has run out as well as the heartbeat going quiet.
  if (row.expired) {
    return {
      kind: 'presumed-dead',
      copy: 'The lease TTL has run out as well. This slot is held by something that is almost certainly gone.',
    }
  }
  if (row.silent_seconds >= thresholds.heartbeat_grace_seconds) {
    return {
      kind: 'silent',
      copy: 'This is already the reconciler\u2019s trigger. Its next pass will reclaim this lease, and that pass is up to five minutes away.',
    }
  }
  return { kind: 'alive', copy: 'Beating normally. Nothing will touch this.' }
}

/** `attempt_to_api`. The per-attempt record result_summary cannot give you. */
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
  peak_rss_bytes: number | null
  peak_disk_bytes: number | null
  oom_near_miss: boolean
  checkpoints: string[]
  input_tokens: number | null
  output_tokens: number | null
  cache_read_input_tokens: number | null
  cache_creation_input_tokens: number | null
  cost_usd: number | null
}

/** `SubmissionService.stats`, service.py:271-288. */
export interface Stats {
  tenant_id: string
  /** One key per TaskState -- count_tasks_by_state iterates the whole enum. */
  tasks_by_state: Record<string, number>
  dispatch_paused: boolean
  limits: Record<string, number>
  generated_at: string
  /** ADMIN ONLY. Absent for everyone else -- absent is not zero. */
  platform_tasks_by_state?: Record<string, number>
}

/** `GET /v1/admin/dispatch`, routes/admin.py:70-75. Admin-gated. */
export interface DispatchControl {
  dispatch_paused: boolean
  updated_at: string | null
  updated_by: string | null
  reason: string | null
}

/** `SubmissionService.providers`, service.py:334-350. */
export interface ProviderEntry {
  provider: string
  /** Provider NAMES only, from tenants/{id}.credentials. Never key material. */
  credential_registered: boolean
  runner_profiles: string[]
  /** null when no quota document exists for this tenant yet. Not zeros. */
  quota: QuotaState | null
}

export interface ProvidersPage {
  tenant_id: string
  providers: ProviderEntry[]
  generated_at: string
}

/**
 * `ProviderState`, models.py:259-265. SIX values, not five.
 *
 * THROTTLED is the one most easily missed and it is the common case during a
 * squeeze, so a chip that falls through to "unknown" for it mislabels exactly
 * the condition the panel exists for.
 *
 * UNKNOWN means "no worker has reported on this provider recently". That is
 * not the same as healthy and gets its own grey.
 */
export type ProviderStateName =
  | 'AVAILABLE' | 'THROTTLED' | 'EXHAUSTED' | 'COOLDOWN' | 'DISABLED' | 'UNKNOWN'

export function providerTone(state: string): Tone | 'unknown' {
  switch (state) {
    case 'AVAILABLE': return 'ok'
    case 'THROTTLED': return 'wait'
    case 'COOLDOWN': return 'wait'
    case 'EXHAUSTED': return 'bad'
    case 'DISABLED': return 'bad'
    default: return 'unknown'
  }
}

/**
 * The two park reasons that need a person. Everything else is the platform
 * working as designed (CONTRACT.md invariant 1), which is why this panel is
 * grey and never red: parked work costs nothing.
 */
export const NEEDS_A_HUMAN: ReadonlySet<string> = new Set([
  'CREDENTIAL_MISSING', 'BUDGET_EXHAUSTED',
])

/**
 * Reasons that are NOT BlockedReason members. `record_blockers` stores
 * whatever AdmissionDenied carried, and three of those are bare strings from
 * the pre-flight checks (admission.py:148,155,157). They mean something quite
 * different from a capacity refusal, so they get their own group rather than
 * being swallowed by an enum lookup.
 */
export const PRE_CAPACITY_REASONS: ReadonlySet<string> = new Set([
  'task_missing', 'not_ready', 'cancel_requested',
])

/** `tenant_to_api`, codec.py:283. Thirteen fields, all of them. */
export interface Tenant {
  tenant_id: string
  kind: 'group' | 'user' | string
  principal: string
  display_name: string | null
  created_at: string
  max_active: number
  capacity_units: number
  monthly_budget_usd: number | null
  enabled: boolean
  /** Providers this tenant has registered a key for. */
  credentials: string[]
  /**
   * null means NO IDENTITY, not an empty string. Render it as such -- a blank
   * cell here reads as "fine" and it is the opposite.
   */
  service_account: string | null
  gcs_prefix: string | null
  namespace: string | null
}

/**
 * `quota_to_api` = asdict(QuotaState) + the state enum value + effective_limit.
 * Every nullable field here is genuinely unknown rather than zero, which is
 * why they are typed `| null` and must render as an em dash.
 */
export interface QuotaState {
  provider: string
  tenant_id: string
  state: 'HEALTHY' | 'COOLDOWN' | 'EXHAUSTED' | 'DISABLED' | string
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
}

/**
 * `swarm_common.states`. Twelve states, and the groupings below are the ones
 * that carry meaning rather than being pretty categories.
 */
export type TaskState =
  | 'SUBMITTED' | 'QUEUED' | 'PARKED' | 'READY' | 'LEASED' | 'DISPATCHED'
  | 'STARTING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED' | 'DEAD_LETTERED'

/**
 * Three of the twelve are never written to a task document. `service.py:122`
 * walks the real state machine but stores only the end state, creating tasks
 * directly at READY or PARKED; QUEUED is the creation state of a WORKFLOW, not
 * a task; and `finish()` is only ever called with SUCCEEDED / FAILED /
 * CANCELLED, so DEAD_LETTERED is an enum member, two membership tests and an
 * unreachable branch.
 *
 * So these must never appear in a filter, a legend or a histogram. A bucket
 * that cannot fill reads as "nothing is broken" rather than "this cannot
 * happen", which is the wrong lesson. DEAD_LETTERED must still be DECODED --
 * the scheduler lists it in _FAILED_PARENT_STATES -- but decoding a string is
 * not the same as offering it as a choice.
 */
export const NEVER_WRITTEN: ReadonlySet<TaskState> = new Set<TaskState>([
  'SUBMITTED', 'QUEUED', 'DEAD_LETTERED',
])

/** The nine a task document can actually hold. Filters and legends use this. */
export const REAL_STATES: readonly TaskState[] = [
  'READY', 'PARKED', 'LEASED', 'DISPATCHED', 'STARTING', 'RUNNING',
  'SUCCEEDED', 'FAILED', 'CANCELLED',
]

/**
 * CONTRACT.md invariant 1 and 3: only these four consume capacity. QUEUED,
 * PARKED and READY cost nothing. A UI that colours "in flight" by intuition
 * rather than by this set teaches the wrong mental model of what costs money.
 */
export const CONCURRENCY_STATES: ReadonlySet<TaskState> = new Set<TaskState>([
  'LEASED', 'DISPATCHED', 'STARTING', 'RUNNING',
])

export const TERMINAL_STATES: ReadonlySet<TaskState> = new Set<TaskState>([
  'SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED',
])

export type Tone = 'ok' | 'bad' | 'live' | 'wait'

export function stateTone(state: TaskState): Tone {
  if (state === 'SUCCEEDED') return 'ok'
  if (state === 'FAILED' || state === 'DEAD_LETTERED') return 'bad'
  if (CONCURRENCY_STATES.has(state)) return 'live'
  return 'wait'
}

/**
 * Colour is never the only signal. Roughly 8% of male viewers cannot separate
 * PARKED amber from FAILED red, so every chip carries a glyph and the state
 * word as well.
 */
export function stateGlyph(state: TaskState): string {
  switch (state) {
    case 'SUCCEEDED': return '\u2713'
    case 'FAILED': return '\u2717'
    case 'CANCELLED': return '\u2300'
    case 'PARKED': return '\u23f8'
    case 'READY': return '\u25cb'
    default: return '\u25cf'
  }
}

/**
 * `models.py:175` -- `list[dict[str, Any]]`, NOT a list of strings. This file
 * used to say `string[]`, which would have rendered "[object Object]" the
 * first time a blocked task appeared.
 *
 * Two unrelated writers produce it and the shapes differ:
 *
 *  - Admission denial (`admission.py:97-114`) always writes all four keys, on
 *    both the at-limit and the paused-pool branch:
 *      { pool, reason, limit, active }
 *  - A worker park (`agent_worker/control.py:595-602`) writes
 *      { reason, ...detail }
 *    where `detail` is arbitrary. On the interrupted path it is the ENTIRE
 *    artifact/log/checkpoint summary, so an entry can legitimately contain an
 *    `artifacts` array.
 *
 * So: key off `reason`, treat everything else as optional enrichment, and
 * never render the entry as JSON -- that is how a paragraph of GCS URIs ends
 * up inside a table cell.
 */
export interface BlockedEntry {
  reason: string
  pool?: string
  limit?: number
  active?: number
  [key: string]: unknown
}

/** `ParkReason`, states.py:125-137. All eight are really written. */
export type ParkReason =
  | 'PROVIDER_QUOTA_EXHAUSTED' | 'PROVIDER_COOLDOWN' | 'PROVIDER_OUTAGE'
  | 'SCHEDULED_RETRY' | 'DEPENDENCY_INCOMPLETE' | 'MANUAL_PAUSE'
  | 'BUDGET_EXHAUSTED' | 'CREDENTIAL_MISSING'

/**
 * Copy for every reason that is ever actually written -- the seven from
 * admission plus the eight ParkReasons.
 *
 * `BlockedReason.BUDGET_LIMIT`, `QUOTA_EXHAUSTED`, `COOLDOWN`, `DEPENDENCY`
 * and `SCHEDULED_RETRY` are members of the enum that nothing ever writes as a
 * blocker, so they are deliberately absent: a legend promising them describes
 * a platform that does not exist.
 *
 * The distinction worth preserving is whose problem it is. TENANT_LIMIT means
 * you are at your own ceiling and only an admin can move it; the global and
 * resource-class limits mean the platform is busy and waiting is the answer.
 * That is why the field is surfaced verbatim rather than collapsed to "busy".
 */
export const REASON_COPY: Readonly<Record<string, string>> = {
  GLOBAL_CONCURRENCY_LIMIT: 'The platform is at its overall limit. Waiting is the answer.',
  TENANT_LIMIT: 'Your tenant is at its own limit. Only an admin can raise it.',
  PROVIDER_CONCURRENCY_LIMIT: 'This provider is at its concurrency limit.',
  RESOURCE_CLASS_LIMIT: 'This resource class is busy platform-wide.',
  RUNNER_LIMIT: 'This runner profile is at its limit.',
  BACKEND_LIMIT: 'This backend is at its limit.',
  MANUAL_PAUSE: 'An operator paused this pool. It will not admit work until resumed.',
  PROVIDER_QUOTA_EXHAUSTED: 'The provider quota is spent. Parked until it resets.',
  PROVIDER_COOLDOWN: 'Backing off after provider errors.',
  PROVIDER_OUTAGE: 'The provider is unavailable.',
  SCHEDULED_RETRY: 'Waiting for a scheduled retry.',
  DEPENDENCY_INCOMPLETE: 'Waiting on an earlier step in its workflow.',
  BUDGET_EXHAUSTED: 'The budget for this work is spent.',
  CREDENTIAL_MISSING: 'No provider key is registered for this tenant.',
}

/**
 * Copy for a reason, falling back to the RAW STRING rather than blanking the
 * cell. `codec.blocked_reason_values()` exists but is wired to no route, so
 * this table is shipped client-side and will go stale; printing the unknown
 * value is what makes that visible instead of silent.
 */
export function reasonCopy(reason: string): string {
  return REASON_COPY[reason] ?? reason
}

/** The one-line "why is this not running" for a task, or null if it is. */
export function whyNotRunning(task: Task): string | null {
  const first = task.blocked_by?.[0]
  if (first?.reason) {
    const where = first.pool ? ` (${first.pool})` : ''
    const at =
      typeof first.limit === 'number' && typeof first.active === 'number'
        ? ` -- ${first.active} of ${first.limit} in use`
        : ''
    return `${reasonCopy(first.reason)}${where}${at}`
  }
  if (task.park_reason) return reasonCopy(task.park_reason)
  return null
}

/**
 * `GET /v1/tasks`, routes/tasks.py:64-95.
 *
 * The key is `next_page_token`. This file said `next_cursor`, which the API
 * has never sent, so paging could never have advanced past the first page.
 *
 * `tenant_id` comes back too and is worth keeping: every row is the caller's
 * own tenant, so the scope belongs in the page header once rather than in a
 * column on every row.
 */
export interface TaskPage {
  tasks: Task[]
  next_page_token?: string | null
  tenant_id?: string
}

/**
 * The frozen catalogue, profiles.py:77-81. Bundled client-side because it is
 * frozen and three entries long; the alternative is a request per render to
 * learn something that cannot change without a contract change.
 */
export const RESOURCE_UNITS: Readonly<Record<string, number>> = {
  standard: 1,
  browser: 2,
  large: 4,
}

/**
 * Column 3, "Why". First match wins.
 *
 * The CANCELLED cascade case is the subtle one. When a workflow parent fails,
 * the scheduler cancels the child (scheduler/loop.py:438-442). Do not
 * string-match its message -- compute it. In this table the parents' states
 * are not loaded, only `depends_on` ids, so the discriminator that works with
 * what the row actually has is `cancel_requested`: the scheduler's cascade
 * writes state, park_reason, blocked_by, completed_at and last_error and
 * NEVER sets cancel_requested (scheduler/store.py:303-323), while every human
 * cancel does, including a whole-workflow cancel which fans out through
 * request_cancel per step. That survives an upstream copy change.
 */
export function whyAgent(task: Task): string {
  if (task.state === 'PARKED') {
    const base = task.park_reason ? reasonCopy(task.park_reason) : 'Parked.'
    return task.next_eligible_at ? `${base} Eligible again ${task.next_eligible_at}.` : base
  }
  if (task.state === 'READY' && task.blocked_by?.length) {
    const b = task.blocked_by[0]
    if (!b) return ''
    // Only show the fraction when BOTH keys are present. An admission blocker
    // always carries them; a worker park's arbitrary detail may not.
    const at =
      typeof b.active === 'number' && typeof b.limit === 'number'
        ? ` (${b.active}/${b.limit})`
        : ''
    return `${reasonCopy(b.reason)}${at}`
  }
  if (task.state === 'FAILED') return task.last_error ?? 'Failed.'
  if (task.state === 'CANCELLED') {
    if (!task.cancel_requested && task.depends_on?.length) {
      return 'An upstream step did not succeed.'
    }
    return 'Cancelled by request.'
  }
  return ''
}

/**
 * Column 6, "Elapsed".
 *
 * `started_at` is written on DISPATCHED -> STARTING, so a LEASED or
 * DISPATCHED task legitimately has none. That must read "queued 4m", never
 * "0s" and never "Invalid Date" -- and `completed_at` can be null on a row
 * that went terminal between two polls, so the terminal branch cannot assume
 * it.
 */
export function elapsed(task: Task, now: number): { text: string; ticking: boolean } {
  const ms = (v: string | null) => (v ? new Date(v).getTime() : NaN)
  const created = ms(task.created_at)
  const started = ms(task.started_at)
  const completed = ms(task.completed_at)

  if (Number.isFinite(completed)) {
    const from = Number.isFinite(started) ? started : created
    if (!Number.isFinite(from)) return { text: '\u2014', ticking: false }
    return { text: duration(completed - from), ticking: false }
  }
  if (Number.isFinite(started)) return { text: duration(now - started), ticking: true }
  if (Number.isFinite(created)) return { text: `queued ${duration(now - created)}`, ticking: true }
  return { text: '\u2014', ticking: false }
}

function duration(msSpan: number): string {
  const s = Math.max(0, Math.round(msSpan / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

/**
 * The computed workflow rollup, NEVER `workflow.state`. That field is written
 * once as QUEUED at service.py:258 and no component ever updates it --
 * nothing in apps/scheduler, apps/reconciler or apps/agent-worker writes the
 * workflows collection at all. Rendering it would label every finished
 * workflow "queued" forever.
 */
export function rollupState(states: TaskState[]): 'running' | 'succeeded' | 'failed' | 'waiting' {
  if (states.some((s) => CONCURRENCY_STATES.has(s))) return 'running'
  if (states.some((s) => s === 'FAILED' || s === 'CANCELLED')) return 'failed'
  if (states.length > 0 && states.every((s) => s === 'SUCCEEDED')) return 'succeeded'
  return 'waiting'
}

/** `workflow_to_api`, codec.py:343-366. Every field it actually sends. */
export interface Workflow {
  workflow_id: string
  tenant_id: string
  state: string
  created_at: string
  updated_at: string
  submitted_by: string | null
  priority: number
  on_step_failure: string
  cancel_requested: boolean
  steps: WorkflowStep[]
}

/**
 * codec.py:354-365. NOTE WHAT IS NOT HERE: there is no `state`.
 *
 * This file used to declare one, and the DAG view coloured its nodes from it.
 * The fixtures supplied it, so it looked right in development and would have
 * rendered every node grey and "unknown" against the real API -- the exact
 * failure docs/web-ui warns about, a field no live code path writes.
 *
 * A step's state is DERIVED: join `task_id` to the task documents that
 * `GET /v1/workflows/{id}` returns alongside the workflow
 * (routes/workflows.py:50-57), or, for the list screen, to a task page. Use
 * `stepState()` below so the join is in one place and its failure is visible.
 */
export interface WorkflowStep {
  step_id: string
  runner_profile: string
  resource_class: string
  /** The DAG edges. Real ones -- this is a tree, not a star. */
  depends_on: string[]
  input_from: string | null
  timeout_seconds?: number | null
  task_id?: string | null
  input?: unknown
}

/**
 * The derived state of a step, and the three ways it can be absent -- which
 * are not the same thing and must not render alike:
 *
 *  - `unstarted`: the step has no task_id. The workflow has not reached it.
 *  - `unknown`:   it has a task_id we have no task document for. Either the
 *                 task page did not include it, or the fetch failed. The UI
 *                 must SAY the state could not be read, never imply idleness.
 *  - a TaskState: the join succeeded.
 */
export type StepState =
  | { kind: 'unstarted' }
  | { kind: 'unknown'; taskId: string }
  | { kind: 'state'; state: TaskState; task: Task }

export function stepState(
  step: WorkflowStep,
  taskById: ReadonlyMap<string, Task> | null,
): StepState {
  if (!step.task_id) return { kind: 'unstarted' }
  const task = taskById?.get(step.task_id)
  if (!task) return { kind: 'unknown', taskId: step.task_id }
  return { kind: 'state', state: task.state, task }
}

// ---------------------------------------------------------------------------
// The account pool  (Settings -> Accounts)
// ---------------------------------------------------------------------------
// Shapes returned by `account_to_api` in
// apps/quota-broker/quota_broker/main.py, which swarm-api proxies verbatim.
//
// NO KEY MATERIAL APPEARS HERE, and there is deliberately no field for its
// length either. `Credential.redacted()` exposes `access_token_len`, which is
// fine in a debug log and is not fine in a browser: a length is a real hint
// about a secret and nothing on this screen needs it. If a field like that
// ever turns up in the payload, it does not get a home in this file.

/** `AccountState` in quota_broker/accounts.py. Four members, no others. */
export type AccountStateName = 'AVAILABLE' | 'PAUSED' | 'DRAINING' | 'REAUTH_REQUIRED'

/**
 * One rate-limit window as the provider reported it.
 *
 * `utilization` is 0-1, NOT a percentage -- 0.87 is 87%. Multiplying is this
 * file's job precisely once, in `readingOf`, so no component can forget.
 */
export interface AccountWindow {
  utilization: number
  resets_at: string
  /**
   * The server's own answer to "has this window already passed its reset",
   * computed at response time. Trusted rather than recomputed here: the
   * browser's clock is not the platform's, and a reading that flips between
   * reset and not-reset depending on whose clock is read is worse than either.
   */
  reset: boolean
}

export interface Account {
  /** `<owner_tenant>:<label>`. Opaque; never split for display. */
  account_id: string
  owner_tenant: string
  label: string
  provider: string
  /** Widened to `string`: an unknown state must render as unknown, not crash. */
  state: AccountStateName | string
  /** Why it is in that state, written for the human who has to act on it. */
  reason: string
  /** Tenants the owner has explicitly lent this account to. Empty is the default. */
  lend_to: string[]
  /** Agents currently holding it. Advisory -- the lease is authoritative. */
  assigned: number
  /**
   * Keyed by the PROVIDER's window names, not a fixed pair. accounts.py:143
   * says so explicitly: "the windows are the provider's to define, and a new
   * one appearing must not need a schema change to be recorded". So this
   * screen reads `five_hour` and `seven_day` by name for its two columns and
   * lists anything else it finds rather than dropping it on the floor.
   */
  windows: Record<string, AccountWindow>
  /** When a reading last arrived. NULL MEANS NONE HAS, which is not zero. */
  observed_at: string | null
  /**
   * True when no reading is recent enough to trust (DEFAULT_STALE_AFTER, 30
   * minutes). Computed server-side because a reading's age is what decides
   * whether to believe it, and the server owns the clock.
   */
  stale: boolean
}

/** `GET /v1/accounts`. `tenant_id` is the scope the server actually applied. */
export interface AccountsPage {
  accounts: Account[]
  /** null when a platform caller asked for every tenant. */
  tenant_id: string | null
}

/** `RefreshOutcome.as_dict()` in quota_broker/credentials.py. */
export interface RefreshResult {
  tenant_id: string
  provider: string
  /** THE ONLY FIELD THAT SAYS IT WORKED. The HTTP status does not. */
  refreshed: boolean
  /** One of the nine reasons in credentials.py. Never rendered raw. */
  reason: string
  expires_at: string | null
}

/** `POST /v1/accounts/{id}/refresh`. A 200 carries a FAILED refresh too. */
export interface RefreshResponse {
  refresh: RefreshResult
  account: Account
}

export const FIVE_HOUR = 'five_hour'
export const SEVEN_DAY = 'seven_day'

/**
 * Colour for a state chip.
 *
 * Returns its own union rather than `Tone`, because two of these are not tones
 * the rest of the app has: PAUSED is deliberate and gets the purple every
 * other paused thing here gets, and an unrecognised state gets grey rather
 * than falling through to a colour that would assert something.
 */
export type AccountTone = 'ok' | 'paused' | 'wait' | 'bad' | 'unknown'

export function accountTone(state: string): AccountTone {
  switch (state) {
    case 'AVAILABLE': return 'ok'
    case 'PAUSED': return 'paused'
    case 'DRAINING': return 'wait'
    case 'REAUTH_REQUIRED': return 'bad'
    default: return 'unknown'
  }
}

/** The one state only a person can clear. The sweep stops trying on it. */
export function needsAHuman(a: Account): boolean {
  return a.state === 'REAUTH_REQUIRED'
}

/**
 * What is actually known about one window, as five cases that must not render
 * alike.
 *
 * This is the whole screen in one function. A missing reading is not zero; a
 * stale reading is not a current one; a window that has already reset is
 * describing a window that no longer exists. `cs status` marks the last two
 * with `~` for exactly this reason, and a figure printed without that mark is
 * a claim that it is current.
 */
export type AccountReading =
  /** No reading has EVER arrived for this account. Not zero -- unmeasured. */
  | { kind: 'never' }
  /** Readings exist, but not for this window. The provider did not report it. */
  | { kind: 'absent' }
  /** The window passed its reset, so the figure describes a window that refilled. */
  | { kind: 'reset'; pct: number; resetsAt: string }
  /** A real figure, too old to trust. Shown, marked, never presented as current. */
  | { kind: 'stale'; pct: number; resetsAt: string; observedAt: string }
  /** Measured, recent, and safe to read as a fact. */
  | { kind: 'live'; pct: number; resetsAt: string; observedAt: string }

export function readingOf(a: Account, key: string): AccountReading {
  if (a.observed_at === null) return { kind: 'never' }
  const w = a.windows[key]
  if (!w || typeof w.utilization !== 'number' || !Number.isFinite(w.utilization)) {
    return { kind: 'absent' }
  }
  const pct = Math.max(0, Math.min(100, w.utilization * 100))
  // `reset` outranks `stale`: a fresh reading of a window that has since
  // refilled is still describing the window before it.
  if (w.reset) return { kind: 'reset', pct, resetsAt: w.resets_at }
  if (a.stale) return { kind: 'stale', pct, resetsAt: w.resets_at, observedAt: a.observed_at }
  return { kind: 'live', pct, resetsAt: w.resets_at, observedAt: a.observed_at }
}

/** True for the two cases `cs status` prefixes with `~`. */
export function isProjected(r: AccountReading): boolean {
  return r.kind === 'stale' || r.kind === 'reset'
}

/**
 * The window that will actually stop you, which is what CLEARS is about.
 *
 * The BINDING window, not the five-hour and not an average: an account at 5%
 * on its five-hour and 90% on its weekly is stopped by the weekly, and
 * averaging them to 47% would send agents at an account that is about to
 * refuse. `Account._binding_remaining` in accounts.py picks the same way, and
 * treats a window past its reset as full again -- so a window that has reset
 * cannot be the binding one while another still has room.
 *
 * Returns null when there is nothing to pick from, which is a real answer:
 * CLEARS then renders as an em dash rather than a time nobody measured.
 */
export function bindingWindow(a: Account): { key: string; window: AccountWindow } | null {
  let best: { key: string; window: AccountWindow } | null = null
  let leastRemaining = Infinity
  for (const [key, w] of Object.entries(a.windows)) {
    if (!w || typeof w.utilization !== 'number' || !Number.isFinite(w.utilization)) continue
    const remaining = w.reset ? 1 : Math.max(0, 1 - w.utilization)
    if (remaining < leastRemaining) {
      leastRemaining = remaining
      best = { key, window: w }
    }
  }
  return best
}

/**
 * A duration the way somebody reads a clock, not the way a computer counts it.
 *
 * Ported field-for-field from `shortDur` in claudeswitch's
 * internal/render/status.go, including the zero-padded minutes, because this
 * column and that one are meant to be the same column. "81h49m" is arithmetic;
 * "3d 10h" is an answer.
 */
export function humaniseUntil(ms: number): string {
  if (!Number.isFinite(ms)) return '—'
  if (ms <= 0) return 'now'
  const s = Math.floor(ms / 1000)
  const m = Math.floor(s / 60)
  const h = Math.floor(m / 60)
  if (h >= 48) return `${Math.floor(h / 24)}d ${h % 24}h`
  if (h >= 1) return `${h}h ${String(m % 60).padStart(2, '0')}m`
  if (m >= 1) return `${m}m`
  return `${s}s`
}

/** Time until an ISO instant, humanised. Null in, em dash out. */
export function clearsIn(iso: string | null | undefined, now: number): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return '—'
  return humaniseUntil(t - now)
}

/**
 * The five-cell bar, as `miniBar` in claudeswitch's internal/render/width.go
 * draws it: round to the nearest fifth, clamp, `▰` filled and `▱`
 * empty.
 *
 * Returned as a count rather than a string so the component can draw real
 * elements -- a screen reader hearing five geometric-shape glyphs learns
 * nothing, and the percentage beside it is the accessible version of the
 * same fact.
 */
export const BAR_CELLS = 5

export function barFilled(pct: number): number {
  const filled = Math.round((pct / 100) * BAR_CELLS)
  return Math.max(0, Math.min(BAR_CELLS, filled))
}
