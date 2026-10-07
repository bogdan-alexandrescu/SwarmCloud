// A TIMELINE CARD READS ITS OWN CONTENT WHEN IT SCROLLS INTO VIEW (#377).
//
// Owner request, 2026-09-30. The page-level ledger read (`GET /v1/outcomes`)
// decides which cards exist and feeds seven of the eight, so it stays eager.
// The one card with content of its own -- "Not finished yet", which reads
// `/v1/stats` and the PARKED list -- reads only once it is near the viewport,
// shows a skeleton in its own shape while the read is in flight, and a failed
// read is a failure with a way to try again, never a card that looks loaded.
//
// jsdom has no IntersectionObserver, so these tests install a double
// (`FakeObserver`) and move the card in and out of view by hand. Without the
// double the page must read at once, as a browser without the API does -- the
// last case holds that, and every case in activity.timeline.test.tsx runs that
// way.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { Me, Stats, Task, TaskPage } from '../types'

const api = vi.hoisted(() => ({
  loadOutcomes: vi.fn(),
  loadMe: vi.fn(),
  loadRunnerProfiles: vi.fn(),
  loadStats: vi.fn(),
  loadTasksInState: vi.fn(),
  loadTenants: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { ActivityScreen } = await import('../Activity')

// ---------------------------------------------------------------------------
// The IntersectionObserver double
// ---------------------------------------------------------------------------

class FakeObserver {
  static all: FakeObserver[] = []
  readonly targets = new Set<Element>()
  constructor(
    readonly callback: IntersectionObserverCallback,
    readonly options: IntersectionObserverInit = {},
  ) {
    FakeObserver.all.push(this)
  }
  observe(el: Element): void {
    this.targets.add(el)
  }
  unobserve(el: Element): void {
    this.targets.delete(el)
  }
  disconnect(): void {
    this.targets.clear()
  }
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

/** Move `el` into or out of view, as the browser reports it to every observer watching it. */
function scroll(el: Element, inView: boolean): void {
  act(() => {
    for (const o of FakeObserver.all) {
      if (!o.targets.has(el)) continue
      const entry = { target: el, isIntersecting: inView, intersectionRatio: inView ? 1 : 0 } as unknown as IntersectionObserverEntry
      o.callback([entry], o as unknown as IntersectionObserver)
    }
  })
}

/** The element the card's observer watches. */
function watched(root: HTMLElement): Element {
  const el = [...FakeObserver.all.flatMap((o) => [...o.targets])].find((t) => root.contains(t) && /Not finished yet/.test(t.textContent ?? ''))
  expect(el, 'nothing observes the Not finished yet card').toBeTruthy()
  return el!
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const failed = (message: string): Result<never> => ({
  status: 'error',
  error: { kind: 'server', httpStatus: 503, code: null, message } as unknown as Extract<Result<never>, { status: 'error' }>['error'],
})

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
  return {
    tenant_id: 'eng',
    dispatch_paused: false,
    tasks_by_state: {
      SUBMITTED: 0, QUEUED: 2, PARKED: 3, READY: 1, LEASED: 1, DISPATCHED: 0,
      STARTING: 0, RUNNING: 2, SUCCEEDED: 272, FAILED: 28, CANCELLED: 416, DEAD_LETTERED: 0,
    },
    limits: {},
    generated_at: new Date(Date.now() - 40_000).toISOString(),
  }
}

const PARKED: TaskPage = {
  tasks: [{ id: 'p1', state: 'PARKED', park_reason: 'CREDENTIAL_MISSING' } as unknown as Task],
  next_page_token: null,
}

function serve(): void {
  api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
  api.loadMe.mockResolvedValue(ok(me()))
  api.loadRunnerProfiles.mockResolvedValue(ok(['claude-code', 'mock']))
  api.loadStats.mockImplementation(() => Promise.resolve(ok(stats())))
  api.loadTasksInState.mockImplementation(() => Promise.resolve(ok(PARKED)))
  api.loadTenants.mockResolvedValue(ok({ tenants: [] }))
}

async function timeline(): Promise<HTMLElement> {
  const { container } = render(<ActivityScreen />)
  await waitFor(() => expect(container.querySelector('.ol-ledger')).not.toBeNull())
  return container as HTMLElement
}

/** The card, found afresh: its element is replaced as it moves from skeleton to content. */
function openCard(root: HTMLElement): HTMLElement {
  const h = [...root.querySelectorAll('.ol-card .ctl-card-title')].find((el) => /^Not finished yet/.test(el.textContent ?? ''))
  expect(h, 'no Not finished yet card').toBeTruthy()
  return h!.closest('.ol-card') as HTMLElement
}

function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

function reducedMotion(reduce: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: reduce && query.includes('prefers-reduced-motion: reduce'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset()
  FakeObserver.all = []
  try {
    window.localStorage.clear()
  } catch {
    // no storage in this environment
  }
  serve()
})

afterEach(() => {
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------

describe('Not finished yet reads when it scrolls into view', () => {
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', FakeObserver)
  })

  it('does not read while the card is off-screen, and draws its skeleton in the meantime', async () => {
    const root = await timeline()
    // Let every effect and resolved promise of the page's own reads settle.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })
    expect(api.loadOutcomes, 'the page-level ledger read must stay eager').toHaveBeenCalled()
    expect(api.loadStats, 'the card read its counts before it was in view').not.toHaveBeenCalled()
    expect(api.loadTasksInState, 'the card read its parked list before it was in view').not.toHaveBeenCalled()
    const c = openCard(root)
    expect(c.querySelector('.ol-skel-bar'), 'an unread card must show its skeleton').not.toBeNull()
    expect(c.querySelector('.ol-open-counts')).toBeNull()
  })

  it('watches with a small rootMargin, so the next card starts reading just before it appears', async () => {
    const root = await timeline()
    watched(root)
    const margins = FakeObserver.all.map((o) => o.options.rootMargin ?? '')
    expect(margins.some((m) => /[1-9]\d*px/.test(m)), `no observer had a positive rootMargin: ${margins.join(' | ')}`).toBe(true)
  })

  it('reads exactly once when it enters view, and not again on leaving and re-entering', async () => {
    const root = await timeline()
    scroll(watched(root), true)
    await waitFor(() => expect(openCard(root).querySelector('.ol-open-counts')).not.toBeNull())
    expect(api.loadStats).toHaveBeenCalledTimes(1)
    expect(api.loadTasksInState).toHaveBeenCalledTimes(1)
    scroll(watched(root), false)
    scroll(watched(root), true)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })
    expect(api.loadStats, 'scrolling past a loaded card read it again').toHaveBeenCalledTimes(1)
    expect(api.loadTasksInState).toHaveBeenCalledTimes(1)
  })

  it('shows the skeleton in the card’s own shape while the read is pending, and replaces it with the data', async () => {
    const pending = deferred<Result<Stats>>()
    api.loadStats.mockImplementation(() => pending.promise)
    const root = await timeline()
    scroll(watched(root), true)
    await waitFor(() => expect(api.loadStats).toHaveBeenCalledTimes(1))
    const c = openCard(root)
    // The card's own frame, title and classes: the grid keeps its place.
    expect(c.classList.contains('ctl-card')).toBe(true)
    expect(c.classList.contains('ol-open')).toBe(true)
    expect(c.getAttribute('aria-busy')).toBe('true')
    expect(c.querySelectorAll('.ol-skel-bar').length, 'the skeleton has the card’s lines').toBeGreaterThanOrEqual(3)
    expect(c.querySelector('.ol-open-counts')).toBeNull()
    await act(async () => {
      pending.resolve(ok(stats()))
    })
    await waitFor(() => expect(openCard(root).querySelector('.ol-open-counts')).not.toBeNull())
    const loaded = openCard(root)
    expect(loaded.querySelector('.ol-skel-bar'), 'the skeleton outlived the data').toBeNull()
    expect(loaded.getAttribute('aria-busy')).not.toBe('true')
    expect(loaded.querySelector('.ol-open-counts')!.textContent).toContain('3 parked · 3 running · 2 queued · 1 ready')
  })

  it('shows a failure with a way to try again, never an empty card, and trying again reads again', async () => {
    api.loadStats
      .mockImplementationOnce(() => Promise.resolve(failed('upstream timed out')))
      .mockImplementation(() => Promise.resolve(ok(stats())))
    const root = await timeline()
    scroll(watched(root), true)
    await waitFor(() => expect(openCard(root).querySelector('.ol-card-failed')).not.toBeNull())
    const c = openCard(root)
    expect(c.textContent).toContain('upstream timed out')
    expect(c.querySelector('.ol-open-counts'), 'a failed read drew counts').toBeNull()
    expect(c.querySelector('.ol-skel-bar'), 'a failed read still looks like it is loading').toBeNull()
    const retry = within(c).getByRole('button', { name: /try again/i })
    fireEvent.click(retry)
    await waitFor(() => expect(api.loadStats).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(openCard(root).querySelector('.ol-open-counts')).not.toBeNull())
    expect(openCard(root).querySelector('.ol-card-failed')).toBeNull()
  })

  it('treats a failed parked list as a failed card too, with a way to try again', async () => {
    api.loadTasksInState
      .mockImplementationOnce(() => Promise.resolve(failed('list refused')))
      .mockImplementation(() => Promise.resolve(ok(PARKED)))
    const root = await timeline()
    scroll(watched(root), true)
    await waitFor(() => expect(openCard(root).querySelector('.ol-card-failed')).not.toBeNull())
    expect(openCard(root).textContent).toContain('list refused')
    fireEvent.click(within(openCard(root)).getByRole('button', { name: /try again/i }))
    await waitFor(() => expect(openCard(root).textContent).toContain('No provider key is registered for this tenant 1'))
    expect(api.loadTasksInState).toHaveBeenCalledTimes(2)
  })

  it('does not flash a failure for a read that finishes after the card scrolled away, and reads again on return', async () => {
    const first = deferred<Result<Stats>>()
    api.loadStats.mockImplementationOnce(() => first.promise).mockImplementation(() => Promise.resolve(ok(stats())))
    const root = await timeline()
    scroll(watched(root), true)
    await waitFor(() => expect(api.loadStats).toHaveBeenCalledTimes(1))
    scroll(watched(root), false)
    await act(async () => {
      first.resolve(failed('late and stale'))
    })
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })
    expect(openCard(root).querySelector('.ol-card-failed'), 'a stale response drew a failure').toBeNull()
    expect(openCard(root).textContent).not.toContain('late and stale')
    expect(openCard(root).querySelector('.ol-skel-bar')).not.toBeNull()
    scroll(watched(root), true)
    await waitFor(() => expect(openCard(root).querySelector('.ol-open-counts')).not.toBeNull())
    expect(api.loadStats).toHaveBeenCalledTimes(2)
  })

  it('draws the moving shimmer by default', async () => {
    reducedMotion(false)
    const root = await timeline()
    const bars = [...openCard(root).querySelectorAll('.ol-skel-bar')]
    expect(bars.length).toBeGreaterThan(0)
    for (const b of bars) {
      expect(b.classList.contains('ctl-pending'), 'the skeleton does not move').toBe(true)
      expect(b.classList.contains('is-static')).toBe(false)
    }
  })

  it('draws a static placeholder, not the shimmer, under prefers-reduced-motion', async () => {
    reducedMotion(true)
    const root = await timeline()
    const bars = [...openCard(root).querySelectorAll('.ol-skel-bar')]
    expect(bars.length).toBeGreaterThan(0)
    for (const b of bars) {
      expect(b.classList.contains('is-static'), 'reduced motion still drew the shimmer').toBe(true)
      expect(b.classList.contains('ctl-pending')).toBe(false)
    }
  })
})

describe('without IntersectionObserver', () => {
  it('reads at once, as a browser without the API must', async () => {
    vi.stubGlobal('IntersectionObserver', undefined)
    const root = await timeline()
    await waitFor(() => expect(openCard(root).querySelector('.ol-open-counts')).not.toBeNull())
    expect(api.loadStats).toHaveBeenCalledTimes(1)
  })
})
