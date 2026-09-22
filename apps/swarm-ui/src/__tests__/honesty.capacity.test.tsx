// The em-dash rule and the partial-read rule, on the screen where they matter.
//
// Capacity is where an operator goes when nothing is being admitted and they
// need to know which ceiling is binding. Three ways it can lie:
//
//   1. print 0 for a figure nobody measured, which reads as "the platform is
//      full" when the truth is "a pool could not be read";
//   2. name ONE pool when several are refusing, so the operator raises it and
//      nothing moves -- a bug this repository has already shipped;
//   3. present a shortened blocker list as a complete one, which tells someone
//      they have cleared everything when they have not.
//
// `loadCapacity` is replaced rather than `fetch`, because the states under
// test are Results and driving them through HTTP would test `read` again.

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Counterfactual, Pool, ProfileAdmission, ProfileBlocker, RunnerProfile } from '../types'
import { headroomFor } from '../types'
import { BlockerList, Counterfactuals, IncompleteNote, headroomFigure } from '../Blockers'
import { expectNoFigures } from './setup'

const loadCapacity = vi.hoisted(() => vi.fn<() => Promise<Result<Capacity>>>())
vi.mock('../api', () => ({ loadCapacity }))

const { CapacityScreen } = await import('../Capacity')

// ---------------------------------------------------------------------------

function pool(over: Partial<Pool>): Pool {
  return {
    name: 'global', hard_limit: 8, adaptive_target: null, quota_derived_limit: null,
    effective_limit: 8, active: 0, available: 8, enabled: true,
    updated_at: '2026-09-22T10:00:00Z', ...over,
  }
}

function blocker(over: Partial<ProfileBlocker>): ProfileBlocker {
  return { pool: 'global', reason: 'GLOBAL_CONCURRENCY_LIMIT', limit: 8, active: 8, group: 'no_room', ...over }
}

function admission(over: Partial<ProfileAdmission>): ProfileAdmission {
  return {
    units: 1, headroom: 3, basis: 'measured', blockers: [], binding: [],
    counterfactual: [], complete: true, unread: [], uncapped: [], ...over,
  }
}

function profile(over: Partial<RunnerProfile>): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools: ['global'], ...over }
}

function capacity(over: Partial<Capacity>): Capacity {
  return {
    pools: [pool({})],
    runner_profiles: {},
    tenant_id: 'eng',
    generated_at: '2026-09-22T10:00:00Z',
    ...over,
  }
}

function renderCapacity(data: Capacity) {
  loadCapacity.mockResolvedValue({ status: 'ok', data, fetchedAt: Date.now(), serverAt: data.generated_at })
  return render(<CapacityScreen />)
}

/** The "Could start" cell for one runner profile, off the rendered table. */
function couldStart(name: string): HTMLElement {
  const row = screen.getByRole('rowheader', { name }).closest('tr')
  if (!row) throw new Error(`no row for ${name}`)
  const cell = row.querySelectorAll('td')[0]
  if (!cell) throw new Error(`no cells in the row for ${name}`)
  return cell as HTMLElement
}

// ---------------------------------------------------------------------------
// An unmeasured figure is an em dash. A measured zero is a 0.
// ---------------------------------------------------------------------------

describe('the headroom figure', () => {
  it('renders an em dash, not a 0, when the API sent no admission block', async () => {
    // THE DEMONSTRATION THIS LAYER EXISTS FOR. The strings "—" and "0" are
    // both present in the source of Capacity.tsx and Blockers.tsx, so no grep
    // over the source can tell these two cases apart. Only rendering can.
    renderCapacity(capacity({ runner_profiles: { 'claude-code': profile({ admission: undefined }) } }))

    const cell = await screen.findByRole('rowheader', { name: 'claude-code' })
    expect(cell).toBeTruthy()
    expect(couldStart('claude-code').textContent).toBe('—')
    expect(couldStart('claude-code').textContent).not.toBe('0')
    expect(couldStart('claude-code').closest('tr')?.className).toContain('unmeasured')
  })

  it('renders 0 when zero is what was measured', async () => {
    renderCapacity(
      capacity({
        runner_profiles: { 'claude-code': profile({ admission: admission({ headroom: 0, basis: 'measured', blockers: [blocker({})], binding: ['global'] }) }) },
      }),
    )
    await screen.findByRole('rowheader', { name: 'claude-code' })
    expect(couldStart('claude-code').textContent).toBe('0')
    expect(couldStart('claude-code').closest('tr')?.className).toContain('over')
  })

  it('says WHICH kind of not-measured it is, in the cell’s own title', async () => {
    // An em dash with no explanation is only marginally better than a zero.
    expect(headroomFigure(headroomFor(profile({ admission: admission({ headroom: null, basis: 'uncapped' }) }))).title)
      .toContain('nothing caps it')
    expect(headroomFigure(headroomFor(profile({ admission: admission({ headroom: null, basis: 'unknown', complete: false, unread: ['global'] }) }))).title)
      .toContain('could not be read')
    expect(headroomFigure(headroomFor(profile({ admission: admission({ headroom: 4 }) }))).title)
      .toContain('4 more could have been admitted')
  })
})

// ---------------------------------------------------------------------------
// Every pool that refused, not the tightest one
// ---------------------------------------------------------------------------

describe('held back by', () => {
  it('names EVERY refusing pool, so raising one is not mistaken for the fix', async () => {
    renderCapacity(
      capacity({
        runner_profiles: {
          'claude-code': profile({
            pools: ['global', 'resource:large', 'provider:anthropic'],
            admission: admission({
              headroom: 0,
              blockers: [
                blocker({ pool: 'resource:large', reason: 'RESOURCE_CLASS_LIMIT', limit: 4, active: 4 }),
                blocker({ pool: 'provider:anthropic', reason: 'PROVIDER_CONCURRENCY_LIMIT', limit: 6, active: 6 }),
              ],
              binding: ['resource:large', 'provider:anthropic'],
            }),
          }),
        },
      }),
    )

    const row = (await screen.findByRole('rowheader', { name: 'claude-code' })).closest('tr')
    const tags = row?.querySelectorAll('.tag') ?? []
    const labels = Array.from(tags).map((t) => t.textContent ?? '')
    expect(labels).toHaveLength(2)
    expect(labels.join(' ')).toContain('large')
    expect(labels.join(' ')).toContain('anthropic')
  })

  it('draws a paused pool differently from a full one, because the remedies are opposite', async () => {
    renderCapacity(
      capacity({
        runner_profiles: {
          'claude-code': profile({
            admission: admission({
              headroom: 0,
              // A paused pool can read 0 of 8 units in use and admit nothing:
              // the case that looks healthiest and is not. It is told apart by
              // the REASON the server sent, never by the count.
              blockers: [blocker({ pool: 'tenant:eng', reason: 'MANUAL_PAUSE', limit: 8, active: 0 })],
            }),
          }),
        },
      }),
    )
    const row = (await screen.findByRole('rowheader', { name: 'claude-code' })).closest('tr')
    const tag = row?.querySelector('.tag')
    expect(tag?.className).toContain('paused')
    expect(tag?.className).not.toContain('full')
    expect(tag?.textContent).toContain('paused')
  })
})

// ---------------------------------------------------------------------------
// A partial read says what it could not see
// ---------------------------------------------------------------------------

describe('an incomplete read', () => {
  it('says the list is incomplete and names the pools nobody read', () => {
    const h = headroomFor(profile({ admission: admission({ headroom: null, basis: 'unknown', complete: false, unread: ['provider:anthropic', 'resource:large'], blockers: [blocker({})] }) }))
    render(<IncompleteNote h={h} />)

    const note = screen.getByRole('status')
    expect(note.textContent).toContain('incomplete')
    expect(note.textContent).toContain('anthropic')
    expect(note.textContent).toContain('large')
    expect(note.textContent).toContain('withheld rather than guessed')
  })

  it('says nothing at all when the read WAS complete', () => {
    const { container } = render(<IncompleteNote h={headroomFor(profile({ admission: admission({}) }))} />)
    expect(container.textContent).toBe('')
  })

  it('an empty blocker list under an incomplete read is not reported as "nothing is refusing"', () => {
    // The dangerous case: measured nothing, and saying so as though it were a
    // clean bill of health.
    const h = headroomFor(profile({ admission: admission({ complete: false, unread: ['global'], blockers: [] }) }))
    render(<BlockerList h={h} groups={undefined} />)
    expect(screen.getByText(/the list is incomplete/)).toBeTruthy()
    expect(screen.queryByText('No pool is refusing this profile.')).toBeNull()
  })

  it('reports no refusal as such only when the read was complete', () => {
    const h = headroomFor(profile({ admission: admission({ complete: true, blockers: [] }) }))
    render(<BlockerList h={h} groups={undefined} />)
    expect(screen.getByText('No pool is refusing this profile.')).toBeTruthy()
  })

  it('does not compute a counterfactual over ceilings it never read', () => {
    const h = headroomFor(profile({ admission: admission({ complete: false, unread: ['global'], counterfactual: [] }) }))
    render(<Counterfactuals h={h} generatedAt="2026-09-22T10:00:00Z" />)
    const text = document.body.textContent ?? ''
    expect(text).toContain('Not computed')
    // And no number is offered in its place.
    expectNoFigures(document.body)
  })
})

// ---------------------------------------------------------------------------
// The grouping is the server's
// ---------------------------------------------------------------------------

describe('blocker grouping', () => {
  const needsAction = blocker({ pool: 'tenant:eng', reason: 'MANUAL_PAUSE', group: null })
  const noRoom = blocker({ pool: 'global', reason: 'GLOBAL_CONCURRENCY_LIMIT', group: null })

  it('uses the groups the response carried', () => {
    const h = headroomFor(profile({ admission: admission({ blockers: [needsAction, noRoom] }) }))
    render(<BlockerList h={h} groups={{ needs_action: ['MANUAL_PAUSE'], no_room: ['GLOBAL_CONCURRENCY_LIMIT'] }} />)
    expect(screen.getByText('Somebody has to act')).toBeTruthy()
    expect(screen.getByText('Eligible, no room')).toBeTruthy()
    expect(screen.queryByText('Not grouped')).toBeNull()
  })

  it('puts a reason in NEITHER group in its own section rather than under "waiting is fine"', () => {
    // Filing an unknown reason under "waiting is a valid answer" is a lie
    // about the remedy, and the more comfortable of the two lies available.
    const h = headroomFor(profile({ admission: admission({ blockers: [blocker({ pool: 'backend:gke', reason: 'SOMETHING_NEW', group: null })] }) }))
    render(<BlockerList h={h} groups={{ needs_action: [], no_room: [] }} />)
    expect(screen.getByText('Not grouped')).toBeTruthy()
    expect(screen.queryByText('Eligible, no room')).toBeNull()
    // And the raw reason is still printed, so the staleness is visible.
    expect(document.body.textContent).toContain('SOMETHING_NEW')
  })
})

// ---------------------------------------------------------------------------
// The counterfactual is a snapshot, not a promise
// ---------------------------------------------------------------------------

describe('the counterfactual', () => {
  function cf(over: Partial<Counterfactual>): Counterfactual {
    return { pool: 'global', action: 'raise', headroom_after: 4, basis_after: 'measured', delta: 4, next_binding: [], ...over }
  }

  it('is worded in the past conditional, against the server’s own instant', () => {
    const h = headroomFor(profile({ admission: admission({ counterfactual: [cf({})] }) }))
    render(<Counterfactuals h={h} generatedAt="2026-09-22T10:00:00Z" />)
    const text = document.body.textContent ?? ''
    expect(text).toContain('would have started')
    expect(text).not.toContain('will start')
    expect(text).not.toContain('you can start')
    expect(document.querySelector('time')?.getAttribute('dateTime') ?? document.querySelector('time')?.getAttribute('datetime'))
      .toBe('2026-09-22T10:00:00Z')
  })

  it('a zero delta names what still binds, rather than reading as a glitch', () => {
    const h = headroomFor(profile({ admission: admission({ counterfactual: [cf({ delta: 0, headroom_after: 0, next_binding: ['provider:anthropic'] })] }) }))
    render(<Counterfactuals h={h} generatedAt="2026-09-22T10:00:00Z" />)
    const row = document.querySelector('.cf-row')
    expect(row?.className).toContain('is-pointless')
    expect(row?.textContent).toContain('nothing would have changed')
    expect(row?.textContent).toContain('anthropic')
  })

  it('an unmeasurable delta is said to be unmeasurable, not shown as 0', () => {
    const h = headroomFor(profile({ admission: admission({ counterfactual: [cf({ delta: null, headroom_after: 3 })] }) }))
    render(<Counterfactuals h={h} generatedAt="2026-09-22T10:00:00Z" />)
    expect(document.querySelector('.cf-effect')?.textContent).toBe('the change could not be measured')
  })
})

// ---------------------------------------------------------------------------
// Whole-screen invariants
// ---------------------------------------------------------------------------

describe('the capacity board as a whole', () => {
  it('states the conjunction, because the common misreading is to add pools up', async () => {
    renderCapacity(capacity({}))
    const line = await screen.findByText(/must clear/)
    expect(line.textContent).toContain('minimum')
    expect(line.textContent).toContain('never a sum')
  })

  it('declares the tenant beside the headroom figures, every time', async () => {
    // Trap D: the pool list is the CALLING tenant's, including for an admin.
    // An unlabelled figure here reads as the platform's capacity.
    renderCapacity(capacity({ tenant_id: 'eng', runner_profiles: { 'claude-code': profile({ admission: admission({}) }) } }))
    expect((await screen.findByText(/for tenant eng/)).className).toContain('scope')
  })

  it('declares scope on every pool family, so two figures are never silently compared', async () => {
    renderCapacity(capacity({ pools: [pool({ name: 'global' }), pool({ name: 'tenant:eng' }), pool({ name: 'provider:anthropic:tenant:eng' })] }))
    await screen.findByText('Global')
    const scopes = Array.from(document.querySelectorAll('h2 .scope')).map((s) => s.textContent)
    expect(scopes).toContain('platform-wide')
    expect(scopes).toContain('this tenant')
    // The per-tenant slice of a provider pool is tenant scope, not platform.
    expect(scopes.filter((s) => s === 'this tenant').length).toBe(2)
  })

  it('marks a pool carrying more than its ceiling as drift rather than as full', async () => {
    renderCapacity(capacity({ pools: [pool({ name: 'global', active: 9, effective_limit: 8, available: 0 })] }))
    expect(await screen.findByText('over ceiling')).toBeTruthy()
    expect(screen.queryByText('full')).toBeNull()
  })

  it('shows a paused pool as paused even when it is carrying nothing', async () => {
    renderCapacity(capacity({ pools: [pool({ name: 'tenant:eng', enabled: false, active: 0 })] }))
    expect(await screen.findByText('paused')).toBeTruthy()
    expect(screen.queryByText('ok')).toBeNull()
  })

  it('a failed read of capacity renders no pool numbers at all', async () => {
    loadCapacity.mockResolvedValue({
      status: 'error',
      error: { kind: 'upstream_degraded', httpStatus: 503, code: 'unavailable', message: 'Firestore did not answer.' },
    })
    render(<CapacityScreen />)
    await screen.findByText('Firestore did not answer.')
    expect(screen.queryByText('Headroom')).toBeNull()
    expect(screen.queryByRole('table')).toBeNull()
    expectNoFigures(document.body, ['HTTP 503'])
  })
})
