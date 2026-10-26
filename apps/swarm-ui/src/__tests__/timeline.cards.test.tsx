// EVERY TIMELINE CARD READS ITS OWN PART OF THE LEDGER, WHEN IT SCROLLS INTO VIEW (#377).
//
// Owner decision, 2026-09-30: per-card reads. The page used to read the whole
// ledger once, eagerly, and draw seven cards from it. Now the headline reads
// only what it draws, and each card sends its own `GET /v1/outcomes` with the
// page's filters and a `section` per block it needs -- when it comes into
// view, with a skeleton until then, a failure with "try again", and no late
// answer ever drawn.
//
// THE PAYLOADS HERE ARE STRICT. `loadOutcomes` answers with the envelope and
// ONLY the sections asked for, as the route does (`Outcomes.read`), so a card
// that reads a block it did not ask for throws on `undefined` and this file
// goes red. That is what holds `CARD_SECTIONS` to what each card draws.
//
// jsdom has no IntersectionObserver; `FakeObserver` stands in for it, as in
// timeline.lazy.test.tsx, and the last case holds the page without one.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Outcomes } from '../outcomes'
import { ledgerFixture } from '../outcomes.fixture'
import type { Me, Stats, TaskPage } from '../types'

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

const { ActivityScreen, LEDGER_LATE_SECTIONS, LEDGER_SECTIONS } = await import('../Activity')

// ---------------------------------------------------------------------------
// What each part of the page may read -- stated here, not imported, so a card
// that starts asking for more (or less) fails this file.
// ---------------------------------------------------------------------------

const ALL = ['buckets', 'totals', 'retries', 'latency', 'groups', 'workflows_failed', 'coverage', 'previous']
const LEDGER = ['buckets', 'totals']
/** The page's second read, after the headline lands (#2422): the two sections that cost reads of their own. */
const LATE = ['coverage', 'previous']
const CARDS: Record<string, string[]> = {
  failures: ['buckets', 'totals'],
  retries: ['buckets', 'retries', 'totals'],
  latency: ['buckets', 'latency', 'totals'],
  reliability: ['buckets', 'groups', 'totals'],
  workflows: ['buckets', 'totals', 'workflows_failed'],
  cost: ['buckets', 'totals'],
  cancels: ['buckets', 'totals'],
}
const IDS = Object.keys(CARDS)

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

function scroll(el: Element, inView: boolean): void {
  act(() => {
    for (const o of FakeObserver.all) {
      if (!o.targets.has(el)) continue
      const entry = { target: el, isIntersecting: inView, intersectionRatio: inView ? 1 : 0 } as unknown as IntersectionObserverEntry
      o.callback([entry], o as unknown as IntersectionObserver)
    }
  })
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const failed = (message: string): Result<never> => ({
  status: 'error',
  error: { kind: 'server', httpStatus: 503, code: null, message } as unknown as Extract<Result<never>, { status: 'error' }>['error'],
})

/** The envelope and only `sections`, as the route serves a sectioned read; everything with none. */
function part(sections: readonly string[] | undefined, edit?: (d: Outcomes) => void): Outcomes {
  const whole = ledgerFixture()
  edit?.(whole)
  const out: Record<string, unknown> = { ...whole }
  if (sections !== undefined && sections.length > 0) for (const s of ALL) if (!sections.includes(s)) delete out[s]
  return out as unknown as Outcomes
}

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
  return {
    tenant_id: 'eng',
    dispatch_paused: false,
    tasks_by_state: {
      SUBMITTED: 0, QUEUED: 2, PARKED: 3, READY: 1, LEASED: 1, DISPATCHED: 0,
      STARTING: 0, RUNNING: 2, SUCCEEDED: 272, FAILED: 28, CANCELLED: 416, DEAD_LETTERED: 0,
    },
    limits: {},
    generated_at: new Date().toISOString(),
  }
}

function serve(): void {
  api.loadOutcomes.mockImplementation((_q: URLSearchParams, sections?: readonly string[]) => Promise.resolve(ok(part(sections))))
  api.loadMe.mockResolvedValue(ok(me()))
  api.loadRunnerProfiles.mockResolvedValue(ok(['claude-code', 'mock']))
  api.loadStats.mockImplementation(() => Promise.resolve(ok(stats())))
  api.loadTasksInState.mockImplementation(() => Promise.resolve(ok<TaskPage>({ tasks: [], next_page_token: null })))
  api.loadTenants.mockResolvedValue(ok({ tenants: [] }))
}

type Call = { query: URLSearchParams; sections: string[] | null; who: 'headline' | 'late' | 'card' }

/**
 * Who sent a read: the page's two lists by identity, because the failures,
 * cost and cancels cards ask for the same two sections the headline does.
 */
function who(sections: unknown): Call['who'] {
  return sections === LEDGER_SECTIONS ? 'headline' : sections === LEDGER_LATE_SECTIONS ? 'late' : 'card'
}

/** Every `loadOutcomes` call so far, with its sections sorted; null when it named none. */
function calls(): Call[] {
  return api.loadOutcomes.mock.calls.map((c) => ({
    query: c[0] as URLSearchParams,
    sections: Array.isArray(c[1]) && c[1].length > 0 ? [...(c[1] as string[])].sort() : null,
    who: who(c[1]),
  }))
}

/** The calls that were not the page's own (the headline and its second read). */
function cardCalls(): Call[] {
  return calls().filter((c) => c.who === 'card')
}

/** A card's ask for `id`'s sections -- never the headline's, which can name the same two. */
const cardAsk = (sections: readonly string[] | undefined, id: string): boolean =>
  who(sections) === 'card' && same(sections ? [...sections].sort() : null, CARDS[id]!)

const same = (a: string[] | null, b: string[]) => a !== null && a.join() === b.join()

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 20))
  })
}

async function timeline(): Promise<HTMLElement> {
  const { container } = render(<ActivityScreen />)
  await waitFor(() => expect(container.querySelector('.ol-ledger')).not.toBeNull())
  await settle()
  return container as HTMLElement
}

/** A card's observed box, found afresh. */
function box(root: HTMLElement, id: string): HTMLElement {
  const el = root.querySelector<HTMLElement>(`[data-card="${id}"]`)
  expect(el, `no card box ${id}`).not.toBeNull()
  return el!
}

const loading = (el: HTMLElement) => el.querySelector('.ol-card.is-loading') !== null
const failing = (el: HTMLElement) => el.querySelector('.ol-card-failed') !== null
const loaded = (el: HTMLElement) => el.querySelector('.ol-card') !== null && !loading(el) && !failing(el)

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
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------

describe('with IntersectionObserver', () => {
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', FakeObserver)
  })

  it('reads only the headline’s sections on open, and no card before it is in view', async () => {
    const root = await timeline()
    const all = calls()
    expect(all.map((c) => c.who), 'the headline was read more than once, or a card read on open').toEqual(['headline', 'late'])
    expect(all[0]!.sections, 'the headline asked for more (or less) than it draws').toEqual(LEDGER)
    expect(all[1]!.sections, 'the second read asked for more (or less) than coverage and previous').toEqual(LATE)
    expect(all[1]!.query.toString(), 'the second read is the headline’s query').toBe(all[0]!.query.toString())
    // Drawn from the headline's sections alone.
    expect(root.querySelector('.ol-figure')!.textContent).toBe('90.7 %')
    for (const id of IDS) {
      const el = box(root, id)
      expect(loading(el), `${id} is not a skeleton before it is in view`).toBe(true)
      expect(el.querySelector('.ol-skel-bar')).not.toBeNull()
    }
  })

  it.each(IDS)('%s reads its own sections, with the page’s filters, and draws from them alone', async (id) => {
    const root = await timeline()
    const headline = calls()[0]!.query
    scroll(box(root, id), true)
    await waitFor(() => expect(loaded(box(root, id)), `${id} did not draw`).toBe(true))
    const mine = cardCalls()
    expect(mine.length, `${id} read ${mine.length} times`).toBe(1)
    expect(mine[0]!.sections).toEqual(CARDS[id])
    expect(mine[0]!.query.toString(), 'a card reads under the page’s filters').toBe(headline.toString())
    for (const other of IDS.filter((o) => o !== id && !same(CARDS[o]!, CARDS[id]!))) {
      expect(loading(box(root, other)), `${other} drew although only ${id} was in view`).toBe(true)
    }
  })

  it('does not read a card again when it scrolls away and back', async () => {
    const root = await timeline()
    scroll(box(root, 'retries'), true)
    await waitFor(() => expect(loaded(box(root, 'retries'))).toBe(true))
    scroll(box(root, 'retries'), false)
    scroll(box(root, 'retries'), true)
    await settle()
    expect(cardCalls().length).toBe(1)
  })

  it('shares one read between cards in view that ask for the same sections', async () => {
    const root = await timeline()
    scroll(box(root, 'failures'), true)
    scroll(box(root, 'cost'), true)
    scroll(box(root, 'cancels'), true)
    await waitFor(() => expect(['failures', 'cost', 'cancels'].every((id) => loaded(box(root, id)))).toBe(true))
    expect(cardCalls().length, 'three cards with one ask sent it more than once').toBe(1)
  })

  it('draws a card that scrolls in later from the read its twin already made, until the filters change', async () => {
    const root = await timeline()
    scroll(box(root, 'failures'), true)
    await waitFor(() => expect(loaded(box(root, 'failures'))).toBe(true))
    scroll(box(root, 'cost'), true)
    await waitFor(() => expect(loaded(box(root, 'cost'))).toBe(true))
    expect(cardCalls().length, 'the same ask was sent again').toBe(1)
    scroll(box(root, 'cost'), false)
    fireEvent.click(within(root.querySelector<HTMLElement>('.ol-span')!).getByRole('button', { name: '30d' }))
    await waitFor(() => expect(cardCalls().length).toBe(2))
    scroll(box(root, 'cancels'), true)
    await waitFor(() => expect(loaded(box(root, 'cancels'))).toBe(true))
    expect(cardCalls().length, 'the 30d read was not shared').toBe(2)
    expect(cardCalls()[1]!.query.get('span')).toBe('30d')
  })

  it('re-reads only the cards in view on a filter change, and marks the rest stale until they scroll in', async () => {
    const root = await timeline()
    scroll(box(root, 'retries'), true)
    scroll(box(root, 'latency'), true)
    await waitFor(() => expect(loaded(box(root, 'retries')) && loaded(box(root, 'latency'))).toBe(true))
    scroll(box(root, 'latency'), false)
    api.loadOutcomes.mockClear()

    fireEvent.click(within(root.querySelector<HTMLElement>('.ol-span')!).getByRole('button', { name: '30d' }))
    await waitFor(() => expect(cardCalls().length).toBeGreaterThan(0))
    await settle()
    const after = cardCalls()
    expect(after.map((c) => c.sections), 'only the card in view re-read').toEqual([CARDS.retries])
    expect(after[0]!.query.get('span')).toBe('30d')
    expect(box(root, 'retries').dataset.stale).toBeUndefined()

    const off = box(root, 'latency')
    expect(off.dataset.stale, 'an off-screen card was not marked stale').toBe('true')
    expect(off.classList.contains('ctl-stale-body')).toBe(true)
    expect(loaded(off), 'a stale card keeps its last figures, dimmed').toBe(true)
    // Never read, so nothing to mark: still the skeleton.
    expect(box(root, 'reliability').dataset.stale).toBeUndefined()
    expect(loading(box(root, 'reliability'))).toBe(true)

    scroll(off, true)
    await waitFor(() => expect(box(root, 'latency').dataset.stale).toBeUndefined())
    const back = cardCalls().filter((c) => same(c.sections, CARDS.latency!))
    expect(back.length).toBe(1)
    expect(back[0]!.query.get('span')).toBe('30d')
  })

  it('re-reads the cards in view on refresh, and no other', async () => {
    const root = await timeline()
    scroll(box(root, 'workflows'), true)
    await waitFor(() => expect(loaded(box(root, 'workflows'))).toBe(true))
    api.loadOutcomes.mockClear()
    fireEvent.click(within(root).getByRole('button', { name: /(^|· )refresh\b/i }))
    await waitFor(() => expect(cardCalls().length).toBe(1))
    await settle()
    expect(cardCalls().map((c) => c.sections)).toEqual([CARDS.workflows])
  })

  it('shows the skeleton in the card’s own frame while its read is pending', async () => {
    const pending = deferred<Result<Outcomes>>()
    api.loadOutcomes.mockImplementation((_q: URLSearchParams, sections?: readonly string[]) =>
      cardAsk(sections, 'retries') ? pending.promise : Promise.resolve(ok(part(sections))),
    )
    const root = await timeline()
    scroll(box(root, 'retries'), true)
    await waitFor(() => expect(cardCalls().length).toBe(1))
    const card = box(root, 'retries').querySelector<HTMLElement>('.ol-card')!
    expect(card.classList.contains('ol-retries'), 'the skeleton is not in the card’s place').toBe(true)
    expect(card.getAttribute('aria-busy')).toBe('true')
    expect(card.querySelector('.ctl-card-title')!.textContent).toBe('Retries and attempts')
    await act(async () => {
      pending.resolve(ok(part(CARDS.retries)))
    })
    await waitFor(() => expect(loaded(box(root, 'retries'))).toBe(true))
    expect(box(root, 'retries').querySelector('.ol-skel-bar')).toBeNull()
  })

  it.each(IDS)('%s shows a failure with a way to try again, and trying again reads again', async (id) => {
    let first = true
    api.loadOutcomes.mockImplementation((_q: URLSearchParams, sections?: readonly string[]) => {
      if (first && cardAsk(sections, id)) {
        first = false
        return Promise.resolve(failed('upstream timed out'))
      }
      return Promise.resolve(ok(part(sections)))
    })
    const root = await timeline()
    scroll(box(root, id), true)
    await waitFor(() => expect(failing(box(root, id))).toBe(true))
    expect(box(root, id).textContent).toContain('upstream timed out')
    expect(loading(box(root, id)), 'a failed read still looks like it is loading').toBe(false)
    fireEvent.click(within(box(root, id)).getByRole('button', { name: /try again/i }))
    await waitFor(() => expect(loaded(box(root, id))).toBe(true))
    expect(cardCalls().filter((c) => same(c.sections, CARDS[id]!)).length).toBe(2)
  })

  it('drops a read that lands after the card scrolled away, and reads again on return', async () => {
    const late = deferred<Result<Outcomes>>()
    let first = true
    api.loadOutcomes.mockImplementation((_q: URLSearchParams, sections?: readonly string[]) => {
      if (first && cardAsk(sections, 'reliability')) {
        first = false
        return late.promise
      }
      return Promise.resolve(ok(part(sections)))
    })
    const root = await timeline()
    scroll(box(root, 'reliability'), true)
    await waitFor(() => expect(cardCalls().length).toBe(1))
    scroll(box(root, 'reliability'), false)
    await act(async () => {
      late.resolve(failed('late and stale'))
    })
    await settle()
    expect(failing(box(root, 'reliability')), 'a late answer drew a failure').toBe(false)
    expect(box(root, 'reliability').textContent).not.toContain('late and stale')
    expect(loading(box(root, 'reliability'))).toBe(true)
    scroll(box(root, 'reliability'), true)
    await waitFor(() => expect(loaded(box(root, 'reliability'))).toBe(true))
    expect(cardCalls().length).toBe(2)
  })

  it('drops a read that lands after the filters changed, so an old figure is never drawn as new', async () => {
    const old = deferred<Result<Outcomes>>()
    api.loadOutcomes.mockImplementation((q: URLSearchParams, sections?: readonly string[]) => {
      if (cardAsk(sections, 'retries') && q.get('span') === '14d') return old.promise
      return Promise.resolve(ok(part(sections)))
    })
    const root = await timeline()
    scroll(box(root, 'retries'), true)
    await waitFor(() => expect(cardCalls().length).toBe(1))
    fireEvent.click(within(root.querySelector<HTMLElement>('.ol-span')!).getByRole('button', { name: '30d' }))
    await waitFor(() => expect(loaded(box(root, 'retries'))).toBe(true))
    await act(async () => {
      old.resolve(ok(part(CARDS.retries, (d) => {
        d.retries = { ...d.retries, rescued: 987 }
      })))
    })
    await settle()
    expect(box(root, 'retries').textContent, 'the 14d answer was drawn under 30d').not.toContain('987 rescued')
    expect(box(root, 'retries').dataset.stale).toBeUndefined()
  })

  it('draws a static placeholder, not the shimmer, under prefers-reduced-motion', async () => {
    reducedMotion(true)
    const root = await timeline()
    for (const id of IDS) {
      const bars = [...box(root, id).querySelectorAll('.ol-skel-bar')]
      expect(bars.length, `${id} has no skeleton`).toBeGreaterThan(0)
      for (const b of bars) {
        expect(b.classList.contains('is-static'), `${id} drew the shimmer under reduced motion`).toBe(true)
        expect(b.classList.contains('ctl-pending')).toBe(false)
      }
    }
  })

  it('draws no card at all when the headline’s read fails', async () => {
    api.loadOutcomes.mockImplementation(() => Promise.resolve(failed('the aggregate could not be read')))
    const { container } = render(<ActivityScreen />)
    await waitFor(() => expect(container.querySelector('.ctl-empty.is-failed')).not.toBeNull())
    expect(container.querySelector('[data-card]')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The second read: coverage and previous, after first paint (#2422)
// ---------------------------------------------------------------------------

describe('the headline never waits on coverage or previous (#2422)', () => {
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', FakeObserver)
  })

  /** The facts line's "sealed" item. */
  function sealedFact(root: HTMLElement): HTMLElement {
    const li = [...root.querySelectorAll<HTMLElement>('.ol-facts .ctl-fact')].find((el) => el.querySelector('b')?.textContent === 'sealed')
    expect(li, 'no sealed fact').toBeDefined()
    return li!
  }

  /** The foot's clauses, one per nowrap child of the run, as read. */
  function footClauses(root: HTMLElement): string[] {
    return [...root.querySelectorAll<HTMLElement>('.ol-prov > span')].map((el) => (el.textContent ?? '').trim())
  }
  /**
   * The second read's own clauses only: the headline's cost clause is
   * pre-existing and outside this check. The text includes the mark's pill, as
   * drawn. The review measured the joined 57-character clause ("reading sealed
   * days and the span before: still being read") running 57 px past a 390 px
   * foot, cut at "still"; 40 characters leaves it well inside.
   */
  const FOOT_CLAUSE_MAX = 40
  function longest(root: HTMLElement): number {
    return Math.max(...footClauses(root).filter((t) => t.includes('sealed days') || t.includes('span before')).map((t) => t.length))
  }

  /** Serve every read at once except the second, which waits on `late`. */
  function holdLate(late: Promise<Result<Outcomes>>): void {
    api.loadOutcomes.mockImplementation((_q: URLSearchParams, sections?: readonly string[]) =>
      who(sections) === 'late' ? late : Promise.resolve(ok(part(sections))),
    )
  }

  it('draws the figure while the second read is out, and says what it is still reading', async () => {
    // MUTATION: put 'coverage' and 'previous' back in LEDGER_SECTIONS, or draw `coverage.days` as 0 until it lands.
    const late = deferred<Result<Outcomes>>()
    holdLate(late.promise)
    const root = await timeline()
    expect(root.querySelector('.ol-figure')!.textContent, 'the figure waited on the second read').toBe('90.7 %')
    const sealed = sealedFact(root)
    expect(sealed.querySelector('.ctl-mark.is-pending'), 'sealed does not say it is reading').not.toBeNull()
    expect(sealed.textContent).toContain('still being read')
    expect(sealed.textContent, 'a count stood in for days not read yet').not.toMatch(/\d/)
    expect(root.querySelector('.ol-delta.is-reading')?.textContent).toContain('still being read')
    const foot = root.querySelector('.ol-prov')!
    expect(foot.querySelector('.ctl-mark.is-pending')).not.toBeNull()
    expect(foot.textContent).not.toContain('sealed ·')
    // MUTATION: join the two back into one clause. A nowrap clause longer than a
    // 390 px foot is cut by its clip (#2422 review: "still" kept, "being read" lost).
    expect(footClauses(root)).toEqual(expect.arrayContaining(['reading sealed days: still being read', 'span before: still being read']))
    expect(longest(root), 'a foot clause too long to fit a phone').toBeLessThanOrEqual(FOOT_CLAUSE_MAX)

    const whole = ledgerFixture()
    await act(async () => {
      late.resolve(ok(part(LATE)))
    })
    await waitFor(() => expect(sealedFact(root).querySelector('.ctl-mark')).toBeNull())
    expect(sealedFact(root).textContent).toContain(`${whole.coverage.days.sealed} of ${whole.coverage.days.total}`)
    expect(root.querySelector('.ol-delta.is-reading')).toBeNull()
    expect(root.querySelector('.ol-delta')?.textContent).toMatch(/^prev /)
    expect(root.querySelector('.ol-prov')!.textContent).toContain(`${whole.coverage.days.sealed} UTC days sealed`)
  })

  it('says coverage and the span before were not read, and why, when the second read fails -- never 0', async () => {
    // MUTATION: draw a failed second read as "0 of N", or as nothing at all.
    holdLate(Promise.resolve(failed('count timed out')))
    const root = await timeline()
    await waitFor(() => expect(sealedFact(root).querySelector('.ctl-mark.is-unread')).not.toBeNull())
    const sealed = sealedFact(root)
    expect(sealed.textContent).toContain('not read')
    expect(sealed.querySelector('.ctl-mark')!.getAttribute('aria-label')).toContain('count timed out')
    expect(sealed.textContent).not.toMatch(/\d/)
    const delta = root.querySelector('.ol-delta.is-unread')
    expect(delta?.textContent).toContain('not read, no delta')
    expect(delta?.querySelector('.ctl-mark')!.getAttribute('aria-label')).toContain('count timed out')
    expect(root.querySelector('.ol-ledger')!.textContent).toContain('terminal agents without completed_at: not read')
    expect(footClauses(root)).toEqual(expect.arrayContaining(['not read sealed days: not read', 'span before: not read']))
    expect(longest(root), 'a foot clause too long to fit a phone').toBeLessThanOrEqual(FOOT_CLAUSE_MAX)
    expect(root.querySelector('.ol-figure')!.textContent, 'a failed second read took the figure with it').toBe('90.7 %')
  })
})

describe('without IntersectionObserver', () => {
  it('reads every card at once, each with its own sections, as a browser without the API must', async () => {
    vi.stubGlobal('IntersectionObserver', undefined)
    const root = await timeline()
    await waitFor(() => expect(IDS.every((id) => loaded(box(root, id)))).toBe(true))
    const asked = new Set(cardCalls().map((c) => (c.sections ?? []).join()))
    expect(asked).toEqual(new Set(Object.values(CARDS).map((s) => s.join())))
  })
})
