// THE FOLLOW-UP TO #185's LEDGER: WHY A TASK ENDED, SAID AS THE ROUTE NOW KNOWS IT.
//
// The owner's decisions of 2026-09-25 on #185 that reach the page:
//
//   2  A cascade that began at a cancel somebody asked for is "after a
//      cancel", not "after a failure". The scheduler writes the same words for
//      both, so lane 3 drew them as one outline. The route splits them now, and
//      lane 3 draws the new cause as the two cancel marks TS-4 already has,
//      together: the flat "ended" bars (a cancel) inside the 1px outline (a
//      cascade). The readout and the Table say which is which. The review of
//      #217 found the bars invisible at 3-4px -- a page-anchored pattern whose
//      gap fell inside the outline -- so the cases below measure the bar
//      pixels inside each outline, in every drawing, rather than the rect.
//   7  The row-window Timeline's stylesheet (`.window-bar`, `.chart .col`,
//      `.stackcol`, `.chart-legend`, `.col-label`, ...) is deleted, and the
//      tests that read it re-pointed at the ledger's own rules.
//   8  Park reasons in platform scope are the caller's own tenant's -- the
//      tasks route is tenant-scoped -- and the card says so wherever it draws
//      a reason, or the absence of one.
//
// And the review of #196: `workflows_failed` counts are null, not 0, when the
// block does not apply (kind=standalone).
//
// And three findings of the post-deploy QA of #197 (epic #222, 2026-09-26, at
// 1440 and 390): the decided lane's "0" and its failed-side max printed into
// each other, with no minimum gap between scale labels where the inspector
// charts' `ValueAxis` keeps one; the provenance foot of a cache hit said
// "0 reads this request" and "N days built by this read" together; and the
// "requested (and other)" bars, anchored to the SVG's origin, painted one row
// of a 3px mark, against lane 3's baseline rule. Pushed before the fix.
//
// WRITTEN TO FAIL ON THE PAGE BEFORE THE CHANGE, IN VITEST. Every payload the
// new causes need is written through a cast (`as unknown as Record<...>`), so
// this file typechecks against the old `outcomes.ts` and the run reaches the
// assertions: #197's first red run failed at tsc and proved nothing, and this
// one must not. The bar-width case is a PIN (the ledger already capped its
// bars); it carries the claim `shell.test.tsx`'s "move 3" made of the deleted
// `.chart .col`.

import STYLES from '../styles.css?raw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'

import { LEDGER_DRAWN } from '../charts/OutcomeLedger'
import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { Outcomes } from '../outcomes'
import type { Me, Stats, Task, TaskPage, Tenant } from '../types'
import { cascade, flatRules, type CascadeEnv } from './cssgate'

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

const WIDE: CascadeEnv = { width: 1440 }
const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })

function me(admin: boolean): Me {
  return {
    tenant: {
      tenant_id: 'eng', kind: 'group', principal: 'eng@saga.xyz', display_name: null,
      created_at: '2026-09-01T00:00:00Z', max_active: 10, capacity_units: 20, monthly_budget_usd: null,
      enabled: true, credentials: [], service_account: null, gcs_prefix: null, namespace: null,
    },
    principal: { email: 'a@saga.xyz', domain: 'saga.xyz', groups: [], is_admin: admin },
    environment: 'dev',
    environment_declared: true,
  }
}

/** eng has 3 parked; the platform 14. The card must never set the one against the other. */
function stats(admin: boolean): Stats {
  const byState = {
    SUBMITTED: 0, QUEUED: 2, PARKED: 3, READY: 1, LEASED: 1, DISPATCHED: 0,
    STARTING: 0, RUNNING: 2, SUCCEEDED: 272, FAILED: 28, CANCELLED: 416, DEAD_LETTERED: 0,
  }
  return {
    tenant_id: 'eng',
    dispatch_paused: false,
    tasks_by_state: byState,
    ...(admin ? { platform_tasks_by_state: { ...byState, PARKED: 14 } } : {}),
    limits: {},
    generated_at: new Date(Date.now() - 40_000).toISOString(),
  }
}

function parked(id: string, reason: string): Task {
  return { id, state: 'PARKED', park_reason: reason } as unknown as Task
}

function serve(payload: Outcomes, opts: { admin?: boolean; parked?: TaskPage } = {}): void {
  api.loadOutcomes.mockResolvedValue(ok(payload))
  api.loadMe.mockResolvedValue(ok(me(opts.admin === true)))
  api.loadRunnerProfiles.mockResolvedValue(ok(['browser', 'claude-code', 'codex', 'generic', 'mock']))
  api.loadStats.mockImplementation(() => Promise.resolve(ok(stats(opts.admin === true))))
  api.loadTasksInState.mockResolvedValue(
    ok<TaskPage>(
      opts.parked ?? {
        tasks: [parked('p1', 'CREDENTIAL_MISSING'), parked('p2', 'CREDENTIAL_MISSING'), parked('p3', 'PROVIDER_QUOTA_EXHAUSTED')],
        next_page_token: null,
      },
    ),
  )
  api.loadTenants.mockResolvedValue(ok({ tenants: ['eng', 'personal', 'verify'].map((tenant_id) => ({ tenant_id }) as Tenant) }))
}

async function timeline(
  payload: Outcomes = ledgerFixture(),
  props: { view?: string | null } = {},
  opts: { admin?: boolean; parked?: TaskPage } = {},
): Promise<HTMLElement> {
  serve(payload, opts)
  const { container } = render(<ActivityScreen {...props} />)
  await waitFor(() => expect(container.querySelector('.ol-ledger, .ctl-empty')).not.toBeNull())
  return container as HTMLElement
}

/** 22 Sep, the fixture's bucket 10: 297 requested, 5 after a failure, 3 swept -- and now 4 after a cancel. */
const SEP22 = 10
const AFTER_CANCEL = 4

/** The fixture with 22 Sep's cascade split as the route now serves it, written through a cast. */
function withCancelCascade(): Outcomes {
  const d = ledgerFixture()
  const c = d.buckets[SEP22]!.cancelled as unknown as Record<string, number>
  c.after_cancel = AFTER_CANCEL
  c.total = (c.total ?? 0) + AFTER_CANCEL
  const b = d.buckets[SEP22] as unknown as Record<string, number>
  b.ended = (b.ended ?? 0) + AFTER_CANCEL
  const t = d.totals.cancelled as unknown as Record<string, number>
  t.after_cancel = AFTER_CANCEL
  t.total = (t.total ?? 0) + AFTER_CANCEL
  return d
}

function cols(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>('.ol-drawing.is-wide .ol-col')]
}

function marksOf(root: HTMLElement, i: number): Element {
  const g = root.querySelector(`.ol-drawing.is-wide .ol-bucket[data-i="${i}"]`)
  expect(g, `bucket ${i} drew no marks`).not.toBeNull()
  return g!
}

function keyed(root: HTMLElement, key: string): string | null {
  const li = [...root.querySelectorAll('.ol-legend .ol-li')].find((x) => x.querySelector(`.ol-k.${key}`) !== null)
  return li === undefined ? null : (li.querySelector('.ol-n')?.textContent ?? '').trim()
}

function card(root: HTMLElement, title: RegExp): HTMLElement {
  const h = [...root.querySelectorAll('.ol-card .ctl-card-title')].find((el) => title.test(el.textContent ?? ''))
  expect(h, `no card titled ${title}`).toBeTruthy()
  return h!.closest('.ol-card') as HTMLElement
}

const num = (el: Element, attr: string, fallback = 0): number => {
  const v = el.getAttribute(attr)
  return v === null ? fallback : Number(v)
}

/** Length of the overlap of [a0, a1) and [b0, b1), in px. */
const overlap = (a0: number, a1: number, b0: number, b1: number): number => Math.max(0, Math.min(a1, b1) - Math.max(a0, b0))

/**
 * HOW MANY PIXELS OF TS-4's FLAT BARS SHOW INSIDE AN OUTLINE, measured down
 * the column, however the bars are drawn: through a `-flat` pattern -- whose
 * bars repeat from the PATTERN's origin, not the mark's -- or as bars of their
 * own under the `.ol-flat` rule. "Inside" is between the inner edges of the
 * outline's 1px stroke, which is centred on the rect's edge.
 *
 * This is the check the review of #217 said was missing: the old mark kept a
 * patterned rect (so "the bars exist" passed) whose bars, at 3-4px on the
 * baseline, all fell under the stroke or in the pattern's 2px gap.
 */
function barsInside(root: HTMLElement, marks: Element, outline: Element): number {
  const top = num(outline, 'y') + 0.5
  const bottom = num(outline, 'y') + num(outline, 'height') - 0.5
  if (bottom <= top) return 0
  return paintedIn(
    root,
    [...marks.querySelectorAll('rect')].filter((el) => el !== outline),
    top,
    bottom,
  )
}

/**
 * How many pixels of TS-4's flat bars the given rects paint between `top` and
 * `bottom`, down the column: a patterned rect through its pattern's bars,
 * repeated from the PATTERN's origin (its `y`), a `.ol-flat` rect as itself.
 */
function paintedIn(root: HTMLElement, rects: Iterable<Element>, top: number, bottom: number): number {
  let px = 0
  for (const el of rects) {
    const y0 = num(el, 'y')
    const y1 = y0 + num(el, 'height')
    const url = /^url\(#(.+)\)$/.exec(el.getAttribute('fill') ?? '')
    if (url !== null) {
      const pattern = [...root.querySelectorAll('pattern')].find((p) => p.getAttribute('id') === url[1])
      const bar = pattern?.querySelector('rect.ol-flat')
      if (pattern === undefined || bar === null || bar === undefined) continue
      expect(pattern.getAttribute('patternTransform'), 'the resolver does not read a transformed pattern').toBeNull()
      const py = num(pattern, 'y')
      const ph = num(pattern, 'height')
      for (let k = Math.floor((y0 - py) / ph) - 1; py + k * ph < y1; k++) {
        const b0 = py + k * ph + num(bar, 'y')
        const b1 = b0 + num(bar, 'height')
        px += overlap(Math.max(b0, y0), Math.min(b1, y1), top, bottom)
      }
    } else if (el.classList.contains('ol-flat')) {
      px += overlap(y0, y1, top, bottom)
    }
  }
  return Math.round(px * 100) / 100
}

/**
 * One whole TS-4 flat bar, 3px: what an after-a-cancel mark must show inside
 * its outline, as its key does. A 1px sliver between two 1px strokes reads as
 * a solid 3px block, not as bars in an outline -- so "some bar pixel" is not
 * the bar, and a mark too short to hold a whole one is the defect again.
 */
const WHOLE_BAR = 3

/** Lane 3's baseline in one drawing: the third of the scale's four solid rules. */
function laneThreeBase(root: HTMLElement, key: string): number {
  const rules = [...root.querySelectorAll(`.ol-drawing.is-${key} .ol-scale line.ol-base`)]
  expect(rules.length, 'the scale has four solid rules: rate 0, decided 0, cancelled, throughput').toBe(4)
  return num(rules[2]!, 'y1')
}

const hosts: HTMLElement[] = []
function fragment(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  hosts.push(host)
  return host
}
function won(el: Element, prop: string | readonly string[]): string | null {
  const r = cascade(STYLES, el, prop, { ...WIDE, states: [] })
  expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
  return r.winner?.value ?? null
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
  for (const h of hosts.splice(0)) h.remove()
})

// ---------------------------------------------------------------------------
// Decision 2: after a cancel is its own mark, its own key, its own words
// ---------------------------------------------------------------------------

describe('lane 3 splits the cascade', () => {
  it('draws after a cancel as the flat bars inside the outline, beside the bare outline a failure takes', async () => {
    // MUTATION: fold after_cancel back into the outline, or draw it without the bars.
    const root = await timeline(withCancelCascade())
    const marks = marksOf(root, SEP22)
    const outline = marks.querySelector('.ol-m-after-cancel')
    expect(outline, 'after a cancel has no outline of its own').not.toBeNull()
    expect(barsInside(root, marks, outline!), 'after a cancel is not drawn as a cancel (a whole flat bar)').toBeGreaterThanOrEqual(WHOLE_BAR)
    // The failure's cascade is still the bare outline, and still its own mark.
    const after = marks.querySelector('.ol-m-after')
    expect(after, 'after a failure lost its outline').not.toBeNull()
    expect(barsInside(root, marks, after!), 'after a failure is drawn with bars in it').toBe(0)
    // Stacked, not overlapping: the outlined bars sit above the flat bars and
    // below the bare outline.
    const flat = marks.querySelector('.ol-m-ended:not(.is-after-cancel)')!
    const y = (el: Element) => Number(el.getAttribute('y'))
    expect(y(outline!)).toBeLessThan(y(flat))
    expect(y(after!)).toBeLessThan(y(outline!))
  })

  it('draws no outline at all for a bucket whose only cascade followed a cancel', async () => {
    const d = ledgerFixture()
    // 20 Sep: 12 requested, 1 after a failure. Re-read as a cancel's cascade.
    const c = d.buckets[8]!.cancelled as unknown as Record<string, number>
    c.after_cancel = c.after_failure ?? 0
    c.after_failure = 0
    const root = await timeline(d)
    const marks = marksOf(root, 8)
    expect(marks.querySelector('.ol-m-after'), 'a cancel drawn as a failure').toBeNull()
    expect(marks.querySelector('.ol-m-after-cancel')).not.toBeNull()
  })

  it('keys the new mark in the readout with the number it draws, and says it in the column and the Table', async () => {
    const root = await timeline(withCancelCascade(), { view: '' })
    fireEvent.mouseEnter(cols(root)[SEP22]!)
    expect(keyed(root, 'is-after-cancel'), 'the readout has no "after a cancel" key').toBe(String(AFTER_CANCEL))
    // The failure's key still prints only the failure's cascade: 5 + 3 swept.
    expect(keyed(root, 'is-after')).toBe('8')
    const legend = root.querySelector('.ol-legend')!.textContent ?? ''
    expect(legend).toContain('after a cancel')
    expect(cols(root)[SEP22]!.getAttribute('aria-label')).toContain(`8 after a failure, ${AFTER_CANCEL} after a cancel`)
    fireEvent.mouseLeave(root.querySelector('.ol-chart-readout')!)
    // The span's totals, by default.
    expect(keyed(root, 'is-after-cancel')).toBe(String(AFTER_CANCEL))
  })

  it('gives the Table the same three sums lane 3 draws', async () => {
    const d = withCancelCascade()
    const root = await timeline(d, { view: 'table=1' })
    const row = root.querySelectorAll('.ol-table tbody tr')[SEP22]!
    expect(row.textContent).toContain(`${d.buckets[SEP22]!.cancelled!.total} · 297 / 8 / ${AFTER_CANCEL}`)
  })

  it('never draws a bar wider than 28px, however wide the drawing (was `.chart .col`, move 3)', async () => {
    const root = await timeline()
    const bars = [...root.querySelectorAll('.ol-drawing.is-wide .ol-m-ok, .ol-drawing.is-wide .ol-m-fin')]
    expect(bars.length).toBeGreaterThan(0)
    for (const bar of bars) expect(Number(bar.getAttribute('width'))).toBeLessThanOrEqual(28)
  })
})

describe('after a cancel shows its bars at every height it is drawn at (the review of #217)', () => {
  // THE DEFECT: the bars were a pattern anchored to the SVG's origin, a 3px
  // bar every 5px, and lane 3's baseline sits at y ≡ 1 (mod 5) in all three
  // drawings (366, 306, 266). A 3-4px mark on the baseline has its only
  // interior rows at 363-364: the pattern's gap. It drew as a bare outline,
  // pixel for pixel the "after a failure" mark of the same count -- which is
  // what most buckets are, since any count under ~21 of 305 is 3-4px.
  //
  // Buckets 0-4 (12-16 Sep) are rewritten with small counts, the common case,
  // at different offsets above the baseline.
  const SMALL: Array<[number, Partial<Record<string, number>>]> = [
    [0, { after_cancel: 1 }],
    [1, { after_failure: 1 }],
    [2, { requested: 22, after_cancel: 1 }],
    [3, { requested: 1, after_cancel: 1, after_failure: 1 }],
    [4, { requested: 7, after_cancel: 21 }],
  ]

  function small(): Outcomes {
    const d = ledgerFixture()
    for (const [i, split] of SMALL) {
      const c = d.buckets[i]!.cancelled as unknown as Record<string, number>
      for (const key of ['requested', 'after_failure', 'after_cancel', 'workflow_sweep', 'other']) c[key] = split[key] ?? 0
      c.total = Object.values(split).reduce<number>((n, v) => n + (v ?? 0), 0)
    }
    return d
  }

  for (const key of ['wide', 'mid', 'narrow'] as const) {
    it(`fills the outline with the flat bars however few it counts, in the ${key} drawing`, async () => {
      // MUTATION: anchor the bars to the page again, or drop the minimum height.
      const root = await timeline(small())
      const marks = (i: number) => {
        const g = root.querySelector(`.ol-drawing.is-${key} .ol-bucket[data-i="${i}"]`)
        expect(g, `bucket ${i} drew no marks in the ${key} drawing`).not.toBeNull()
        return g!
      }
      for (const [i, split] of SMALL) {
        const g = marks(i)
        const cancel = g.querySelector('.ol-m-after-cancel')
        const failure = g.querySelector('.ol-m-after')
        expect(cancel !== null, `bucket ${i}: an after-a-cancel outline`).toBe((split.after_cancel ?? 0) > 0)
        expect(failure !== null, `bucket ${i}: an after-a-failure outline`).toBe((split.after_failure ?? 0) > 0)
        if (cancel !== null) {
          expect(barsInside(root, g, cancel), `bucket ${i}: no whole flat bar shows inside the after-a-cancel outline`).toBeGreaterThanOrEqual(WHOLE_BAR)
        }
        if (failure !== null) {
          expect(barsInside(root, g, failure), `bucket ${i}: the after-a-failure outline is not hollow`).toBe(0)
        }
      }
    })

    it(`keeps every cancel mark inside lane 3, minimum heights included, in the ${key} drawing`, async () => {
      // A mark's minimum height is added to its count's, so a full column with
      // a small cascade on top rose past the lane into the label above it.
      // MUTATION: stack the minimums without taking them back from the column.
      const root = await timeline(withCancelCascade())
      const base = laneThreeBase(root, key)
      const drawn = LEDGER_DRAWN.find((d) => d.key === key)!
      const laneTop = base - drawn.lanes[2]
      const within = `.ol-drawing.is-${key} .ol-bucket`
      const rects = [
        ...root.querySelectorAll(
          ['.ol-m-ended', '.ol-m-after', '.ol-m-after-cancel', '.ol-flat'].map((c) => `${within} ${c}`).join(', '),
        ),
      ]
      expect(rects.length).toBeGreaterThan(0)
      for (const r of rects) {
        const y = num(r, 'y')
        const where = `${r.getAttribute('class')} in bucket ${r.closest('.ol-bucket')?.getAttribute('data-i')}`
        expect(y, `${where} rises above lane 3`).toBeGreaterThanOrEqual(laneTop - 0.05)
        expect(y + num(r, 'height'), `${where} falls below lane 3's baseline`).toBeLessThanOrEqual(base + 0.05)
      }
    })
  }
})

describe('the sheet draws the new mark in TS-4’s cancel forms', () => {
  it('outlines after a cancel as it outlines after a failure: no fill, --text-dim, 1px', () => {
    // MUTATION: a hue on either outline, or a fill on the after-cancel one.
    const f = fragment('<svg class="ol-svg"><rect class="ol-m-after"/><rect class="ol-m-after-cancel"/></svg>')
    const cancel = f.querySelector('.ol-m-after-cancel')
    expect(cancel, 'no after-cancel outline in the fixture').not.toBeNull()
    expect(won(cancel!, 'fill')).toBe('none')
    expect(won(cancel!, 'stroke')).toBe('var(--text-dim)')
    expect(won(cancel!, 'stroke')).toBe(won(f.querySelector('.ol-m-after')!, 'stroke'))
  })

  it('draws the key by the very rule the flat bars use, with the outline around it', () => {
    const f = fragment('<p class="ol-legend"><i class="ol-k is-ended"></i><i class="ol-k is-after-cancel"></i></p>')
    const bars = won(f.querySelector('.is-ended')!, ['background', 'background-image'])
    expect(bars ?? '').toMatch(/^repeating-linear-gradient\(\s*to bottom/)
    expect(won(f.querySelector('.is-after-cancel')!, ['background', 'background-image'])).toBe(bars)
    expect(won(f.querySelector('.is-after-cancel')!, ['border', 'border-color'])).toContain('var(--text-dim)')
  })
})

// ---------------------------------------------------------------------------
// Decision 7: the row window's stylesheet is gone
// ---------------------------------------------------------------------------

describe('the row-window Timeline’s rules are deleted, not kept for the tests that read them', () => {
  // `(?<![\w-])` so `.ctl-chart-legend` and `.ol-chart` are not the deleted
  // `.chart-legend` and `.chart`.
  const DEAD: Array<[string, RegExp]> = [
    ['.window-bar', /(?<![\w-])\.window-bar(?![\w-])/],
    ['.wb-*', /(?<![\w-])\.wb-[\w-]+/],
    ['.chart', /(?<![\w-])\.chart(?![\w-])/],
    ['.col-label', /(?<![\w-])\.col-label(?![\w-])/],
    ['.stackcol', /(?<![\w-])\.stackcol(?![\w-])/],
    ['.chart-legend', /(?<![\w-])\.chart-legend(?![\w-])/],
    ['.cl-*', /(?<![\w-])\.cl-(?:n|at|all|basis)(?![\w-])/],
  ]
  for (const [name, re] of DEAD) {
    it(`carries no rule for ${name}`, () => {
      // MUTATION: restore any one of them.
      const left = flatRules(STYLES).filter((r) => re.test(r.selector)).map((r) => `${r.selector} :${r.line}`)
      expect(left).toEqual([])
    })
  }
})

// ---------------------------------------------------------------------------
// Decision 8: park reasons are one tenant's, and the card says so
// ---------------------------------------------------------------------------

describe('Not finished yet, in platform scope', () => {
  it('says the park reasons are the caller’s tenant’s beside the platform’s counts', async () => {
    // MUTATION: drop the line, or draw it only when the tenant is known.
    const root = await timeline(ledgerFixture(), { view: 'scope=platform' }, { admin: true })
    const c = card(root, /^Not finished yet/)
    await waitFor(() => expect(c.querySelector('.ol-open-counts')).not.toBeNull())
    await waitFor(() => expect(c.textContent).toContain('CREDENTIAL_MISSING 2'))
    expect(c.querySelector('.ol-open-counts')!.textContent).toContain('14 parked')
    expect(c.querySelector('.ol-reason-scope')?.textContent).toBe('park reasons: tenant eng only')
  })

  it('says so when the tenant has nothing parked, where the platform may', async () => {
    const root = await timeline(ledgerFixture(), { view: 'scope=platform' }, {
      admin: true,
      parked: { tasks: [], next_page_token: null },
    })
    const c = card(root, /^Not finished yet/)
    await waitFor(() => expect(c.querySelector('.ol-reason-scope')).not.toBeNull())
    expect(c.querySelector('.ol-reason-scope')!.textContent).toBe('park reasons: tenant eng only · none parked there')
  })

  it('counts "reasons for k of n" in the tenant the reasons came from, never over the platform', async () => {
    // MUTATION: `n` from platform_tasks_by_state again (it read "3 of 14").
    const root = await timeline(ledgerFixture(), { view: 'scope=platform' }, {
      admin: true,
      parked: { tasks: [parked('p1', 'CREDENTIAL_MISSING'), parked('p2', 'CREDENTIAL_MISSING'), parked('p3', 'MANUAL_PAUSE')], next_page_token: 'more' },
    })
    const c = card(root, /^Not finished yet/)
    await waitFor(() => expect(c.textContent).toContain('reasons for 3 of'))
    expect(c.textContent).toContain('reasons for 3 of 3')
    expect(c.textContent).not.toContain('reasons for 3 of 14')
  })

  it('draws no scope line in tenant scope, where the counts and the reasons are one tenant’s', async () => {
    const root = await timeline()
    const c = card(root, /^Not finished yet/)
    await waitFor(() => expect(c.textContent).toContain('CREDENTIAL_MISSING 2'))
    expect(c.querySelector('.ol-reason-scope')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The review of #196: counts that do not apply are null
// ---------------------------------------------------------------------------

describe('Workflows that failed, when the block does not apply', () => {
  it('draws "standalone tasks only" and no digit from the null counts the route sends', async () => {
    const d = ledgerFixture()
    const w = d.workflows_failed as unknown as Record<string, unknown>
    w.applicable = false
    w.rows = []
    w.failing_steps = []
    w.with_ended_steps = null
    w.with_failed_steps = null
    w.rows_total = null
    const root = await timeline(d)
    const c = card(root, /^Workflows that failed/)
    expect(c.textContent).toContain('standalone tasks only')
    expect(c.textContent).not.toMatch(/\bnull\b|\d+ of \d+/)
  })
})

// ---------------------------------------------------------------------------
// The post-deploy QA of #197 (epic #222, 2026-09-26)
// ---------------------------------------------------------------------------

/**
 * The gap the inspector charts' vertical value axes keep between two labels
 * (`ValueAxis`, `minGapPx={14}` in AttemptPhases and PeakMemory). A --t-micro
 * digit is ~9px tall, so 14px baseline to baseline leaves ~5px between glyphs.
 */
const LABEL_GAP = 14

/** Every scale label in one drawing's gutter, top to bottom. */
function gutterLabels(root: HTMLElement, key: string): Array<{ text: string; y: number }> {
  return [...root.querySelectorAll(`.ol-drawing.is-${key} .ol-gutter text`)]
    .map((t) => ({ text: (t.textContent ?? '').trim(), y: num(t, 'y') }))
    .sort((a, b) => a.y - b.y)
}

/** The fixture with every bucket's succeeded and failed swapped: at most 9 up and 111 down. */
function failuresOutnumber(): Outcomes {
  const d = ledgerFixture()
  for (const b of d.buckets) {
    const s = b.succeeded
    b.succeeded = b.failed
    b.failed = s
  }
  return d
}

describe('the decided lane’s scale labels keep the inspector charts’ gap (epic #222)', () => {
  // QA at 1440 and 390: the decided lane's "0" and its failed-side max "7"
  // printed 4px and 9px into each other. Both sides share one count scale, so
  // the zero sits wherever the split puts it -- near the floor when successes
  // outnumber failures, near the top the other way -- and the gutter drew all
  // three labels whatever the distance. `ValueAxis` has the rule: the value
  // the axis must show first, then the top, then the floor, each dropped
  // rather than printed on one already kept.
  const CASES: Array<[string, () => Outcomes, string]> = [
    ['the dev split, 111 up and 9 down', ledgerFixture, '111'],
    ['failures outnumbering successes, 9 up and 111 down', failuresOutnumber, '111'],
  ]
  for (const key of ['wide', 'mid', 'narrow'] as const) {
    for (const [name, payload, larger] of CASES) {
      it(`keeps every scale label ${LABEL_GAP}px from the next, with ${name}, in the ${key} drawing`, async () => {
        // MUTATION: print the decided lane's three labels unconditionally again.
        const root = await timeline(payload())
        const labels = gutterLabels(root, key)
        expect(labels.length, 'the gutter drew no labels').toBeGreaterThanOrEqual(4)
        for (let i = 1; i < labels.length; i++) {
          const a = labels[i - 1]!
          const b = labels[i]!
          expect(b.y - a.y, `"${a.text}" at ${a.y} and "${b.text}" at ${b.y} print into each other`).toBeGreaterThanOrEqual(LABEL_GAP)
        }
        const texts = labels.map((l) => l.text)
        // The zero both sides hang from is the value the axis must show, and
        // the side that sets the scale keeps its max.
        expect(texts, 'the decided lane lost its zero').toContain('0')
        expect(texts, 'the larger side lost its max').toContain(larger)
      })
    }
  }
})

describe('the provenance foot of a cache hit (epic #222)', () => {
  // QA: "from the 60 s cache · 0 reads this request" beside "28 days built by
  // this read". On a hit the route hands back the ORIGINAL payload with
  // `cached: true` (swarm_api.outcomes `read`), so `coverage.derived_now` --
  // like `generated_at` and `reads` -- belongs to the read the cache kept.
  const at = (d: Outcomes) => new Date(d.generated_at).toLocaleTimeString(undefined, { timeZone: d.tz, hourCycle: 'h23' })

  it('says the days were built by the read the cache kept, at its time, not by this request', async () => {
    // MUTATION: say "built by this read" whatever `cached` says.
    const d = ledgerFixture()
    d.cached = true
    d.coverage.derived_now = 28
    const root = await timeline(d)
    const prov = root.querySelector('.ol-prov')!.textContent ?? ''
    expect(prov).toContain('0 reads this request')
    expect(prov, 'a cache hit claims this request built the days').not.toContain('this read')
    expect(prov).toContain(`28 UTC days built by the ${at(d)} read`)
  })

  it('says this read built them when it did, in the rollup’s own unit', async () => {
    const d = ledgerFixture()
    d.coverage.derived_now = 28
    const root = await timeline(d)
    expect(root.querySelector('.ol-prov')!.textContent).toContain('28 UTC days built by this read')
  })

  it('counts them as tenant-days in platform scope, as it counts the sealed ones', async () => {
    const d = ledgerFixture()
    d.scope = { kind: 'platform', tenants: ['eng', 'personal', 'verify'], excluded: [], tenants_complete: true }
    d.cached = true
    d.coverage.derived_now = 60
    const root = await timeline(d, { view: 'scope=platform' }, { admin: true })
    expect(root.querySelector('.ol-prov')!.textContent).toContain(`60 tenant-days built by the ${at(d)} read`)
  })
})

describe('the requested bars stand on lane 3’s baseline (observed on #217, epic #222)', () => {
  // The flat bars of "requested (and other)" were a pattern anchored to the
  // SVG's origin. Lane 3's baseline is y ≡ 1 (mod 5) in every drawing (366,
  // 306, 266), so a 3px mark -- one requested cancel beside 305 -- painted
  // its bottom row alone, against the baseline rule: a thicker baseline, not
  // a bar. Anchored to the baseline, every requested mark stands on a whole
  // 3px bar, and the columns still line up, since they share the baseline.
  for (const key of ['wide', 'mid', 'narrow'] as const) {
    it(`stands every requested mark on one whole flat bar, in the ${key} drawing`, async () => {
      // MUTATION: anchor the pattern to the SVG's origin again.
      const root = await timeline()
      const base = laneThreeBase(root, key)
      const marks = [...root.querySelectorAll(`.ol-drawing.is-${key} .ol-bucket .ol-m-ended`)]
      // 19, 20, 22, 23, 24 and 25 Sep; 19 and 23 Sep are one requested cancel each (3px).
      expect(marks.length, 'the fixture drew no requested mark').toBe(6)
      for (const m of marks) {
        const i = m.closest('.ol-bucket')?.getAttribute('data-i')
        expect(num(m, 'y') + num(m, 'height'), `bucket ${i}: the requested mark is not on the baseline`).toBeCloseTo(base, 1)
        expect(paintedIn(root, [m], base - WHOLE_BAR, base), `bucket ${i}: the requested mark's bottom ${WHOLE_BAR}px are not one whole bar`).toBe(WHOLE_BAR)
      }
    })
  }
})
