// NEEDS ACTION, ONE RULE IN TWO PLACES (owner decision, 2026-10-01).
//
// `blockerGroup` (types.ts) splits a refusal by REMEDY: somebody must act, or
// waiting is a valid answer. Until this change only a unit test called it: the
// Agents list drew every waiting task in one undifferentiated list, and the
// Capacity board's top group was "Needs a look", decided by its own
// classification (`needsALook`), which also took in pools that are merely
// full. Two screens, two rules, and a full pool filed beside one no wait will
// ever reopen.
//
// THE DECISION. Agents splits its Waiting tab into "Needs action" and "No
// room"; Capacity's top group is "Needs action" and holds exactly the pools
// `blockerGroup` files there -- paused, set to zero by a person, with no limit
// set, or capped below one task of their class -- and a full pool stays in its
// family.
//
// MUTATION: draw Agents' Waiting tab as one list, or decide Capacity's top
// group by `needsALook` again, and the tests below go red.

import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import type { ResourceClasses } from '../api'
import type { Result } from '../fetch'
import type { Capacity, Pool, Task } from '../types'
import { POOL_LIMIT_UNSET, poolGroup, taskGroup } from '../types'
import { task } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadResourceClasses: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadCapacity: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentsScreen } = await import('../Agents')
const { CapacityScreen } = await import('../Capacity')

const CLASSES: ResourceClasses = {
  standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 },
  browser: { name: 'browser', cpu: 8, memory_gib: 16, disk_gib: 8, units: 2 },
  large: { name: 'large', cpu: 8, memory_gib: 32, disk_gib: 16, units: 4 },
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-01T10:00:00Z' }
}

function waiting(over: Partial<Task>): Task {
  return task({ state: 'READY', started_at: null, completed_at: null, ...over })
}

/** A browser task (2 units) under `resource:browser` at limit 1: never admitted at this limit. */
const TOO_SMALL = waiting({
  id: 'tsk_toosmall0',
  resource_class: 'browser',
  blocked_by: [{ pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 1, active: 0 }],
})

/** A task behind a pool with no limit set (#374). */
const UNSET_TASK = waiting({
  id: 'tsk_unsetpool',
  blocked_by: [{ pool: 'tenant:eng', reason: POOL_LIMIT_UNSET, active: 0 }],
})

/** A standard task behind a full global pool: waiting is the answer. */
const FULL = waiting({
  id: 'tsk_fullpool0',
  blocked_by: [{ pool: 'global', reason: 'GLOBAL_LIMIT', limit: 4, active: 4 }],
})

function section(heading: RegExp): HTMLElement {
  const h = screen.getByRole('heading', { name: heading })
  const s = h.closest('section')
  expect(s, `the "${heading.source}" heading is not inside a section`).not.toBeNull()
  return s as HTMLElement
}

function taskIds(el: HTMLElement): string[] {
  return [...el.querySelectorAll<HTMLElement>('[data-task-id]')].map((r) => r.dataset.taskId ?? '').sort()
}

describe('taskGroup: the Waiting tab split, by blockerGroup with the task units', () => {
  it('files below-units and limit-unset under needs action, a full pool under no room', () => {
    expect(taskGroup(TOO_SMALL, 2)).toBe('needs_action')
    expect(taskGroup(UNSET_TASK, 1)).toBe('needs_action')
    expect(taskGroup(FULL, 1)).toBe('no_room')
    // The control: the same limit-1 pool for a task it fits under is a wait.
    expect(taskGroup({ ...TOO_SMALL, resource_class: 'standard' }, 1)).toBe('no_room')
  })
})

describe('the Agents list splits Waiting into Needs action and No room', () => {
  it('draws each waiting task under the group its blockers put it in', async () => {
    api.loadTasks.mockResolvedValue(ok({ tasks: [FULL, TOO_SMALL, UNSET_TASK], tenant_id: 'acme' }))
    api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))

    render(<AgentsScreen onOpen={() => {}} />)

    await waitFor(() => {
      expect(taskIds(section(/^Needs action/))).toEqual(['tsk_toosmall0', 'tsk_unsetpool'])
    })
    expect(taskIds(section(/^No room/))).toEqual(['tsk_fullpool0'])
    // Needs action comes first: it is the group somebody has to act on.
    const heads = screen.getAllByRole('heading').map((h) => h.textContent ?? '')
    const a = heads.findIndex((t) => t.startsWith('Needs action'))
    const b = heads.findIndex((t) => t.startsWith('No room'))
    expect(a).toBeGreaterThanOrEqual(0)
    expect(a).toBeLessThan(b)
  })
})

// ---------------------------------------------------------------------------
// Capacity
// ---------------------------------------------------------------------------

function pool(name: string, over: Partial<Pool> = {}): Pool {
  return {
    name,
    hard_limit: 8,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 8,
    active: 0,
    available: 8,
    enabled: true,
    updated_at: '2026-10-01T10:00:00Z',
    ...over,
  }
}

const POOLS: Pool[] = [
  pool('global', { active: 8, available: 0 }), // full: waiting is the answer
  pool('tenant:eng', { hard_limit: null, effective_limit: null, available: null }), // no limit set
  pool('resource:browser', { hard_limit: 1, effective_limit: 1, available: 1 }), // below one browser task
  pool('resource:standard', { hard_limit: 0, effective_limit: 0, available: 0 }), // set to zero
  pool('runner:codex', { enabled: false }), // paused
  pool('provider:anthropic', { hard_limit: 0, effective_limit: 0, available: 0 }), // quota zero: names nobody
]

describe('poolGroup: the same rule, read off a pool', () => {
  it('files paused, set-to-zero, limit-unset and below-units pools under needs action', () => {
    const byName = new Map(POOLS.map((p) => [p.name, p]))
    expect(poolGroup(byName.get('tenant:eng')!)).toBe('needs_action')
    expect(poolGroup(byName.get('resource:browser')!, 2)).toBe('needs_action')
    expect(poolGroup(byName.get('resource:standard')!)).toBe('needs_action')
    expect(poolGroup(byName.get('runner:codex')!)).toBe('needs_action')
    expect(poolGroup(byName.get('global')!)).not.toBe('needs_action')
    expect(poolGroup(byName.get('provider:anthropic')!)).not.toBe('needs_action')
    // Without the class weight a limit of 1 is not known to be too small.
    expect(poolGroup(byName.get('resource:browser')!, null)).not.toBe('needs_action')
  })
})

describe('Capacity puts the pools that need a person under "Needs action" at the top', () => {
  it('holds exactly the blockerGroup needs-action pools, and leaves a full pool in its family', async () => {
    const data: Capacity = { pools: POOLS, runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-10-01T10:00:00Z' }
    api.loadCapacity.mockResolvedValue(ok(data))
    api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))

    render(<CapacityScreen />)

    const names = (el: Element) => [...el.querySelectorAll('tbody .ctl-sub')].map((s) => s.textContent ?? '').sort()
    await waitFor(() => {
      const top = section(/^Needs action/)
      expect(names(top)).toEqual(['resource:browser', 'resource:standard', 'runner:codex', 'tenant:eng'])
    })
    const top = section(/^Needs action/)
    // It is the first group on the board.
    expect(document.querySelector('.cap-families > section')).toBe(top)
    expect(screen.queryByRole('heading', { name: /Needs a look/ })).toBeNull()
    // A full pool and a provider zeroed by quota stay in their families.
    expect(names(top)).not.toContain('global')
    expect(names(top)).not.toContain('provider:anthropic')
  })
})
