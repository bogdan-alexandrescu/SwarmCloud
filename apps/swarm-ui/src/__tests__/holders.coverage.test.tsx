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
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { HoldersBoard } from '../api'
import type { Result } from '../fetch'
import type { LinkOut } from '../primitives'
import type { LeasePage, LeaseRow, Pool } from '../types'

const api = vi.hoisted(() => ({ loadHolders: vi.fn() }))
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

/**
 * What `Screen` hands the shared empty state, recorded on the way through.
 *
 * A PASS-THROUGH, NOT A STUB: the real `Absent` still draws, so every other
 * case in this file sees the same DOM it always did. It exists for CP-21, which
 * is a claim about WHICH SLOT the way out arrives in -- `empty.link`, the one
 * §6.9's shape reserves for it -- and a rendered anchor looks the same whether
 * the primitive drew it or a screen typed it into the sentence.
 */
const absent = vi.hoisted(() => ({ calls: [] as { link?: LinkOut; children?: unknown }[] }))
vi.mock('../primitives', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../primitives')>()
  return {
    ...actual,
    Absent: (props: Parameters<typeof actual.Absent>[0]) => {
      absent.calls.push(props)
      return actual.Absent(props)
    },
  }
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

/**
 * CP-7 (#85). A counter that leaked on a pool NO live lease names is the
 * drift this card exists to find, and it was the one case it could not see:
 * the card compared only pools some lease mentioned, so `tenant:eng` holding
 * 2 units with no lease behind them was simply not a row, and the card drew
 * `real zero` over it.
 *
 * Over EVERY live lease, a pool no lease names has a lease side of 0 by
 * measurement, not by assumption, so it is compared. Over a cut window it is
 * not -- a lease the window left out may name it -- which the second case
 * holds.
 */
describe('with every live lease in hand, Drift compares every pool', () => {
  function withLeakedPool(coverage: Record<string, unknown>): HoldersBoard {
    const b = board(coverage)
    return { ...b, pools: [...(b.pools ?? []), pool('tenant:eng', 2)] }
  }

  it('finds a counter no lease accounts for', async () => {
    api.loadHolders.mockResolvedValue(ok(withLeakedPool({ active_beyond_window: 0, truncated: false })))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const card = driftCard()
    expect(card.querySelector('.ctl-mark.is-zero'), 'a leaked counter was drawn as a real zero').toBeNull()
    const row = [...card.querySelectorAll('tbody tr')].find((tr) => (tr.textContent ?? '').includes('tenant:eng'))
    expect(row, 'the leaked pool is not compared').toBeTruthy()
    expect(row!.querySelector('td[data-label="Delta"]')?.textContent).toBe('+2')
  })

  it('does not manufacture a delta over a cut window', async () => {
    api.loadHolders.mockResolvedValue(ok(withLeakedPool({ active_beyond_window: 2, truncated: true })))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const rows = [...driftCard().querySelectorAll('tbody tr')].map((tr) => tr.textContent ?? '')
    expect(rows.some((t) => t.includes('tenant:eng'))).toBe(false)
  })
})

/**
 * CP-21 (#85). The populated summary says `every tenant`; the empty state
 * said nothing about scope, so "no unreleased leases" read as a claim about
 * whoever was looking. And an empty state is a mark, a heading, one sentence
 * and a way out (§6.9) -- it had no way out.
 */
describe('the empty state says whose leases it counted, and where to go', () => {
  it('names its scope and links out', async () => {
    api.loadHolders.mockResolvedValue({ status: 'empty', fetchedAt: Date.now(), serverAt: '2026-09-24T10:00:00Z' })
    render(<HoldersScreen />)
    await screen.findByText('No unreleased leases', undefined, WAIT)
    const text = summary()
    expect(text).toContain('every tenant')
    const links = [...document.querySelectorAll('a[href^="#"]')]
    expect(links.length, 'the empty state has no way out').toBeGreaterThan(0)
  })

  /**
   * CP-21's other half: the way out goes in `Screen`'s `empty.link` slot, not
   * in the body. #144 wrote the Pools link into the sentence because the slot
   * did not exist yet; #145 added it and nothing moved the link.
   *
   * MUTATION: put the anchor back in `empty.body`. The primitive is handed no
   * link, and the sentence carries an anchor of its own.
   */
  it('hands the way out to the empty state’s link slot, not to its sentence', async () => {
    absent.calls.length = 0
    api.loadHolders.mockResolvedValue({ status: 'empty', fetchedAt: Date.now(), serverAt: '2026-09-24T10:00:00Z' })
    render(<HoldersScreen />)
    await screen.findByText('No unreleased leases', undefined, WAIT)
    expect(absent.calls.length, 'Screen drew the empty state without the shared primitive').toBeGreaterThan(0)
    const last = absent.calls[absent.calls.length - 1]!
    expect(last.link, 'the Pools link is not in the link slot').toEqual({ href: '#capacity/pools', label: 'Pools' })
    const sentence = render(<>{last.children as ReactNode}</>)
    expect(sentence.container.querySelector('a'), 'the sentence still carries a link of its own').toBeNull()
    // The scope the populated summary states is still in the sentence.
    expect(sentence.container.textContent).toContain('every tenant')
  })
})
