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
