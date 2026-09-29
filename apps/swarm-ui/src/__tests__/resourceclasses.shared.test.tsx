// ONE READ OF THE RESOURCE-CLASS CATALOGUE, HOWEVER MANY SCREENS WANT IT (#227).
//
// THE DEFECT. Profile headroom, Profiles and Submit each mounted
// `useResourceClasses()` and each fired its own GET /v1/resource-classes, so
// three screens on one tab were three reads of a table that changes only with
// a deploy. The hook now goes through one shared, cached read: the first mount
// asks, every later mount is handed the same answer.
//
// WHAT IS NOT CACHED. A failed read claims nothing and is not kept, so the next
// mount asks again rather than inheriting a failure for the life of the tab.
//
// `loadCapacity` and `loadResourceClasses` are replaced, as
// blockers.belowunits.test.tsx does it; the rest of the api module is real.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { ResourceClasses } from '../api'
import type { Capacity, Pool, ProfileBlocker, RunnerProfile } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadResourceClasses: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { CapacityScreen } = await import('../Capacity')
const { ProfilesScreen } = await import('../Profiles')
const { ProfileFacts } = await import('../Submit')

const CLASSES: ResourceClasses = {
  standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 },
  browser: { name: 'browser', cpu: 8, memory_gib: 16, disk_gib: 8, units: 2 },
  large: { name: 'large', cpu: 8, memory_gib: 32, disk_gib: 16, units: 4 },
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-29T10:00:00Z' }
}

function pool(over: Partial<Pool> & { name: string }): Pool {
  return {
    hard_limit: 8, adaptive_target: null, quota_derived_limit: null,
    effective_limit: 8, active: 0, available: 8, enabled: true,
    updated_at: '2026-09-29T10:00:00Z', ...over,
  }
}

// A browser task under `resource:browser` at limit 1: the one verdict that
// needs the catalogue's units, so a screen that got them says "never".
const BELOW: ProfileBlocker = { pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 1, active: 0, group: 'no_room' }

const BROWSER: RunnerProfile = {
  resource_class: 'browser', backend: 'GKE_AUTOPILOT', provider: null, units: 2,
  pools: ['global', 'tenant:eng', 'resource:browser'],
  admission: {
    units: 2, headroom: 0, basis: 'measured', blockers: [BELOW], binding: [BELOW.pool],
    counterfactual: [], complete: true, unread: [], uncapped: [],
  },
}

const CAPACITY: Capacity = {
  pools: [
    pool({ name: 'global', hard_limit: 50, effective_limit: 50, available: 50 }),
    pool({ name: 'tenant:eng', hard_limit: 10, effective_limit: 10, available: 10 }),
    pool({ name: 'resource:browser', hard_limit: 1, effective_limit: 1, available: 1 }),
  ],
  runner_profiles: { browser: BROWSER },
  tenant_id: 'eng',
  generated_at: '2026-09-29T10:00:00Z',
}

function threeScreens() {
  return render(
    <>
      <CapacityScreen />
      <ProfilesScreen />
      <ProfileFacts name="browser" profile={BROWSER} pools={CAPACITY.pools} />
    </>,
  )
}

const NEVER = /can never (be )?admit/i

describe('the resource-class catalogue is read once for every screen that wants it', () => {
  it('Profile headroom, Profiles and Submit mount with one GET /v1/resource-classes between them', async () => {
    api.loadCapacity.mockResolvedValue(ok(CAPACITY))
    api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))
    const { container } = threeScreens()
    // Every one of the three got the answer: Submit's room sentence and the
    // headroom list both say "never", which only the units can decide.
    await waitFor(() => expect(container.querySelector('.sbf-room')!.textContent).toMatch(NEVER), { timeout: 3000 })
    await waitFor(() => expect(container.querySelector('.blocker-group.needs-action'), 'the headroom list never got the units').not.toBeNull(), { timeout: 3000 })
    expect(api.loadResourceClasses, 'each screen fired its own read of the same table').toHaveBeenCalledTimes(1)
  })

  it('a screen mounted later is handed the answer already read', async () => {
    api.loadCapacity.mockResolvedValue(ok(CAPACITY))
    api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))
    const first = render(<ProfileFacts name="browser" profile={BROWSER} pools={CAPACITY.pools} />)
    await waitFor(() => expect(first.container.textContent).toMatch(NEVER), { timeout: 3000 })
    first.unmount()
    const again = render(<ProfileFacts name="browser" profile={BROWSER} pools={CAPACITY.pools} />)
    // Drawn with the units on the first frame: nothing had to be asked.
    expect(again.container.textContent).toMatch(NEVER)
    expect(api.loadResourceClasses).toHaveBeenCalledTimes(1)
  })

  it('does not keep a failed read: the next mount asks again', async () => {
    const failed: Result<{ resource_classes: ResourceClasses }> = {
      status: 'error',
      error: { kind: 'server_error', httpStatus: 503, code: null, message: 'unavailable' },
    }
    api.loadResourceClasses.mockResolvedValueOnce(failed).mockResolvedValue(ok({ resource_classes: CLASSES }))
    const first = render(<ProfileFacts name="browser" profile={BROWSER} pools={CAPACITY.pools} />)
    await waitFor(() => expect(api.loadResourceClasses).toHaveBeenCalledTimes(1))
    // A failed catalogue claims nothing: the box reads as it did before #66.
    expect(first.container.textContent).not.toMatch(NEVER)
    first.unmount()
    const again = render(<ProfileFacts name="browser" profile={BROWSER} pools={CAPACITY.pools} />)
    await waitFor(() => expect(again.container.textContent).toMatch(NEVER), { timeout: 3000 })
    expect(api.loadResourceClasses).toHaveBeenCalledTimes(2)
  })
})
