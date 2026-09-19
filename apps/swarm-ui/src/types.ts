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

export interface TaskPage {
  tasks: Task[]
  next_cursor?: string | null
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
