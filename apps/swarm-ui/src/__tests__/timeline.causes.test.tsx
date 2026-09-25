// THE FOLLOW-UP TO #185's LEDGER: WHY A TASK ENDED, SAID AS THE ROUTE NOW KNOWS IT.
//
// The owner's decisions of 2026-09-25 on #185 that reach the page:
//
//   2  A cancel whose parent was CANCELLED is "after a cancel", not "after a
//      failure". The scheduler writes the same words for both, so lane 3 drew
//      them as one outline. The route splits them now, and lane 3 draws the
//      new cause as the two cancel marks TS-4 already has, together: the flat
//      "ended" bars (a cancel) inside the 1px outline (a cascade). The readout
//      and the Table say which is which.
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
    const bars = marks.querySelector('.ol-m-ended.is-after-cancel')
    expect(bars, 'after a cancel is not drawn as a cancel (the flat bars)').not.toBeNull()
    expect(bars!.getAttribute('fill') ?? '').toMatch(/^url\(#.*-flat\)$/)
    expect(Number(bars!.getAttribute('height'))).toBeGreaterThanOrEqual(3)
    // The failure's cascade is still the bare outline, and still its own mark.
    expect(marks.querySelector('.ol-m-after'), 'after a failure lost its outline').not.toBeNull()
    // Stacked, not overlapping: the outlined bars sit above the flat bars and
    // below the bare outline.
    const flat = marks.querySelector('.ol-m-ended:not(.is-after-cancel)')!
    const y = (el: Element) => Number(el.getAttribute('y'))
    expect(y(bars!)).toBeLessThan(y(flat))
    expect(y(marks.querySelector('.ol-m-after')!)).toBeLessThan(y(bars!))
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
