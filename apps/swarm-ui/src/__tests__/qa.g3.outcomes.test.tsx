/**
 * BROWSER QA G3 (2026-10-07, swarm.saga.xyz at 1440 and 390): Timeline ›
 * Outcomes.
 *
 *   G3-01  On a partial span the headline, its readout and "list them"
 *          disagreed with the cards and the drawer: `173 failed in the span →
 *          list them` opened `291 failed · in this span`, the cards were whole
 *          while the headline was 6 of 30 days, and the build did not continue
 *          until the 60 s poll or a click.
 *   G3-08  "24h" was two spans: Outcomes reads whole hours from 22:00, Lanes
 *          the last 24 hours to the minute, and neither said so.
 *   G3-16  The span's two drill links ran together (`list them203 cancelled`).
 *   G3-17  Full service-account addresses pushed Reliability's and Workflows
 *          that failed's last columns past the card at 1440.
 *   G3-24  `clears itself DEPENDENCY_INCOMPLETE 8`: a raw enum on the page.
 *
 * MUTATIONS, each turns a case red: print the readout's count on a partial
 * span's drill link; drop the continuation timer (or keep it firing when a
 * read built nothing); drop `alignedWords` from the note or the span title;
 * drop `display: flex` / the gap from `.ol-span-drill`; print `r.key` whole in
 * the Person column; print the raw park reason again.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, waitFor } from '@testing-library/react'

import STYLES from '../styles.css?raw'
import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import { alignedWords, instantLabel, type Outcomes, type OutcomeBucket } from '../outcomes'
import type { Me, Stats, Task, TaskPage } from '../types'
import { cascade } from './cssgate'

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

const { ActivityScreen, CONTINUE_BUILD_MS, buildContinues } = await import('../Activity')
const { ReliabilityCard, WorkflowsFailedCard, OpenWorkCard } = await import('../Ledger')
const { parseView } = await import('../outcomes')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })

function me(): Me {
  return {
    tenant: {
      tenant_id: 'eng', kind: 'group', principal: 'eng@saga.xyz', display_name: null,
      created_at: '2026-09-01T00:00:00Z', max_active: 10, capacity_units: 20, monthly_budget_usd: null,
      enabled: true, credentials: [], service_account: null, gcs_prefix: null, namespace: null,
    },
    principal: { email: 'a@saga.xyz', domain: 'saga.xyz', groups: [], is_admin: false },
    environment: 'dev',
    environment_declared: true,
  }
}

function stats(): Stats {
  const byState = {
    SUBMITTED: 0, QUEUED: 0, PARKED: 8, READY: 0, LEASED: 0, DISPATCHED: 0,
    STARTING: 0, RUNNING: 0, SUCCEEDED: 1, FAILED: 0, CANCELLED: 0, DEAD_LETTERED: 0,
  }
  return { tenant_id: 'eng', dispatch_paused: false, tasks_by_state: byState, limits: {}, generated_at: new Date().toISOString() }
}

function unread(b: OutcomeBucket): OutcomeBucket {
  return {
    ...b, state: 'unread', unread_reason: 'derive_budget', submitted: null, ended: null, succeeded: null,
    failed: null, dead_lettered: null, cancelled: null, rate: null, failure_classes: null, cost: null,
  }
}

/**
 * The fixture built 6 of its 14 days: the first eight past the derive budget,
 * the totals summed over the other six, and this read having built some.
 */
function partial(derived = 6): Outcomes {
  const d = ledgerFixture()
  d.buckets = d.buckets.map((b, i) => (i < 8 ? unread(b) : b))
  const read = d.buckets.filter((b) => b.state !== 'unread')
  const sum = (f: (b: OutcomeBucket) => number) => read.reduce((n, b) => n + f(b), 0)
  d.totals = {
    ...d.totals,
    complete: false,
    buckets_read: read.length,
    succeeded: sum((b) => b.succeeded ?? 0),
    failed: sum((b) => b.failed ?? 0),
    dead_lettered: 0,
    cancelled: { ...d.totals.cancelled, total: sum((b) => b.cancelled?.total ?? 0) },
  }
  d.coverage = { ...d.coverage, derived_now: derived }
  return d
}

/** The headline's read asks for `coverage`; no card does. */
const isHeadline = (sections: readonly string[]) => sections.includes('coverage')

function serve(headline: () => Outcomes, card: () => Outcomes = ledgerFixture, parkReasons: string[] = []): void {
  api.loadOutcomes.mockImplementation((_q: URLSearchParams, sections: readonly string[]) =>
    Promise.resolve(ok(isHeadline(sections) ? headline() : card())),
  )
  api.loadMe.mockResolvedValue(ok(me()))
  api.loadRunnerProfiles.mockResolvedValue(ok(['claude-code', 'mock']))
  api.loadStats.mockImplementation(() => Promise.resolve(ok(stats())))
  api.loadTasksInState.mockResolvedValue(
    ok<TaskPage>({ tasks: parkReasons.map((r, i) => ({ id: `p${i}`, state: 'PARKED', park_reason: r }) as unknown as Task), next_page_token: null }),
  )
  api.loadTenants.mockResolvedValue(ok({ tenants: [] }))
}

const headlineReads = () => api.loadOutcomes.mock.calls.filter((c) => isHeadline(c[1] as string[])).length

async function page(view: string | null = null): Promise<HTMLElement> {
  const { container } = render(<ActivityScreen view={view} onView={() => {}} />)
  await waitFor(() => expect(container.querySelector('.ol-ledger')).not.toBeNull())
  return container as HTMLElement
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset()
  try {
    window.localStorage.clear()
  } catch {
    // no storage here; the page must not need it
  }
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------
// G3-01
// ---------------------------------------------------------------------------

describe('G3-01: a partial span', () => {
  it('(b) prints no readout count on the span drill links, which open the whole span’s rows', async () => {
    serve(() => partial())
    const root = await page()
    const d = partial()
    const links = [...root.querySelectorAll<HTMLAnchorElement>('.ol-span-drill .ol-drill')]
    expect(links.map((l) => l.textContent)).toHaveLength(2)
    for (const l of links) {
      // The readout's sums over 6 built days are not the drawer's total.
      expect(l.textContent).not.toMatch(/^\s*\d/)
      expect(l.textContent).toContain('whole span')
    }
    expect(links[0]!.textContent).not.toContain(`${d.totals.failed} failed`)
  })

  it('(b) prints the count again once the span is whole, where it is the drawer’s', async () => {
    serve(ledgerFixture)
    const root = await page()
    const t = ledgerFixture().totals
    const link = root.querySelector<HTMLAnchorElement>('.ol-span-drill .ol-drill')!
    expect(link.textContent).toContain(`${t.failed + t.dead_lettered} failed in the span`)
  })

  it('(a) asks the next read by itself while the build continues, and the headline becomes whole', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let n = 0
    serve(() => (n++ === 0 ? partial() : ledgerFixture()))
    const root = await page()
    expect(root.querySelector('.ol-partial')?.textContent).toContain('6 of 14 days')
    expect(root.querySelector('.ol-building')?.textContent).toContain('still building')
    expect(headlineReads()).toBe(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONTINUE_BUILD_MS + 50)
    })
    await waitFor(() => expect(headlineReads()).toBe(2))
    await waitFor(() => expect(root.querySelector('.ol-partial')).toBeNull())
  })

  it('(a) stops continuing when a read built nothing more, leaving it to the poll', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    serve(() => partial(0))
    await page()
    // The first read of a query is always continued once; the second built nothing.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONTINUE_BUILD_MS + 50)
    })
    await waitFor(() => expect(headlineReads()).toBe(2))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONTINUE_BUILD_MS * 3)
    })
    expect(headlineReads()).toBe(2)
  })

  it('(a) continues only a budget-partial payload that built something', () => {
    expect(buildContinues(ledgerFixture(), null), 'a whole span').toBe(false)
    expect(buildContinues(partial(6), 6), 'it built days this read').toBe(true)
    expect(buildContinues(partial(0), null), 'the first read of a query').toBe(true)
    expect(buildContinues(partial(0), 6), 'nothing built, nothing more read').toBe(false)
    const failedRead = partial(6)
    failedRead.buckets = failedRead.buckets.map((b) => (b.state === 'unread' ? { ...b, unread_reason: 'read_failed' } : b))
    expect(buildContinues(failedRead, null), 'a failed read is not a build in progress').toBe(false)
  })

  it('(c) says on the headline when the cards cover the whole span and the headline does not', async () => {
    serve(() => partial(0))
    const root = await page()
    await waitFor(() => expect(root.querySelector('.ol-cover-split')).not.toBeNull())
    expect(root.querySelector('.ol-cover-split')!.textContent).toBe('cards: whole span · headline: 6 of 14 days built')
  })

  it('(c) says nothing of the kind when the headline is whole', async () => {
    serve(ledgerFixture)
    const root = await page()
    await waitFor(() => expect(root.querySelector('[data-card="failures"] .ol-card, [data-card="failures"] .ctl-card')).not.toBeNull())
    expect(root.querySelector('.ol-cover-split')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// G3-08
// ---------------------------------------------------------------------------

/** The fixture as a 24h read at 20:58 that the route started at the hour, 22:00 the day before. */
function hourAligned(): Outcomes {
  const d = ledgerFixture()
  d.requested = { ...d.requested, span: '24h' }
  d.bucket = 'hour'
  d.generated_at = '2026-10-07T03:58:00Z'
  d.until = d.generated_at
  d.since = '2026-10-06T05:00:00Z'
  return d
}

describe('G3-08: one span name, two ranges', () => {
  it('names how a named span was aligned, and nothing for one the route did not move', () => {
    const d = hourAligned()
    expect(alignedWords(d)).toBe(`24h · hour-aligned from ${instantLabel(d.since, d.tz)}`)
    const exact = hourAligned()
    exact.since = new Date(Date.parse(exact.generated_at) - 24 * 3_600_000).toISOString()
    expect(alignedWords(exact)).toBeNull()
    const range = hourAligned()
    range.requested = { ...range.requested, span: null }
    expect(alignedWords(range)).toBeNull()
  })

  it('prints the alignment in the note and the chosen span’s title, and both ranges on the Lanes link', async () => {
    serve(hourAligned)
    const root = await page('span=24h')
    const d = hourAligned()
    const words = alignedWords(d)!
    await waitFor(() => expect(root.querySelector('.ol-aligned')?.textContent).toContain(words))
    const chosen = [...root.querySelectorAll<HTMLButtonElement>('.ol-span button')].find((b) => b.textContent === '24h')!
    expect(chosen.title).toBe(words)
    const lanes = root.querySelector<HTMLAnchorElement>('.tl-pages a[href="/timeline"]')!
    expect(lanes.title).toContain('Lanes reads the last 24h to the minute')
    expect(lanes.title).toContain(words)
  })
})

// ---------------------------------------------------------------------------
// G3-16
// ---------------------------------------------------------------------------

describe('G3-16: the span drill links are two links', () => {
  it('lays them out as a wrapping row with a gap', async () => {
    serve(ledgerFixture)
    const root = await page()
    const row = root.querySelector('.ol-span-drill')!
    expect(cascade(STYLES, row, 'display', { width: 1440 }).winner?.value).toBe('flex')
    expect(cascade(STYLES, row, 'flex-wrap', { width: 1440 }).winner?.value).toBe('wrap')
    expect(cascade(STYLES, row, 'gap', { width: 1440 }).winner?.value).toBe('var(--ctl-s3)')
  })
})

// ---------------------------------------------------------------------------
// G3-17
// ---------------------------------------------------------------------------

describe('G3-17: a person is the local part, the address in the title', () => {
  const addr = 'swarm-' + 'verify-runner' + '@example-project.iam.gserviceaccount.com'

  it('prints Reliability’s person by the local part, capped at 220px', () => {
    const d = ledgerFixture()
    d.groups = { ...d.groups, by: 'submitted_by', rows: [{ ...d.groups.rows[0]!, key: addr }] }
    const { container } = render(<ReliabilityCard data={d} group="submitted_by" platform={false} onGroup={() => {}} />)
    const who = container.querySelector<HTMLElement>('tbody th .ol-who')!
    expect(who.textContent).toBe('swarm-verify-runner')
    expect(who.title).toBe(addr)
    expect(cascade(STYLES, who, 'max-width', { width: 1440 }).winner?.value).toBe('220px')
    expect(cascade(STYLES, who, 'text-overflow', { width: 1440 }).winner?.value).toBe('ellipsis')
  })

  it('prints Workflows that failed’s submitter the same way', () => {
    const d = ledgerFixture()
    const row = d.workflows_failed.rows[0]!
    d.workflows_failed = { ...d.workflows_failed, rows: [{ ...row, submitted_by: addr }] }
    const { container } = render(<WorkflowsFailedCard data={d} spanLabel="30d" />)
    const who = container.querySelector<HTMLElement>('tbody .ol-who')!
    expect(who.textContent).toBe('swarm-verify-runner')
    expect(who.title).toBe(addr)
  })
})

// ---------------------------------------------------------------------------
// G3-24
// ---------------------------------------------------------------------------

describe('G3-24: park reasons in words', () => {
  it('prints reasonCopy’s words with the count, and the token only in the title', () => {
    const parked = Array.from({ length: 8 }, (_, i) => ({ id: `p${i}`, state: 'PARKED', park_reason: 'DEPENDENCY_INCOMPLETE' }) as unknown as Task)
    const { container } = render(
      <OpenWorkCard
        open={{ stats: { status: 'ok', data: stats() }, parked: { status: 'ok', data: { tasks: parked, next_page_token: null } } }}
        view={parseView('')}
        tenant="eng"
        now={Date.now()}
      />,
    )
    const line = container.querySelector('.ol-itself')!
    expect(line.textContent).toBe('clears itself Waiting on an earlier step in its workflow 8')
    expect(line.textContent).not.toContain('DEPENDENCY_INCOMPLETE')
    expect(line.querySelector<HTMLElement>('.ol-reason')!.title).toBe('DEPENDENCY_INCOMPLETE')
  })
})
