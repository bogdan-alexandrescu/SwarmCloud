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
  park_reason: string | null
  blocked_by: string[] | null
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
  | 'QUEUED' | 'READY' | 'LEASED' | 'DISPATCHED' | 'STARTING' | 'RUNNING'
  | 'PARKED' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED' | 'DEAD_LETTERED' | 'BLOCKED'

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

export function stateTone(state: TaskState): 'ok' | 'bad' | 'live' | 'wait' {
  if (state === 'SUCCEEDED') return 'ok'
  if (state === 'FAILED' || state === 'DEAD_LETTERED') return 'bad'
  if (CONCURRENCY_STATES.has(state)) return 'live'
  return 'wait'
}

export interface TaskPage {
  tasks: Task[]
  next_cursor?: string | null
}

/** `workflow_to_api`. */
export interface Workflow {
  workflow_id: string
  tenant_id: string
  state: string
  created_at: string
  updated_at: string
  submitted_by: string | null
  steps: WorkflowStep[]
}

export interface WorkflowStep {
  step_id: string
  runner_profile: string
  resource_class: string
  /** The DAG edges. Real ones -- this is a tree, not a star. */
  depends_on: string[]
  input_from: string | null
  task_id?: string | null
  state?: string | null
}
