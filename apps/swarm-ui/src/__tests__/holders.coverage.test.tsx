// A TRUNCATED LIST MUST NOT LOOK COMPLETE.
//
// `/v1/admin/leases` serves the newest `limit` live leases, and since PR #19 it
// says how many live leases the window left out (`active_beyond_window`) and
// whether it was full with more behind it (`truncated`). The Holders screen
// read neither. So at 200 of 214 live leases it drew "200 unreleased leases",
// a table headed "Every holder" with "200 rows", and -- when the listed leases
// happened to agree with the counters -- a `real zero` on the accounting-drift
// card: a claim of agreement across the fleet computed over a page that left
// fourteen leases out. The route's own docstring says a delta is evidence only
// when that count is 0.
//
// Each case below renders the real screen against the real shape. They were
// pushed before Holders.tsx read the fields, and the three that are not the
// complete case failed there.

import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { HoldersBoard } from '../api'
import type { Result } from '../fetch'
import type { LeasePage, LeaseRow, Pool } from '../types'

const api = vi.hoisted(() => ({ loadHolders: vi.fn() }))
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { HoldersScreen } = await import('../Holders')

const WAIT = { timeout: 5000 } as const

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-24T10:00:00Z' }
}

function lease(n: number): LeaseRow {
  return {
    lease_id: `lease-${String(n).padStart(10, '0')}`,
    task_id: `task-${String(n).padStart(10, '0')}`,
    attempt_id: `att-${n}`,
    tenant_id: 'eng',
    generation: 1,
    pools: ['global', 'resource:standard'],
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: '2026-09-24T09:00:00Z',
    dispatch_deadline: '2026-09-24T09:05:00Z',
    expires_at: '2026-09-24T11:00:00Z',
    heartbeat_at: '2026-09-24T09:59:00Z',
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: 30,
    heartbeat_ever: true,
    last_error: null,
  }
}

function pool(name: string, active: number): Pool {
  return {
    name,
    hard_limit: 40,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 40,
    active,
    available: 40 - active,
    enabled: true,
    updated_at: '2026-09-24T10:00:00Z',
  }
}

/**
 * Three listed leases, and pool counters that AGREE with them -- 3 on
 * `global`, 3 on `resource:standard`. That is the case that drew `real zero`:
 * the delta over the rows is nought whether or not the rows are every lease.
 */
function board(coverage: Record<string, unknown>): HoldersBoard {
  return {
    page: {
      leases: [lease(1), lease(2), lease(3)],
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: '2026-09-24T10:00:00Z',
      active_only: true,
      tenant_id: null,
      units_held: 3,
      ...coverage,
    } as unknown as LeasePage,
    pools: [pool('global', 3), pool('resource:standard', 3)],
    poolsDetail: null,
  }
}

async function mount(coverage: Record<string, unknown>): Promise<void> {
  api.loadHolders.mockResolvedValue(ok(board(coverage)))
  render(<HoldersScreen />)
  await screen.findByText('Every holder', undefined, WAIT)
}

function driftCard(): Element {
  const title = [...document.querySelectorAll('.ctl-card-title')].find((t) =>
    (t.textContent ?? '').startsWith('Accounting drift'),
  )
  expect(title, 'no accounting-drift card').toBeTruthy()
  return title!.closest('.ctl-card')!
}

function tableNote(): string {
  const bar = [...document.querySelectorAll('.ctl-toolbar')].find((t) =>
    (t.querySelector('.ctl-card-title')?.textContent ?? '') === 'Every holder',
  )
  expect(bar, 'no "Every holder" toolbar').toBeTruthy()
  return (bar!.querySelector('.ctl-card-note')?.textContent ?? '').replace(/\s+/g, ' ').trim()
}

function summary(): string {
  return (document.body.textContent ?? '').replace(/\s+/g, ' ')
}

describe('Holders says whether its rows are every live lease', () => {
  it('draws a complete read exactly as before: a real zero, and the rows as a count', async () => {
    await mount({ active_beyond_window: 0, truncated: false, examined: 3 })
    const card = driftCard()
    expect(card.querySelector('.ctl-mark.is-zero')?.textContent).toBe('real zero')
    expect(card.querySelector('.ctl-mark.is-partial')).toBeNull()
    expect(tableNote()).toBe('3 rows')
    expect(summary()).toContain('3 unreleased leases')
    expect(document.querySelector('.ctl-mark.is-partial, .ctl-mark.is-absent')).toBeNull()
  })

  it('never calls agreement over a cut window a real zero, and says what the rows are out of', async () => {
    await mount({ active_beyond_window: 2, truncated: true, examined: 3 })
    const card = driftCard()
    // THE LIE THIS EXISTS FOR: agreement over three of five is not agreement.
    expect(card.querySelector('.ctl-mark.is-zero'), 'a cut window drew a real zero').toBeNull()
    const mark = card.querySelector('.ctl-mark.is-partial')
    expect(mark, 'the drift card does not mark its comparison as partial').not.toBeNull()
    expect(mark!.textContent).toBe('partial')
    // The sentence, with this read's own numbers, is the accessible name.
    expect(mark!.getAttribute('aria-label')).toMatch(/2 live leases are not among them/)
    expect(card.textContent).toContain('2 live not listed')

    // THE LIST SAYS IT IS CUT.
    expect(tableNote()).toContain('3 of 5 rows')
    expect(tableNote()).toContain('2 not shown')
    expect(summary()).toContain('3 of 5 unreleased leases')
    // And the class mix, computed over the same rows, says so too.
    const mix = [...document.querySelectorAll('.ctl-card')].find(
      (c) => c.querySelector('.ctl-card-title')?.textContent === 'Class mix',
    )
    expect(mix, 'no class-mix card').toBeTruthy()
    expect(mix!.querySelector('.ctl-card-note')?.textContent).toContain('3 of 5 leases')
  })

  it('treats `truncated` without a count as cut, and invents no number for it', async () => {
    await mount({ truncated: true })
    expect(driftCard().querySelector('.ctl-mark.is-zero'), 'a truncated window drew a real zero').toBeNull()
    expect(driftCard().querySelector('.ctl-mark.is-partial')).not.toBeNull()
    expect(tableNote()).toContain('3+ rows')
    expect(tableNote()).toContain('more not shown')
  })

  it('does not read a missing count as a zero: an older API says nothing, and nothing is claimed', async () => {
    // Neither field: the page an API from before PR #19 serves. Absent is not
    // zero -- a list that cannot say whether it was cut must not look whole.
    await mount({})
    const card = driftCard()
    expect(card.querySelector('.ctl-mark.is-zero'), 'an unreported coverage drew a real zero').toBeNull()
    expect(card.querySelector('.ctl-mark.is-absent')?.textContent).toBe('not measured')
    expect(card.textContent).toContain('coverage unreported')
    expect(tableNote()).toContain('completeness unreported')
    expect(summary()).toContain('3 unreleased leases listed')
  })
})
