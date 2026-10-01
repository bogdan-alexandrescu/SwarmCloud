// A POOL WHOSE LIMIT IS BELOW WHAT ONE TASK WEIGHS IS NOT A BUSY POOL (#66).
//
// THE DEFECT. Admission adds a task's WEIGHT -- its resource class's `units`,
// standard 1, browser 2, large 4 -- to every pool it must clear, and refuses
// when `active + units > limit` (`evaluate_capacity`, admission.py). So with
// `resource:browser` at limit 1, a browser task (2 units) is refused on every
// drain: 0 + 2 > 1 with nothing running at all. The refusal carries the full
// pool's reason, RESOURCE_CLASS_LIMIT, and a positive `limit`, so every screen
// that reads a blocker drew it as FULL -- "0 of 1 units in use", filed under
// "eligible, no room", which tells the reader to wait. No wait clears it.
//
// THE SAME KIND OF FACT AS A LIMIT OF 0, which these screens already tell
// apart (`blockerCeiling` gives `set-to-zero`, waitreason.test.tsx): the task
// can never be admitted at this limit, and a person has to raise it.
//
// WHERE THE WEIGHT COMES FROM. The blocker does not carry it. The task's units
// are its resource class's, and `GET /v1/resource-classes` serves `units` for
// each class -- so that route is what is read, and a catalogue that could not
// be read claims nothing. The UI keeps no copy of the table.
//
// `loadCapacity` and `loadResourceClasses` are replaced; the rest of the api
// module is real, as tables.scroll.test.tsx does it.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { ResourceClasses } from '../api'
import type { Capacity, Pool, ProfileAdmission, ProfileBlocker, RunnerProfile } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadResourceClasses: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { ProfileFacts } = await import('../Submit')

// ---------------------------------------------------------------------------

const CLASSES: ResourceClasses = {
  standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 },
  browser: { name: 'browser', cpu: 8, memory_gib: 16, disk_gib: 8, units: 2 },
  large: { name: 'large', cpu: 8, memory_gib: 32, disk_gib: 16, units: 4 },
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-28T10:00:00Z' }
}

function pool(over: Partial<Pool> & { name: string }): Pool {
  return {
    hard_limit: 8, adaptive_target: null, quota_derived_limit: null,
    effective_limit: 8, active: 0, available: 8, enabled: true,
    updated_at: '2026-09-28T10:00:00Z', ...over,
  }
}

function blocker(over: Partial<ProfileBlocker>): ProfileBlocker {
  return { pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 1, active: 0, group: 'no_room', ...over }
}

function admission(b: ProfileBlocker): ProfileAdmission {
  return {
    units: 2, headroom: 0, basis: 'measured', blockers: [b], binding: [b.pool],
    counterfactual: [], complete: true, unread: [], uncapped: [],
  }
}

/** The `browser` profile, refused by `resource:browser` as `b` says. */
function browser(b: ProfileBlocker): RunnerProfile {
  return {
    resource_class: 'browser', backend: 'GKE_AUTOPILOT', provider: null, units: 2,
    pools: ['global', 'tenant:eng', 'resource:browser'], admission: admission(b),
  }
}

function capacity(b: ProfileBlocker): Capacity {
  return {
    pools: [
      pool({ name: 'global', hard_limit: 50, effective_limit: 50, available: 50 }),
      pool({ name: 'tenant:eng', hard_limit: 10, effective_limit: 10, available: 10 }),
      pool({
        name: b.pool, hard_limit: b.limit, effective_limit: b.limit,
        active: b.active, available: Math.max(0, b.limit - b.active),
      }),
    ],
    runner_profiles: { browser: browser(b) },
    tenant_id: 'eng',
    generated_at: '2026-09-28T10:00:00Z',
  }
}

function serve(b: ProfileBlocker, classes: Result<{ resource_classes: ResourceClasses }> = ok({ resource_classes: CLASSES })) {
  api.loadCapacity.mockResolvedValue(ok(capacity(b)))
  api.loadResourceClasses.mockResolvedValue(classes)
}

const NEVER = /can never (be )?admit/i
const RAISE = /(somebody|someone|a person) (has|needs) to raise/i

// ---------------------------------------------------------------------------
// Submit: the "right now" box
// ---------------------------------------------------------------------------

describe('Submit, the right-now box for a browser task', () => {
  it('says the task can never be admitted, not "held down by, 0 of 1 in use"', async () => {
    serve(blocker({}))
    const b = blocker({})
    const { container } = render(
      <ProfileFacts name="browser" profile={browser(b)} pools={capacity(b).pools} />,
    )
    await waitFor(() => expect(container.textContent).toMatch(NEVER), { timeout: 3000 })
    expect(container.textContent).toMatch(RAISE)
    expect(container.textContent).not.toMatch(/0 of 1 (weighted )?units in use/)
    expect(container.querySelector('.tag.full'), 'the compact row is not tagged full').toBeNull()
  })

  // MOVED FROM THE PROFILE HEADROOM CARD (removed 2026-10-01). The card was
  // one of three surfaces drawing this verdict; Submit's box is the one left
  // that draws a blocker row, through the same `blockerVerdict`.

  /** The other half: a limit the task fits under is still a full pool. */
  it('keeps `full` for a pool at a ceiling one task fits under', async () => {
    serve(blocker({ limit: 4, active: 3 }))
    const b = blocker({ limit: 4, active: 3 })
    const { container } = render(<ProfileFacts name="browser" profile={browser(b)} pools={capacity(b).pools} />)
    await waitFor(() => expect(container.querySelector('.blocker-list .tag.full')).not.toBeNull(), { timeout: 3000 })
    expect(container.textContent).toMatch(/3 of 4 units in use/)
    expect(container.textContent).not.toMatch(NEVER)
  })

  /** A limit of 0 keeps the mark it already had. */
  it('keeps `limit 0` for a pool set to zero', async () => {
    serve(blocker({ limit: 0, active: 0 }))
    const b = blocker({ limit: 0, active: 0 })
    const { container } = render(<ProfileFacts name="browser" profile={browser(b)} pools={capacity(b).pools} />)
    await waitFor(() => expect(container.querySelector('.blocker-list .tag')).not.toBeNull(), { timeout: 3000 })
    expect(container.querySelector('.blocker-list .tag')!.textContent?.trim()).toBe('limit 0')
    expect(container.textContent).not.toMatch(NEVER)
  })

  /**
   * NO CATALOGUE, NO CLAIM. The weight is read from `/v1/resource-classes`
   * and from nowhere else. MUTATION: read `RESOURCE_UNITS` from types.ts when
   * the route fails.
   */
  it('claims nothing about the weight when the resource-class catalogue could not be read', async () => {
    serve(blocker({}), {
      status: 'error',
      error: { kind: 'unreachable', httpStatus: null, code: null, message: 'The API could not be reached.' },
    })
    const b = blocker({})
    const { container } = render(<ProfileFacts name="browser" profile={browser(b)} pools={capacity(b).pools} />)
    await waitFor(() => expect(container.querySelector('.blocker-list')).not.toBeNull(), { timeout: 3000 })
    expect(container.textContent).not.toMatch(NEVER)
  })
})
