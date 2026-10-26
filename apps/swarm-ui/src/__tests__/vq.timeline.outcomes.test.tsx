// VISUAL QA, WORK › TIMELINE › OUTCOMES (VQ-TIMELINE; part of #2391), AS BEHAVIOUR.
//
//   * VQ-021: the headline's read (`buckets`, `totals`, `coverage`,
//     `previous`) took ~25 s while the cards' `buckets` + `totals` read had
//     already drawn "partial · 10 of 14 days", and the chart region was a grey
//     sweep with no word on it. The region now says what it is reading, and
//     the chart draws from the first `buckets` + `totals` read that lands --
//     the one the "Why agents failed" card shares -- while the sealed days and
//     the span before it are still being read.
//   * VQ-022: a failure class with 0 drew a baseline stub that read as a small
//     non-zero amount, and a long label ran into its bar. A 0 row now draws no
//     bar, and the label cuts with an ellipsis before the bar column, its full
//     name in its title.
//   * VQ-024: at 390 the span control dropped 24h and from–to into the sheet
//     and wrapped 3+1. Every span the desktop offers is now in the one
//     control, one row that scrolls sideways if it must.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { Outcomes } from '../outcomes'
import type { Me, Stats, TaskPage, Tenant } from '../types'
import { painted } from './marks'

const api = vi.hoisted(() => ({
  loadOutcomes: vi.fn(),
  loadMe: vi.fn(),
  loadRunnerProfiles: vi.fn(),
  loadStats: vi.fn(),
  loadTasksInState: vi.fn(),
  loadTenants: vi.fn(),
  loadTaskWindow: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { ActivityScreen } = await import('../Activity')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })

function me(): Me {
  return {
    tenant: {
      tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null,
      created_at: '2026-09-01T00:00:00Z', max_active: 10, capacity_units: 20, monthly_budget_usd: null,
      enabled: true, credentials: [], service_account: null, gcs_prefix: null, namespace: null,
    },
    principal: { email: 'a@example.com', domain: 'example.com', groups: [], is_admin: false },
    environment: 'dev',
    environment_declared: true,
  }
}

function stats(): Stats {
  const byState = {
    SUBMITTED: 0, QUEUED: 0, PARKED: 0, READY: 0, LEASED: 0, DISPATCHED: 0,
    STARTING: 0, RUNNING: 0, SUCCEEDED: 1, FAILED: 0, CANCELLED: 0, DEAD_LETTERED: 0,
  }
  return { tenant_id: 'eng', dispatch_paused: false, tasks_by_state: byState, limits: {}, generated_at: new Date().toISOString() }
}

/** The sections a call asked for (`loadOutcomes(query, sections)`). */
const sectionsOf = (call: unknown[]): string[] => [...((call[1] as readonly string[] | undefined) ?? [])]

/**
 * The route as the QA run met it: a `buckets` + `totals` read answers at once,
 * as a sectioned payload (the envelope and those two only); the headline's
 * read, which also asks for `coverage` and `previous`, has not answered.
 */
function slowHeadline(full: Outcomes): void {
  api.loadOutcomes.mockImplementation((_q: URLSearchParams, sections: readonly string[] = []) => {
    if (sections.includes('coverage')) return new Promise(() => {})
    const part: Record<string, unknown> = { ...full }
    for (const k of ['retries', 'latency', 'groups', 'workflows_failed', 'coverage', 'previous']) {
      if (!sections.includes(k)) delete part[k]
    }
    return Promise.resolve(ok(part as unknown as Outcomes))
  })
}

function serveRest(): void {
  api.loadMe.mockResolvedValue(ok(me()))
  api.loadRunnerProfiles.mockResolvedValue(ok(['claude-code', 'codex']))
  api.loadStats.mockImplementation(() => Promise.resolve(ok(stats())))
  api.loadTasksInState.mockResolvedValue(ok<TaskPage>({ tasks: [], next_page_token: null }))
  api.loadTenants.mockResolvedValue(ok({ tenants: [{ tenant_id: 'eng' } as Tenant] }))
}

function card(root: HTMLElement, title: RegExp): HTMLElement {
  const h = [...root.querySelectorAll('.ol-card .ctl-card-title')].find((el) => title.test(el.textContent ?? ''))
  expect(h, `no card titled ${title}`).toBeTruthy()
  return h!.closest('.ol-card') as HTMLElement
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('VQ-021: the chart region says what it is reading, and draws what has landed', () => {
  it('names the read in the pending region while nothing has landed', () => {
    serveRest()
    api.loadOutcomes.mockReturnValue(new Promise(() => {}))
    const { container } = render(<ActivityScreen />)
    const region = container.querySelector('.ol-body')!
    expect(region.querySelector('.ol-pending.ctl-pending'), 'the sweep keeps the chart’s geometry').not.toBeNull()
    // MUTATION: drop the `.ol-reading` caption beside the sweep in Activity.tsx.
    expect(region.querySelector('.ol-reading')?.textContent).toBe('Reading the outcome ledger for 14d…')
    expect(region.getAttribute('aria-busy')).toBe('true')
  })

  it('draws the chart from the first buckets + totals read while coverage and the span before are still read', async () => {
    serveRest()
    const d = ledgerFixture()
    slowHeadline(d)
    const { container } = render(<ActivityScreen />)
    // MUTATION: draw the sweep until the headline's own read lands (the old `data === null` branch).
    await waitFor(() => expect(container.querySelector('.ol-ledger .ol-figure')).not.toBeNull())
    expect(container.querySelector('.ol-pending'), 'no whole-chart sweep once buckets have landed').toBeNull()
    expect(container.querySelector('.ol-figure')!.textContent).toBe('90.7 %')
    expect(container.querySelector('.ol-drawing'), 'the drawing itself is drawn').not.toBeNull()
    // What is still out is said in words, where the delta and the sealed days go.
    expect(container.querySelector('.ol-ledger .ol-reading')?.textContent).toBe('still reading the sealed days and the span before')
    expect(container.querySelector('.ol-delta'), 'no delta without the span before').toBeNull()
    const facts = container.querySelector('.ol-facts')!.textContent ?? ''
    expect(facts).toContain(`${d.totals.buckets_read} of ${d.totals.buckets} days read`)
    expect(facts, 'the sealed count is not invented before coverage lands').not.toMatch(/sealed/)
    // The chart asks for nothing of its own: its buckets are the cards' one shared read.
    const calls = api.loadOutcomes.mock.calls.filter((c) => sectionsOf(c).join(',') === 'buckets,totals')
    expect(calls.length).toBe(1)
  })
})

describe('VQ-022: Why agents failed draws no bar for 0, and cuts a long label before its bar', () => {
  it('draws a 0 class with no bar, and every label carries its full name as a title', async () => {
    serveRest()
    api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
    const { container } = render(<ActivityScreen />)
    await waitFor(() => expect(container.querySelector('.ol-failures .ol-row')).not.toBeNull())
    const c = card(container as HTMLElement, /^Why agents failed/)
    const timeout = c.querySelector('[data-row="timeout"]')!
    expect(timeout.querySelector('.ol-row-n')!.textContent).toBe('0')
    // MUTATION: draw `UsageTrack pct={0}` for a 0 row again in Ledger.tsx `Row`.
    expect(timeout.querySelector('.ctl-util-track'), 'a 0 row draws a bar stub').toBeNull()
    expect(timeout.querySelector('.ol-row-none'), 'the bar column keeps its cell').not.toBeNull()
    const runner = c.querySelector('[data-row="runner error"]')!
    expect(runner.querySelector('.ctl-util-track'), 'a non-zero row keeps its bar').not.toBeNull()
    const long = c.querySelector('[data-row="spec signature invalid"] .ol-row-name')!
    expect(long.getAttribute('title')).toBe('spec signature invalid')
    for (const env of [{ width: 1440 }, { width: 390 }]) {
      // MUTATION: drop `overflow: hidden` from `.ol-row-name` in styles.css.
      expect(painted(long, 'overflow', env)).toBe('hidden')
      expect(painted(long, 'text-overflow', env)).toBe('ellipsis')
      expect(painted(long, 'min-width', env)).toBe('0')
    }
    // ONE COUNT COLUMN AT EVERY WIDTH (review of VQ-022): the count is the
    // grid's next cell after the drawn ones before it, so a 0 row and a
    // non-zero row put it in the same column only if they draw as many cells
    // before it. At 390 the bar is hidden, so the empty cell must be too.
    const before = (row: Element, env: { width: number }) => {
      const cells = Array.from(row.children)
      const n = cells.indexOf(row.querySelector('.ol-row-n')!)
      return cells.slice(0, n).filter((el) => painted(el, 'display', env) !== 'none').length
    }
    for (const env of [{ width: 1440 }, { width: 390 }]) {
      // MUTATION: drop `.ol-row > .ol-row-none { display: none }` from the 560px block.
      expect(before(timeout, env), `a 0 row's count column at ${env.width}`).toBe(before(runner, env))
    }
  })
})

describe('VQ-024: at 390 the span control offers every span the desktop does, on one row', () => {
  it('draws 24h, 7d, 14d, 30d, 90d and from–to in the one control, and none in the sheet', async () => {
    serveRest()
    api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
    const { container } = render(<ActivityScreen />)
    await waitFor(() => expect(container.querySelector('.ol-ledger')).not.toBeNull())
    const span = container.querySelector<HTMLElement>('.ol-toolbar > .ol-span')!
    const labels = within(span).getAllByRole('button').map((b) => b.textContent)
    expect(labels).toEqual(['24h', '7d', '14d', '30d', '90d', 'from–to'])
    // MUTATION: put back `className: 'is-wide-only'` on 24h and from–to.
    for (const b of within(span).getAllByRole('button')) {
      expect(painted(b, 'display', { width: 390 }), `${b.textContent} is hidden on a phone`).not.toBe('none')
    }
    expect(container.querySelector('.ol-span-more'), 'a second span control in the sheet').toBeNull()
    expect(painted(span, 'flex-wrap', { width: 390 }), 'the spans wrap 3+1').toBe('nowrap')
    expect(painted(span, 'overflow-x', { width: 390 })).toBe('auto')
    expect(painted(span.querySelector('button')!, 'flex', { width: 390 }), 'the phone basis lays them three to a row').toBe('1 0 auto')
  })
})
