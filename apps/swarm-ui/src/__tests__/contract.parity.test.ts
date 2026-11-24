// THE CONSOLE'S RESTATEMENTS OF THE FROZEN CONTRACT, HELD TO THE GENERATED
// CONSTANTS (part of #72).
//
// These were section 5 of scripts/lib/check-contract-parity.sh, which read
// types.ts and outcomes.ts as text with regular expressions and compared what
// it found with `swarm_common`. `scripts/gen_api_types.py` now writes
// `src/generated/constants/states.json` from the live Python objects
// (scripts/api_constants/states.py), and `--check` (CI, and the worker's
// pre-publish gate) fails while that file differs from them. So comparing the
// console with the JSON compares it with the frozen contract, and no test
// reads the other language's source.
//
// WHAT EACH CASE HOLDS, BOTH WAYS (an extra member fails as a missing one does):
//   * RESOURCE_UNITS against the catalogue's code-default unit weights;
//   * the task states: the TaskState union, CONCURRENCY_STATES,
//     TERMINAL_STATES, and REAL_STATES/NEVER_WRITTEN partitioning them;
//   * ParkReason, ProviderStateName, EndCause and END_CAUSES (in order);
//   * the pool families: PoolKind, POOL_FAMILY_ORDER, what poolKind parses
//     off each name pool_names_for gives, and providerTenantPool's spelling;
//   * QUOTA_SWEEP_INTERVAL_SECONDS against the interval states.py states
//     (check-contract-parity.sh holds that one to the Terraform cron);
//   * the shapes: the generated Task's parent_* fields, AttemptRow's
//     credential fields typed with CredentialTier, CredentialTier itself,
//     WorkflowStep.input_from as the API's string map, and CancelCauseKey
//     counting child_cascade.
//
// A UNION HAS NO RUN-TIME VALUE, so it is spelled out once here as a
// `Record<Union, true>`: `tsc` (`npm run typecheck`, which includes this
// directory) refuses the literal when the union gains or loses a member, and
// vitest compares the literal's keys with the JSON. A field of a type is held
// by a type-level assertion that compiles only while the field is there.
//
// MUTATION: change RESOURCE_UNITS.large to 3, drop 'CANCELLED' from
// REAL_STATES, add a member to END_CAUSES, or drop 'api_key' from
// CredentialTier, and a case here (or tsc on this file) fails.

import { describe, expect, it } from 'vitest'

import statesConstants from '../generated/constants/states.json'
import type { Task as ApiTask, WorkflowStepCreate } from '../generated/api-types'
import type { CancelCauseKey } from '../outcomes'
import {
  CONCURRENCY_STATES,
  END_CAUSES,
  NEVER_WRITTEN,
  PARK_REASONS,
  POOL_FAMILY_ORDER,
  QUOTA_SWEEP_INTERVAL_SECONDS,
  REAL_STATES,
  RESOURCE_UNITS,
  TERMINAL_STATES,
  poolKind,
  providerTenantPool,
  type AttemptRow,
  type CredentialTier,
  type EndCause,
  type ParkReason,
  type PoolKind,
  type ProviderStateName,
  type Task,
  type TaskState,
  type WorkflowStep,
} from '../types'

const sorted = (values: Iterable<string>): string[] => [...values].sort()

type Same<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false
type Has<T, K extends PropertyKey> = K extends keyof T ? true : false
type Present<T, K extends keyof T> = Record<string, never> extends Pick<T, K> ? false : true

// Each literal compiles only while it names exactly the members of its union.
const TASK_STATE: Record<TaskState, true> = {
  SUBMITTED: true, QUEUED: true, PARKED: true, READY: true, LEASED: true, DISPATCHED: true,
  STARTING: true, RUNNING: true, SUCCEEDED: true, FAILED: true, CANCELLED: true, DEAD_LETTERED: true,
}
const PROVIDER_STATE: Record<ProviderStateName, true> = {
  AVAILABLE: true, THROTTLED: true, EXHAUSTED: true, COOLDOWN: true, DISABLED: true, UNKNOWN: true,
}
const CREDENTIAL_TIER: Record<CredentialTier, true> = { plan: true, api_key: true }
const POOL_KIND: Record<PoolKind, true> = {
  global: true, tenant: true, provider: true, resource: true, runner: true, backend: true,
}

const POOLS = statesConstants.pool_names as {
  tenant_id: string
  provider: string
  resource_class: string
  runner_profile: string
  backend: string
  pools: string[]
}

describe('the unit weights', () => {
  it('RESOURCE_UNITS is the catalogue\'s code-default units, class for class', () => {
    expect(RESOURCE_UNITS).toEqual(statesConstants.resource_class_units)
  })
})

describe('the task lifecycle', () => {
  it('the TaskState union is the server\'s task states', () => {
    expect(sorted(Object.keys(TASK_STATE))).toEqual(sorted(statesConstants.task_state))
  })

  it('CONCURRENCY_STATES and TERMINAL_STATES are the server\'s sets', () => {
    expect(sorted(CONCURRENCY_STATES)).toEqual(statesConstants.concurrency_states)
    expect(sorted(TERMINAL_STATES)).toEqual(statesConstants.terminal_states)
  })

  it('REAL_STATES and NEVER_WRITTEN partition the server\'s states exactly', () => {
    const real = new Set<string>(REAL_STATES)
    expect(REAL_STATES.length, 'REAL_STATES repeats a state').toBe(real.size)
    expect([...NEVER_WRITTEN].filter((s) => real.has(s)), 'in both lists').toEqual([])
    expect(sorted([...REAL_STATES, ...NEVER_WRITTEN])).toEqual(sorted(statesConstants.task_state))
  })
})

describe('the reason and provider enums', () => {
  it('the ParkReason union is the server\'s park reasons', () => {
    const same: Same<ParkReason, (typeof PARK_REASONS)[number]> = true
    expect(same).toBe(true)
    expect(sorted(PARK_REASONS)).toEqual(sorted(statesConstants.park_reason))
    expect(PARK_REASONS.length, 'PARK_REASONS repeats a reason').toBe(new Set(PARK_REASONS).size)
  })

  it('the ProviderStateName union is the server\'s provider states', () => {
    expect(sorted(Object.keys(PROVIDER_STATE))).toEqual(sorted(statesConstants.provider_state))
  })

  it('END_CAUSES is the server\'s end causes, in the frozen order', () => {
    const same: Same<EndCause, (typeof END_CAUSES)[number]> = true
    expect(same).toBe(true)
    expect([...END_CAUSES]).toEqual(statesConstants.end_cause)
  })
})

describe('the pool families', () => {
  const families = POOLS.pools.map((name) => (name.includes(':') ? name.split(':')[0]! : name))

  it('the generated pool list is read', () => {
    expect(POOLS.pools).toContain('global')
    expect(POOLS.pools.length).toBeGreaterThan(5)
  })

  it('PoolKind and POOL_FAMILY_ORDER are the families pool_names_for names', () => {
    expect(sorted(Object.keys(POOL_KIND))).toEqual(sorted(new Set(families)))
    expect(POOL_FAMILY_ORDER.length, 'POOL_FAMILY_ORDER repeats a family').toBe(new Set(POOL_FAMILY_ORDER).size)
    expect(sorted(POOL_FAMILY_ORDER)).toEqual(sorted(new Set(families)))
  })

  it('poolKind parses each pool name to its own family, never to global by default', () => {
    for (const [i, name] of POOLS.pools.entries()) expect(poolKind(name), name).toBe(families[i])
  })

  it('providerTenantPool spells the provider-and-tenant pool as pool_names_for does', () => {
    const want = POOLS.pools.filter((p) => p.startsWith('provider:') && p.includes(POOLS.tenant_id))
    expect([providerTenantPool(POOLS.provider, POOLS.tenant_id)]).toEqual(want)
  })
})

describe('the quota broker sweep', () => {
  it('QUOTA_SWEEP_INTERVAL_SECONDS is the interval the scheduler cron runs at', () => {
    expect(QUOTA_SWEEP_INTERVAL_SECONDS).toBe(statesConstants.quota_sweep_interval_seconds)
  })
})

describe('the credential an attempt ran on (contract request 152)', () => {
  it('the CredentialTier union is the frozen CREDENTIAL_TIERS', () => {
    expect(sorted(Object.keys(CREDENTIAL_TIER))).toEqual(sorted(statesConstants.credential_tiers))
  })

  it('AttemptRow declares the three fields, the tier typed with the union', () => {
    const declared: [Has<AttemptRow, 'account_id'>, Has<AttemptRow, 'credential_source'>, Has<AttemptRow, 'credential_tier'>] =
      [true, true, true]
    const tier: Same<AttemptRow['credential_tier'], CredentialTier | null | undefined> = true
    expect([...declared, tier]).toEqual([true, true, true, true])
  })
})

describe('the served shapes', () => {
  it('the generated Task, and the console\'s, carry the child-task fields', () => {
    const generated: [Has<ApiTask, 'parent_task_id'>, Has<ApiTask, 'parent_attempt_id'>] = [true, true]
    const extended: Same<Task['parent_task_id'], ApiTask['parent_task_id']> = true
    expect([...generated, extended]).toEqual([true, true, true])
  })

  it('WorkflowStep.input_from is the API\'s string map, always served', () => {
    const shape: Same<WorkflowStep['input_from'], NonNullable<WorkflowStepCreate['input_from']>> = true
    const always: Present<WorkflowStep, 'input_from'> = true
    expect([shape, always]).toEqual([true, true])
  })

  it('the outcome ledger counts child_cascade as its own cancel class', () => {
    expect(statesConstants.end_cause).toContain('child_cascade')
    const counted: 'child_cascade' extends CancelCauseKey ? true : false = true
    expect(counted).toBe(true)
  })
})
