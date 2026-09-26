// U8: EVERY SCREEN STILL DRAWS THE SAME STATES, THROUGH ONE SET OF PRIMITIVES.
//
// WHAT CHANGED. Overview and AgentDetail each carried their own tile, their own
// utilisation track and their own empty state, and Capacity and Holders drew
// their tracks by hand: six hand-written `.ctl-util-track`s in four files,
// three pairs of wrappers that had already diverged (design-system.md §9.4).
// They were collapsed onto `primitives.tsx`, and Overview's injected
// `OVERVIEW_CSS` was folded into `styles.css`.
//
// WHAT THIS FILE PROVES, AND HOW. A refactor is correct when every screen shows
// what it showed before, so these tests were written against the screens as
// they stood BEFORE the collapse and pushed first, on their own, where they
// passed -- that run is the record of what the screens drew. They then pass
// unchanged against the collapsed screens. The one assertion that failed on the
// old screens is marked below: it is the divergence §9.4 named, fixed on
// purpose, not a state that moved by accident.
//
// WHAT IS ASSERTED IS THE STATE, NOT THE GEOMETRY. The honesty rules are
// absent != zero, unknown is hatched, a verdict is a hue, and over-ceiling is
// drawn rather than clipped. Each is a class or an element the reader's eye
// resolves to a different picture. Pixel widths are asserted only where two
// renderings agreed exactly before and after.
//
// AND THE STRUCTURE, at the foot: one file draws each primitive. Without that
// half, a seventh copy of the track could keep every state below and still be
// the next place one is lost.

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { AgentRun, HoldersBoard, SpendRollup } from '../api'
import type { Result } from '../fetch'
import {
  GIB,
  type Account,
  type AccountsPage,
  type Capacity,
  type LeasePage,
  type LeaseRow,
  type Pool,
  type ProfileAdmission,
  type RunnerProfile,
  type Stats,
  type TaskPage,
} from '../types'
import { attempt, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadTasks: vi.fn(),
  loadLeases: vi.fn(),
  loadProviders: vi.fn(),
  loadAccountPool: vi.fn(),
  loadWorkflows: vi.fn(),
  loadStats: vi.fn(),
  loadSpend: vi.fn(),
  loadHolders: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { OverviewScreen } = await import('../Overview')
const { Run } = await import('../AgentDetail')
const { CapacityScreen } = await import('../Capacity')
const { HoldersScreen } = await import('../Holders')

const WAIT = { timeout: 5000 } as const

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-24T10:00:00Z' }
}

function never<T>(): Promise<Result<T>> {
  return new Promise(() => {})
}

// ---------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------

function pool(over: Partial<Pool>): Pool {
  return {
    name: 'global',
    hard_limit: 4,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 4,
    active: 0,
    available: 4,
    enabled: true,
    updated_at: '2026-09-24T10:00:00Z',
    ...over,
  }
}

function admission(over: Partial<ProfileAdmission>): ProfileAdmission {
  return {
    units: 1,
    headroom: 3,
    basis: 'measured',
    blockers: [],
    binding: [],
    counterfactual: [],
    complete: true,
    unread: [],
    uncapped: [],
    ...over,
  }
}

function profile(pools: string[], a: ProfileAdmission): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools, admission: a }
}

function account(over: Partial<Account>): Account {
  return {
    account_id: 'eng:laptop',
    owner_tenant: 'eng',
    label: 'laptop',
    provider: 'anthropic-subscription',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: {},
    observed_at: null,
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  }
}

const NOW_ISO = new Date().toISOString()

/** One profile per track state, each bound by its own pool. */
const CAPACITY: Capacity = {
  pools: [
    pool({ name: 'pool:zero', active: 0 }),
    pool({ name: 'pool:some', active: 1 }),
    pool({ name: 'pool:over', active: 6 }),
    pool({ name: 'pool:paused', active: 2, enabled: false }),
  ],
  runner_profiles: {
    // A required pool nobody could read: no binding pool, so no ceiling.
    'p-unread': profile(
      ['pool:gone'],
      admission({ headroom: null, basis: 'unknown', complete: false, unread: ['pool:gone'] }),
    ),
    'p-zero': profile(['pool:zero'], admission({ headroom: 4, binding: ['pool:zero'] })),
    'p-some': profile(['pool:some'], admission({ headroom: 3, binding: ['pool:some'] })),
    'p-over': profile(['pool:over'], admission({ headroom: 0, binding: ['pool:over'] })),
    'p-paused': profile(['pool:paused'], admission({ headroom: 0, binding: ['pool:paused'] })),
  },
  tenant_id: 'eng',
  generated_at: '2026-09-24T10:00:00Z',
}

const ACCOUNTS: AccountsPage = {
  accounts: [
    account({ account_id: 'eng:never', label: 'never', observed_at: null }),
    account({
      account_id: 'eng:zero',
      label: 'zero',
      observed_at: NOW_ISO,
      windows: { five_hour: { utilization: 0, resets_at: '2099-01-01T00:00:00Z', reset: false } },
    }),
    account({
      account_id: 'eng:stale',
      label: 'stale',
      observed_at: NOW_ISO,
      stale: true,
      windows: { five_hour: { utilization: 0.5, resets_at: '2099-01-01T00:00:00Z', reset: false } },
    }),
  ],
  tenant_id: 'eng',
  unreadable_documents: [],
  unreadable_document_count: 0,
}

const STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: {},
  dispatch_paused: false,
  limits: {},
  generated_at: '2026-09-24T10:00:00Z',
}

const TASKS: TaskPage = { tasks: [], next_page_token: null }

function spend(): SpendRollup {
  return {
    tenantId: 'eng',
    tasksOnPage: 1,
    tasksWithAttempts: 1,
    tasksSampled: 1,
    failedReads: 0,
    failedDetail: null,
    attempts: 1,
    attemptsWithCost: 1,
    attemptsWithTokens: 0,
    costUsd: 0.25,
    inputTokens: null,
    outputTokens: null,
    cacheReadTokens: null,
    cacheCreationTokens: null,
    from: '2026-09-24T09:00:00Z',
    to: '2026-09-24T10:00:00Z',
  }
}

function renderOverview(reads: Partial<Record<keyof typeof api, Result<unknown> | Promise<Result<unknown>>>> = {}) {
  api.loadCapacity.mockResolvedValue(ok(CAPACITY))
  api.loadTasks.mockResolvedValue(ok(TASKS))
  api.loadLeases.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadProviders.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadWorkflows.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadStats.mockResolvedValue(ok(STATS))
  api.loadAccountPool.mockResolvedValue(ok(ACCOUNTS))
  api.loadSpend.mockResolvedValue(ok(spend()))
  for (const [name, result] of Object.entries(reads)) {
    const fn = api[name as keyof typeof api]
    if (result instanceof Promise) fn.mockReturnValue(result)
    else fn.mockResolvedValue(result)
  }
  return render(<OverviewScreen />)
}

/** The `.ctl-util` row whose name starts with `name`. */
function row(name: string): Element {
  const found = [...document.querySelectorAll('.ctl-util')].find(
    (r) => (r.querySelector('.ctl-util-name b')?.textContent ?? '').trim() === name,
  )
  expect(found, `no .ctl-util row is named ${name}`).toBeTruthy()
  return found!
}

function track(r: Element): Element {
  const t = r.querySelector('.ctl-util-track')
  expect(t, 'the row drew no track').not.toBeNull()
  return t!
}

/** The one tile whose label reads `label`. */
function tile(label: string): Element {
  const found = [...document.querySelectorAll('.ctl-metric')].find(
    (t) => (t.querySelector('.ctl-metric-label')?.childNodes[0]?.textContent ?? '').trim() === label,
  )
  expect(found, `no tile is labelled ${label}`).toBeTruthy()
  return found!
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

describe('Overview draws the four track states, through the shared track', () => {
  it('hatches an unreadable ceiling, ticks a measured zero, fills a reading', async () => {
    renderOverview()
    await screen.findByText('p-zero', undefined, WAIT)

    const unread = track(row('p-unread'))
    expect(unread.classList.contains('is-unknown'), 'an unreadable ceiling is not hatched').toBe(true)
    expect(unread.querySelector('.ctl-util-fill'), 'a hatched track carries a fill').toBeNull()

    const zero = track(row('p-zero'))
    expect(zero.classList.contains('is-zero'), 'a measured zero lost its baseline').toBe(true)
    expect(zero.querySelector('.ctl-util-zero'), 'a measured zero draws no tick').not.toBeNull()
    expect(zero.querySelector('.ctl-util-fill'), 'a measured zero drew a fill').toBeNull()

    const some = track(row('p-some'))
    expect(some.classList.contains('is-unknown') || some.classList.contains('is-zero')).toBe(false)
    const fill = some.querySelector<HTMLElement>('.ctl-util-fill')
    expect(fill, 'a reading drew no fill').not.toBeNull()
    expect(fill!.style.width).toBe('25%')
    // Nothing to say, so no verdict: the fill is the monochrome default.
    expect(fill!.className.trim()).toBe('ctl-util-fill')
  })

  it('draws over-ceiling as a bad fill plus a hatched excess, and a paused pool as paused', async () => {
    renderOverview()
    await screen.findByText('p-over', undefined, WAIT)

    const over = track(row('p-over'))
    expect(over.querySelector('.ctl-util-fill.is-bad'), 'an over-ceiling pool is not a verdict').not.toBeNull()
    expect(over.querySelector('.ctl-util-over'), 'the excess is clipped instead of drawn').not.toBeNull()

    const paused = track(row('p-paused'))
    expect(paused.querySelector('.ctl-util-fill.is-paused'), 'a paused pool is not drawn as held').not.toBeNull()
    expect(paused.querySelector('.ctl-util-over')).toBeNull()
  })

  it('keeps an unpolled account hatched, a zero account ticked, and a stale one grey', async () => {
    renderOverview()
    await screen.findByText('never', undefined, WAIT)

    expect(track(row('never')).classList.contains('is-unknown')).toBe(true)
    expect(row('never').querySelector('.ctl-util-figure')?.textContent).toBe('—')

    // OV-1: EVERY % CARRIES ITS WORD. The rows printed a bare `%` under a
    // headline printing `% left`, so the same glyph meant two opposite things
    // one line apart. They are % used, and say so.
    const zero = track(row('zero'))
    expect(zero.classList.contains('is-zero')).toBe(true)
    expect(row('zero').querySelector('.ctl-util-figure')?.textContent).toBe('0% used')

    // PROJECTED: real, not current. The documented grey, never a verdict hue.
    const stale = track(row('stale'))
    const fill = stale.querySelector('.ctl-util-fill')
    expect(fill?.classList.contains('ov-projected'), 'a stale reading lost its projected grey').toBe(true)
    expect(fill?.classList.contains('is-warn') || fill?.classList.contains('is-bad')).toBe(false)
    expect(row('stale').querySelector('.ctl-util-figure')?.textContent).toBe('~50% used')
  })
})

describe('Overview draws the four tile states, through the shared tile', () => {
  /**
   * TWO TILES CARRY THE FOUR PICTURES NOW (OV-12). `Token spend` and `Account
   * headroom` left the strip -- each drew a figure its own panel draws -- so
   * the four renderings are asked of the two facts that remain: Running
   * (unread, then pending in a second render) and Units held (absent, then a
   * figure that is the doorway to Pools).
   */
  it('a failed read, a read in flight, an absence and a figure stay four pictures', async () => {
    const first = renderOverview({
      loadStats: { status: 'error', error: { kind: 'server_error', httpStatus: 500, code: null, message: 'boom' } },
    })
    await screen.findByText('p-zero', undefined, WAIT)
    await screen.findByText('never', undefined, WAIT)
    await waitFor(() => expect(tile('Running').classList.contains('is-unread')).toBe(true), WAIT)

    // Two facts, and neither is a figure a panel below draws again.
    const labels = [...document.querySelectorAll('.ctl-metrics .ctl-metric-label')].map((l) =>
      (l.childNodes[0]?.textContent ?? '').trim(),
    )
    expect(labels).toEqual(['Running', 'Units held'])

    // UNREAD: the read failed. A mark, no digit.
    const running = tile('Running')
    expect(running.querySelector('.ctl-metric-value .ctl-mark.is-unread')).not.toBeNull()
    expect(running.querySelector('.ctl-metric-value')?.textContent).not.toMatch(/\d/)

    // ABSENT: no global pool is configured, so nothing reports the figure.
    const units = tile('Units held')
    expect(units.classList.contains('is-absent')).toBe(true)
    expect(units.querySelector('.ctl-metric-value .ctl-mark.is-absent')).not.toBeNull()
    expect(units.querySelector('.ctl-metric-value')?.textContent).not.toMatch(/\d/)
    first.unmount()

    // SECOND RENDER: the count still in flight, and a global pool to count.
    renderOverview({
      loadStats: never(),
      loadCapacity: ok({ ...CAPACITY, pools: [...CAPACITY.pools, pool({ name: 'global', active: 2 })] }),
    })
    await screen.findByText('p-zero', undefined, WAIT)

    // READING: still in flight. Neither of the two absences, and no word.
    const pending = tile('Running')
    expect(pending.querySelector('.ctl-metric-value .ctl-pending')).not.toBeNull()
    expect(pending.classList.contains('is-absent') || pending.classList.contains('is-unread')).toBe(false)
    expect(pending.querySelector('.ctl-mark')).toBeNull()

    // A FIGURE, and the doorway it is: the whole tile is the link.
    const held = tile('Units held')
    expect(held.tagName).toBe('A')
    expect(held.getAttribute('href')).toBe('#capacity/pools')
    expect(held.querySelector('.ctl-metric-value')?.textContent).toMatch(/\d/)
    expect(held.getAttribute('aria-label')).toMatch(/^Units held\. /)
  })
})

describe('Overview draws its empty states through the shared empty state', () => {
  it('a failed read is marked `not read`, a real zero `real zero`, inside the card', async () => {
    renderOverview({
      loadCapacity: {
        status: 'error',
        error: { kind: 'server_error', httpStatus: 503, code: null, message: 'down' },
      },
      loadTasks: { status: 'empty', fetchedAt: Date.now() },
    })
    await screen.findAllByText(/Pool state unread/, undefined, WAIT)
    await screen.findAllByText(/No task exists/, undefined, WAIT)

    const failed = [...document.querySelectorAll('.ctl-empty.ov-empty.is-failed')].find((e) =>
      (e.querySelector('h3')?.textContent ?? '').includes('Pool state unread'),
    )
    expect(failed, 'the unread pool state is not a failed empty state').toBeTruthy()
    expect(failed!.querySelector('.ctl-mark.is-unread')?.textContent).toBe('not read')
    expect(failed!.textContent).not.toMatch(/\d/)

    const zero = [...document.querySelectorAll('.ctl-empty.ov-empty')].find((e) =>
      (e.querySelector('h3')?.textContent ?? '').includes('No task exists'),
    )
    expect(zero, 'an empty task list is not a real-zero empty state').toBeTruthy()
    expect(zero!.classList.contains('is-failed')).toBe(false)
    expect(zero!.querySelector('.ctl-mark.is-zero')?.textContent).toBe('real zero')
    // The sentence is the mark's accessible name, not a paragraph.
    expect(zero!.querySelector('.ctl-mark')?.getAttribute('aria-label')).toMatch(/real zero/i)
    expect(zero!.querySelector('p')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// AgentDetail
// ---------------------------------------------------------------------------

function agentRun(over: Partial<AgentRun> = {}): AgentRun {
  return {
    task: task({ state: 'SUCCEEDED', attempt_count: 1 }),
    events: [],
    eventsDetail: null,
    attempts: [attempt(1, { peak_rss_bytes: 2 * GIB, peak_disk_bytes: 0 })],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  }
}

async function mountRun(r: AgentRun): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={r} />)
  await waitFor(() => expect(container.querySelector('.ctl-metrics')).not.toBeNull(), WAIT)
  return container as HTMLElement
}

describe('AgentDetail draws requested-vs-utilised through the shared track', () => {
  it('hatches every bar when the ceiling could not be read', async () => {
    await mountRun(agentRun({ classes: null, classesDetail: 'HTTP 503' }))
    for (const name of ['memory', 'workspace', 'cpu']) {
      const t = track(row(name))
      expect(t.classList.contains('is-unknown'), `${name} has no ceiling and is not hatched`).toBe(true)
      expect(t.querySelector('.ctl-util-fill'), `${name} drew a fill with no ceiling`).toBeNull()
    }
  })

  // #184: cpu is sampled now; an attempt row served with no `usage` reading
  // -- this fixture's -- is one hatched `not served` row, never a zero.
  it('fills a measured peak, and hatches cpu when the attempt serves no reading', async () => {
    await mountRun(agentRun())
    const memory = track(row('memory')).querySelector<HTMLElement>('.ctl-util-fill')
    expect(memory, 'a measured peak drew no fill').not.toBeNull()
    expect(memory!.style.width).toBe('25%')
    expect(memory!.className.trim()).toBe('ctl-util-fill')
    expect(track(row('cpu')).classList.contains('is-unknown')).toBe(true)
  })

  it('draws a peak over its ceiling as a bad fill to the ceiling plus the excess', async () => {
    await mountRun(agentRun({ attempts: [attempt(1, { peak_rss_bytes: 10 * GIB, peak_disk_bytes: GIB })] }))
    const t = track(row('memory'))
    const fill = t.querySelector<HTMLElement>('.ctl-util-fill.is-bad')
    expect(fill, 'an over-ceiling peak is not a verdict').not.toBeNull()
    // 8 GiB of 10: the ceiling's share of what was used. Unchanged by U8.
    expect(fill!.style.width).toBe('80%')
    expect(t.querySelector<HTMLElement>('.ctl-util-over')?.style.width).toBe('20%')
  })

  // THE ONE STATE U8 CHANGES ON PURPOSE (design-system.md §9.4). This screen's
  // own track drew a measured 0 B workspace as a zero-width fill -- a widget
  // that failed to paint, beside a figure that says 0 -- while Overview's drew
  // the baseline tick. One track now, so the tick is drawn here too. This
  // assertion is the one that failed against the pre-collapse screen.
  it('draws a measured zero with the baseline tick, as every other track does', async () => {
    await mountRun(agentRun())
    const t = track(row('workspace'))
    expect(t.classList.contains('is-zero'), 'a measured 0 B draws no baseline').toBe(true)
    expect(t.querySelector('.ctl-util-zero')).not.toBeNull()
    expect(t.querySelector('.ctl-util-fill'), 'a measured zero drew a zero-width fill').toBeNull()
    expect(row('workspace').querySelector('.ctl-util-figure')?.textContent).toMatch(/^0\b/)
  })

  it('keeps a failed attempt read on the unread tile, with no figure', async () => {
    await mountRun(agentRun({ attempts: null, attemptsDetail: 'HTTP 503' }))
    const peak = tile('Peak memory')
    expect(peak.classList.contains('is-unread')).toBe(true)
    expect(peak.querySelector('.ctl-metric-value')?.textContent).not.toMatch(/\d/)
    const failed = document.querySelector('.ctl-empty.is-failed')
    expect(failed?.querySelector('.ctl-mark.is-unread')?.textContent).toBe('not read')
    expect(failed?.getAttribute('role')).toBe('status')
  })
})

// ---------------------------------------------------------------------------
// Capacity and Holders
// ---------------------------------------------------------------------------

describe('Capacity draws each pool card through the shared track', () => {
  it('a ceiling of 0 fills in its mark’s tone, a zero is ticked, a reading fills, and the track is a meter', async () => {
    api.loadCapacity.mockResolvedValue(
      ok<Capacity>({
        pools: [
          pool({ name: 'backend:a', hard_limit: 0, effective_limit: 0, active: 0, available: 0 }),
          pool({ name: 'backend:b', active: 0 }),
          pool({ name: 'backend:c', active: 2 }),
          pool({ name: 'backend:d', active: 5 }),
          pool({ name: 'backend:e', active: 1, enabled: false }),
        ],
        runner_profiles: {},
        tenant_id: 'eng',
        generated_at: '2026-09-24T10:00:00Z',
      }),
    )
    render(<CapacityScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Cards' }, WAIT))
    await waitFor(() => expect(document.querySelectorAll('.cap-pool').length).toBe(5), WAIT)

    const card = (name: string) => {
      const c = [...document.querySelectorAll('.cap-pool')].find(
        (p) => p.querySelector('.cap-pool-name')?.getAttribute('title') === name,
      )
      expect(c, `no card for ${name}`).toBeTruthy()
      return track(c!)
    }

    // #159 RE-POINT (CP-14, #85). This asserted `backend:a`, at a ceiling of 0,
    // drew the hatched `is-unknown` track: the card used to read a 0 ceiling
    // as "no ceiling". CP-14 made the tile say `/ 0` and carry the `limit 0`
    // mark, because a ceiling of 0 WAS read and admits nothing -- and the
    // review of #159 found the hatch still beside them, saying "not
    // measured" about the one figure on the tile that was. A `backend:` pool
    // only a person writes, so its limit 0 is the paused tone, and the track
    // is full in it: no room, measured.
    const a = card('backend:a')
    expect(a.classList.contains('is-unknown'), 'a ceiling that was read is drawn as not measured').toBe(false)
    expect(a.querySelector<HTMLElement>('.ctl-util-fill.is-paused')?.style.width).toBe('100%')

    const b = card('backend:b')
    expect(b.classList.contains('is-zero')).toBe(true)
    expect(b.querySelector('.ctl-util-zero')).not.toBeNull()

    const c = card('backend:c').querySelector<HTMLElement>('.ctl-util-fill')
    expect(c?.style.width).toBe('50%')
    expect(c?.className.trim()).toBe('ctl-util-fill')

    // Over the ceiling: the card says so with a chip, and the bar is bad and
    // FULL -- this screen has never drawn an overflow segment.
    const d = card('backend:d')
    expect(d.querySelector<HTMLElement>('.ctl-util-fill.is-bad')?.style.width).toBe('100%')
    expect(d.querySelector('.ctl-util-over')).toBeNull()

    expect(card('backend:e').querySelector('.ctl-util-fill.is-paused')).not.toBeNull()

    for (const name of ['backend:a', 'backend:b', 'backend:c', 'backend:d', 'backend:e']) {
      const t = card(name)
      expect(t.getAttribute('role'), `${name}'s track is not a meter`).toBe('meter')
      expect(t.getAttribute('aria-label')).toMatch(/units in use$/)
    }
  })
})

function lease(over: Partial<LeaseRow>): LeaseRow {
  return {
    lease_id: 'lease-aaaaaaaaaa',
    task_id: 'task-bbbbbbbbbb',
    attempt_id: 'att-1',
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
    ...over,
  }
}

describe('Holders draws its class mix through the shared row', () => {
  it('draws each class as a fill proportional to the largest', async () => {
    const board: HoldersBoard = {
      page: {
        leases: [
          lease({}),
          lease({ lease_id: 'lease-cccccccccc', units: 4, pools: ['global', 'resource:large'] }),
        ],
        units_held: 5,
        tenant_id: null,
        active_only: true,
        active_beyond_window: 0,
        truncated: false,
        examined: 2,
      } as unknown as LeasePage,
      pools: [pool({ name: 'global', active: 5 })],
      poolsDetail: null,
    }
    api.loadHolders.mockResolvedValue(ok(board))
    render(<HoldersScreen />)
    await screen.findByText('Class mix', undefined, WAIT)

    const large = row('large').querySelector<HTMLElement>('.ctl-util-fill')
    const standard = row('standard').querySelector<HTMLElement>('.ctl-util-fill')
    expect(large?.style.width).toBe('100%')
    expect(standard?.style.width).toBe('25%')
    expect(row('large').querySelector('.ctl-util-figure')?.textContent).toBe('4u')
    expect(row('standard').querySelector('.ctl-util-by')?.textContent).toBe('1 lease')
  })
})

// ---------------------------------------------------------------------------
// One definition of each primitive
// ---------------------------------------------------------------------------

const SOURCES = import.meta.glob<string>('../*.tsx', { query: '?raw', import: 'default', eager: true })

/** A screen's source with its comments blanked, so a comment naming a class is not a use of it. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/^\s*\/\/.*$/gm, ' ')
}

function drawnBy(pattern: RegExp): string[] {
  return Object.entries(SOURCES)
    .filter(([, text]) => pattern.test(code(text)))
    .map(([path]) => path.replace('../', ''))
    .sort()
}

describe('each primitive is drawn by one file', () => {
  it('read the screens it claims to', () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(20)
    expect(Object.keys(SOURCES)).toContain('../Overview.tsx')
    expect(Object.keys(SOURCES)).toContain('../AgentDetail.tsx')
  })

  it('draws the utilisation track in exactly one place', () => {
    // Six were hand-written in four files: Overview (three branches),
    // AgentDetail, Capacity and Holders.
    expect(drawnBy(/className=[{"'`][^>]*\bctl-util-track\b/)).toEqual(['primitives.tsx'])
  })

  it('draws the metric tile in exactly one place', () => {
    expect(drawnBy(/className="ctl-metric-label"/)).toEqual(['primitives.tsx'])
  })

  it('defines Mark, Metric, Absent and the track once', () => {
    // `Tile` (Overview's strip policy), `CardAbsent` (the in-card variant) and
    // `CeilingRow` (AgentDetail's used/ceiling) are callers of these, not
    // copies: none of them draws the primitive's markup.
    for (const name of ['Mark', 'Metric', 'Absent', 'UtilTrack', 'Util']) {
      expect(drawnBy(new RegExp(`function ${name}\\(`)), `${name} is defined more than once`).toEqual([
        ...(name === 'Util' ? [] : ['primitives.tsx']),
      ])
    }
  })
})
