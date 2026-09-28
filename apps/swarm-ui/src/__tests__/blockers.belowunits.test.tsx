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
import { render, screen, waitFor } from '@testing-library/react'

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

const { CapacityScreen } = await import('../Capacity')
const { ProfilesScreen } = await import('../Profiles')
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

/** Every mark on the blocker list under the card, as `word|tone`. */
function listMarks(el: HTMLElement): string[] {
  return [...el.querySelectorAll('.blocker-group .ctl-chip')].map((c) => {
    const tone = [...c.classList].find((k) => k.startsWith('is-')) ?? '(none)'
    return `${(c.textContent ?? '').trim()}|${tone}`
  })
}

const NEVER = /can never (be )?admit/i
const RAISE = /(somebody|someone|a person) (has|needs) to raise/i

// ---------------------------------------------------------------------------
// Profile headroom: the card's blocker list and its Status cell
// ---------------------------------------------------------------------------

describe('Profile headroom, a pool whose limit is below one task', () => {
  /**
   * THE MEASURED CASE FROM #66. MUTATION: drop the units check and this row is
   * `full`, "0 of 1 units in use", under "eligible, no room" again.
   */
  it('says a browser task can never be admitted at limit 1, and that a person has to raise it', async () => {
    serve(blocker({}))
    const { container } = render(<ProfilesScreen />)

    await waitFor(
      () => expect(container.querySelector('.blocker-group.needs-action'), 'filed where waiting is the answer').not.toBeNull(),
      { timeout: 3000 },
    )
    const acting = container.querySelector('.blocker-group.needs-action') as HTMLElement
    expect(acting.textContent).toMatch(NEVER)
    expect(acting.textContent).toMatch(RAISE)
    expect(acting.textContent, 'the limit the task cannot fit under is named').toMatch(/limit (of )?1\b/)
    expect(acting.textContent, 'and so is what one task weighs').toMatch(/2 units/)
    expect(container.querySelector('.blocker-group.no-room')).toBeNull()

    for (const m of listMarks(container)) expect(m, 'nothing is busy: nothing is running').not.toMatch(/^full\|/)
    expect(container.textContent).not.toMatch(/busy platform-wide/)
    expect(container.textContent).not.toMatch(/0 of 1 units in use/)
  })

  it('does not tag the pool `full` in its Status cell either', async () => {
    serve(blocker({}))
    render(<ProfilesScreen />)
    const row = (await screen.findByRole('rowheader', { name: /resource:browser/ }, { timeout: 3000 })).closest('tr')!
    await waitFor(() => expect(row.className).not.toMatch(/\bfull\b/), { timeout: 3000 })
    const status = row.querySelector('[data-label="Status"] .ctl-chip') as HTMLElement
    expect(status.textContent?.trim()).not.toBe('full')
    expect(status.className, 'a person has to act: the paused tone, as a limit of 0 set by an operator is drawn').toContain('is-paused')
    expect(status.getAttribute('title') ?? status.getAttribute('aria-label') ?? '').toMatch(NEVER)
  })

  /** The other half: a limit the task fits under is still a full pool. */
  it('keeps `full` for a pool at a ceiling one task fits under', async () => {
    serve(blocker({ limit: 4, active: 3 }))
    const { container } = render(<ProfilesScreen />)
    await waitFor(() => expect(container.querySelector('.blocker-group.no-room')).not.toBeNull(), { timeout: 3000 })
    expect(listMarks(container)).toEqual(['full|is-warn'])
    expect(container.textContent).toMatch(/3 of 4 units in use/)
    expect(container.textContent).not.toMatch(NEVER)
  })

  /** A limit of 0 keeps the message it already had. */
  it('keeps the limit-0 message for a pool set to zero', async () => {
    serve(blocker({ limit: 0, active: 0 }))
    const { container } = render(<ProfilesScreen />)
    await waitFor(() => expect(container.querySelector('.blocker-group.needs-action')).not.toBeNull(), { timeout: 3000 })
    expect(listMarks(container)).toEqual(['limit 0|is-paused'])
    expect(container.textContent).toMatch(/paused by operator \(limit 0\)/)
    expect(container.textContent).not.toMatch(NEVER)
  })

  /**
   * NO CATALOGUE, NO CLAIM. The weight is read from `/v1/resource-classes`
   * and from nowhere else; a screen that fell back to a bundled table here
   * would be carrying the copy #66 says it must not. MUTATION: read
   * `RESOURCE_UNITS` from types.ts when the route fails.
   */
  it('claims nothing about the weight when the resource-class catalogue could not be read', async () => {
    serve(blocker({}), {
      status: 'error',
      error: { kind: 'unreachable', httpStatus: null, code: null, message: 'The API could not be reached.' },
    })
    const { container } = render(<ProfilesScreen />)
    await waitFor(() => expect(container.querySelector('.blocker-group')).not.toBeNull(), { timeout: 3000 })
    expect(container.textContent).not.toMatch(NEVER)
  })
})

// ---------------------------------------------------------------------------
// Pools: the Held back by chip
// ---------------------------------------------------------------------------

describe('Pools, the Held back by chip for the same pool', () => {
  it('is not drawn as `browser 0/1` in the full tone', async () => {
    serve(blocker({}))
    render(<CapacityScreen />)
    const row = (await screen.findByRole('rowheader', { name: 'browser' }, { timeout: 3000 })).closest('tr')!
    await waitFor(
      () => {
        const chip = row.querySelector('[data-label="Held back by"] .ctl-chip') as HTMLElement
        expect(chip.className).toContain('is-paused')
      },
      { timeout: 3000 },
    )
    const chip = row.querySelector('[data-label="Held back by"] .ctl-chip') as HTMLElement
    expect(chip.textContent).not.toMatch(/0\/1/)
    expect(chip.getAttribute('title')).toMatch(NEVER)
  })
})

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
})
