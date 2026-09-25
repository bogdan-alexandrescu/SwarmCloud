// THE TIMELINE AS AN OUTCOME LEDGER (#185), AS BEHAVIOUR -- and the tenants table.
//
// The page used to read the newest 200-2,000 tasks and label whatever span
// they covered; every case that pinned that window (TS-8's "All N tasks",
// TS-10's People table, TS-11/12's metric strip and Token spend, TS-22's
// two-basis legend) described a design the owner retired on 2026-09-25 and is
// replaced here by the decision that retired it:
//
//   * a real span, 14d by default, applied by the server and written to the
//     hash, with every bucket drawn and measured zeroes before the first task;
//   * the success rate -- succeeded over succeeded + failed + dead-lettered,
//     cancels excluded -- as the page's one figure, with k of n, its Wilson
//     interval and what it leaves out;
//   * four lanes on one axis (rate, decided, cancelled on its own scale,
//     throughput as the one submission-time series), a readout that is the
//     legend, and a Table twin;
//   * eight cards, "Token spend" renamed "Reported cost · not a bill", and the
//     Rows control gone.
//
// The api module is replaced with the CONTRACT's payload (`ledgerFixture`,
// built in the exact response shape of GET /v1/outcomes), because the route
// is built in a parallel lane: these tests are what hold the UI to the
// contract rather than to whatever the route happens to return first.
//
// Every case below was committed RED against the row-window Timeline before
// the ledger landed; the pull request names the run.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import { HELP } from '../help'
import { ledgerFixture } from '../outcomes.fixture'
import type { Outcomes, OutcomeBucket } from '../outcomes'
import type { Me, Stats, Task, TaskPage, Tenant } from '../types'

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

const { ActivityScreen, TenantsScreen } = await import('../Activity')

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

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

const STATS: Stats = {
  tenant_id: 'eng',
  dispatch_paused: false,
  tasks_by_state: {
    SUBMITTED: 0, QUEUED: 2, PARKED: 3, READY: 1, LEASED: 1, DISPATCHED: 0,
    STARTING: 0, RUNNING: 2, SUCCEEDED: 272, FAILED: 28, CANCELLED: 416, DEAD_LETTERED: 0,
  },
  limits: {},
  generated_at: '2026-09-25T11:10:02Z',
}

function parked(id: string, reason: string): Task {
  return { id, state: 'PARKED', park_reason: reason } as unknown as Task
}

function serve(payload: Outcomes | Result<Outcomes>, opts: { admin?: boolean } = {}): void {
  const r: Result<Outcomes> = 'status' in payload ? payload : ok(payload)
  api.loadOutcomes.mockResolvedValue(r)
  api.loadMe.mockResolvedValue(ok(me(opts.admin === true)))
  api.loadRunnerProfiles.mockResolvedValue(ok(['browser', 'claude-code', 'codex', 'generic', 'mock']))
  api.loadStats.mockResolvedValue(ok(STATS))
  api.loadTasksInState.mockResolvedValue(
    ok<TaskPage>({ tasks: [parked('p1', 'CREDENTIAL_MISSING'), parked('p2', 'CREDENTIAL_MISSING'), parked('p3', 'PROVIDER_QUOTA_EXHAUSTED')], next_page_token: null }),
  )
  api.loadTenants.mockResolvedValue(
    ok({ tenants: ['eng', 'personal', 'verify'].map((tenant_id) => ({ tenant_id }) as Tenant) }),
  )
}

async function timeline(
  payload: Outcomes | Result<Outcomes> = ledgerFixture(),
  props: { view?: string | null; onView?: (q: string) => void } = {},
  opts: { admin?: boolean } = {},
): Promise<HTMLElement> {
  serve(payload, opts)
  const { container } = render(<ActivityScreen {...props} />)
  await waitFor(() => expect(container.querySelector('.ol-ledger, .ctl-empty')).not.toBeNull())
  return container as HTMLElement
}

/** The query the ledger was last read with. */
function lastQuery(): URLSearchParams {
  const calls = api.loadOutcomes.mock.calls
  expect(calls.length, 'the ledger was never read').toBeGreaterThan(0)
  return calls[calls.length - 1]![0] as URLSearchParams
}

/** One drawing's columns: tests read the wide one, the way chart.narrow.test.tsx does. */
function cols(root: HTMLElement, drawing: 'wide' | 'mid' | 'narrow' = 'wide'): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(`.ol-drawing.is-${drawing} .ol-col`)]
}

function drawing(root: HTMLElement, key: 'wide' | 'mid' | 'narrow' = 'wide'): Element {
  const d = root.querySelector(`.ol-drawing.is-${key}`)
  expect(d, `no ${key} drawing`).not.toBeNull()
  return d!
}

/** A bucket's marks in one drawing, by index. */
function bucketMarks(root: HTMLElement, i: number, key: 'wide' | 'mid' | 'narrow' = 'wide'): Element | null {
  return drawing(root, key).querySelector(`.ol-bucket[data-i="${i}"]`)
}

/** The readout's figures, in order: rate, succeeded, failed, dead-lettered, cancelled, after a failure, submitted, finished. */
function readout(root: HTMLElement): string[] {
  return [...root.querySelectorAll('.ol-legend .ol-n')].map((n) => (n.textContent ?? '').trim())
}

function card(root: HTMLElement, title: RegExp): HTMLElement {
  const h = [...root.querySelectorAll('.ol-card .ctl-card-title')].find((el) => title.test(el.textContent ?? ''))
  expect(h, `no card titled ${title}`).toBeTruthy()
  return h!.closest('.ol-card') as HTMLElement
}

const DAY = (iso: string) => new Date(iso).toLocaleString(undefined, { timeZone: 'Europe/Bucharest', day: 'numeric', month: 'short' })

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset()
  try {
    window.localStorage.clear()
  } catch {
    // no storage in this environment; the page must not need it
  }
})

afterEach(() => {
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------
// The read: a real span, applied by the server, written to the hash
// ---------------------------------------------------------------------------

describe('the read', () => {
  it('asks for 14 days by default, in the viewer’s zone, with the previous span for the delta', async () => {
    await timeline()
    const q = lastQuery()
    expect(q.get('span'), 'the owner’s default span is 14d').toBe('14d')
    expect(q.get('tz'), 'tz is required by the contract').toBe(Intl.DateTimeFormat().resolvedOptions().timeZone)
    expect(q.get('compare')).toBe('previous')
    expect(q.get('bucket')).toBe('auto')
    // Tenant scope sends no tenant: the route takes it from the caller (invariant 9).
    expect(q.has('scope')).toBe(false)
    expect(q.has('tenant')).toBe(false)
    expect(q.has('exclude_tenant')).toBe(false)
  })

  it('reads the span the hash names, and writes a new span to the hash', async () => {
    const onView = vi.fn()
    const root = await timeline(ledgerFixture(), { view: 'span=30d', onView })
    expect(lastQuery().get('span')).toBe('30d')
    const seg = within(root.querySelector<HTMLElement>('.ol-span')!)
    expect(seg.getByRole('button', { name: '30d' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(seg.getByRole('button', { name: '7d' }))
    expect(onView).toHaveBeenLastCalledWith('span=7d')
  })

  it('offers 24h, 7d, 14d, 30d, 90d and a from–to range, and no Rows control', async () => {
    const root = await timeline()
    const seg = root.querySelector<HTMLElement>('.ol-span')!
    expect([...seg.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['24h', '7d', '14d', '30d', '90d', 'from–to'])
    expect(within(seg).getByRole('button', { name: '14d' }).getAttribute('aria-pressed')).toBe('true')
    // THE ROWS CONTROL AND ITS RULE ARE RETIRED (owner decision on #185).
    const labels = [...root.querySelectorAll('label')].map((l) => (l.textContent ?? '').trim())
    expect(labels.some((l) => l.startsWith('Rows')), 'the Rows control is still drawn').toBe(false)
    expect(root.textContent, 'the window bar still claims a row window').not.toMatch(/Last \d+ tasks|All \d+ tasks|older tasks exist/)
  })

  it('sends a from–to range as since and an exclusive until', async () => {
    const onView = vi.fn()
    const root = await timeline(ledgerFixture(), { view: '', onView })
    fireEvent.click(within(root.querySelector<HTMLElement>('.ol-span')!).getByRole('button', { name: 'from–to' }))
    const range = root.querySelector<HTMLElement>('.ol-range')!
    const [from, to] = [...range.querySelectorAll('input')]
    fireEvent.change(from!, { target: { value: '2026-09-01' } })
    fireEvent.change(to!, { target: { value: '2026-09-10' } })
    fireEvent.click(within(range).getByRole('button', { name: 'apply' }))
    // "to 10 Sep" includes the 10th, so the route's exclusive until is the 11th.
    expect(onView).toHaveBeenLastCalledWith('since=2026-09-01&until=2026-09-11')
  })

  it('shows the server’s bucket choice, and offers month only at 60 days or more', async () => {
    const root = await timeline()
    const select = [...root.querySelectorAll('label')].find((l) => l.textContent?.startsWith('Group by'))!.querySelector('select')!
    expect(select.querySelector('option[value="auto"]')!.textContent).toBe('auto · day')
    const month = select.querySelector<HTMLOptionElement>('option[value="month"]')!
    expect(month.disabled, 'month over 14 days draws one lonely column').toBe(true)
    expect(month.textContent).toContain('span under 60 days')
  })

  it('remembers the last view in this browser, and opens a bare #work/timeline on it', async () => {
    window.localStorage.setItem('swarm.timeline.view', 'span=90d')
    const onView = vi.fn()
    await timeline(ledgerFixture(), { view: null, onView })
    expect(lastQuery().get('span')).toBe('90d')
    expect(onView, 'the remembered view is not written back to the address').toHaveBeenCalledWith('span=90d')
  })
})

// ---------------------------------------------------------------------------
// The headline: one figure, with its denominator and what it excludes
// ---------------------------------------------------------------------------

describe('the headline', () => {
  it('reads 90.7 % with 272 of 300 decided, its interval, and the cancels it excludes', async () => {
    const root = await timeline()
    const fig = root.querySelector<HTMLElement>('.ol-headline .ol-figure')!
    expect(fig.textContent).toBe('90.7 %')
    expect(fig.classList.contains('ctl-figure'), 'the rate is not at the figure step').toBe(true)
    expect(root.querySelector('.ol-kofn')!.textContent).toBe('272 of 300 decided')
    expect(fig.getAttribute('aria-label')).toContain('95 % interval 86.8–93.5 %')
    expect(root.querySelector('.ol-head .ctl-card-note')!.textContent).toBe('excludes 416 cancelled')
    // The one `?` on the screen follows the label it explains (AH-24).
    expect(root.querySelector('.ol-head button[aria-label^="Help: "]')).not.toBeNull()
  })

  it('drops the delta when the previous span decided nothing, and says why', async () => {
    const root = await timeline()
    expect(root.querySelector('.ol-delta')!.textContent).toBe('prev 14d: no finished work, so no delta')
  })

  it('drops the delta when the previous span is partial', async () => {
    const d = ledgerFixture()
    d.previous = { ...d.previous!, complete: false, succeeded: 200, failed: 20, rate: { k: 200, n: 220, p: 0.9091, lo: 0.86, hi: 0.94 } }
    const root = await timeline(d)
    expect(root.querySelector('.ol-delta')!.textContent).toBe('prev 14d partial, no delta')
  })

  it('prints the delta in points when both spans are whole', async () => {
    const d = ledgerFixture()
    d.previous = { ...d.previous!, complete: true, succeeded: 150, failed: 20, rate: { k: 150, n: 170, p: 0.8824, lo: 0.82, hi: 0.92 } }
    const root = await timeline(d)
    expect(root.querySelector('.ol-delta')!.textContent).toBe('prev 14d 88.2 % · +2.4 pts')
  })

  it('says "no finished work", never 0 %, when nothing was decided', async () => {
    const d = ledgerFixture()
    d.totals = { ...d.totals, succeeded: 0, failed: 0, rate: null }
    const root = await timeline(d)
    const fig = root.querySelector('.ol-headline .ol-figure')!
    expect(fig.textContent).toBe('no finished work')
    expect(fig.textContent).not.toMatch(/\d/)
    expect(root.querySelector('.ol-headline')!.textContent).not.toMatch(/0 %/)
  })

  it('marks every total partial, with k of n buckets, when a bucket was not read', async () => {
    const d = ledgerFixture()
    d.totals = { ...d.totals, complete: false, buckets_read: 13 }
    const root = await timeline(d)
    const partial = root.querySelector('.ol-headline .ol-partial')!
    expect(partial.querySelector('.ctl-mark.is-partial')).not.toBeNull()
    expect(partial.textContent).toContain('13 of 14 days')
  })
})

// ---------------------------------------------------------------------------
// The lanes
// ---------------------------------------------------------------------------

describe('the ledger', () => {
  it('draws every bucket from since to until, the empty days before the first task as measured zeroes', async () => {
    const root = await timeline()
    const c = cols(root)
    expect(c, 'one column per bucket, none omitted').toHaveLength(14)
    // 12-15 Sep: nothing ended. Each lane draws the real-zero tick, not a gap.
    for (const i of [0, 1, 2, 3]) {
      const marks = bucketMarks(root, i)!
      expect(marks.querySelectorAll('.ol-zero').length, `day ${i} draws no real-zero tick`).toBe(3)
      expect(marks.querySelector('.ol-m-ok, .ol-m-bad, .ol-m-fin')).toBeNull()
    }
    // …and no rate point: a rate over nothing is a gap, never 0 %.
    const points = [...drawing(root).querySelectorAll('.ol-m-pt')].map((p) => Number(p.getAttribute('data-i')))
    expect(points).not.toContain(0)
    expect(points).not.toContain(3)
    expect(points[0]).toBe(4)
  })

  it('names each column with its full day and every count, and draws no value only in a hover', async () => {
    const root = await timeline()
    const c = cols(root)
    const twentySecond = c[10]!
    expect(twentySecond.getAttribute('role')).toBe('img')
    expect(twentySecond.hasAttribute('title')).toBe(false)
    const name = twentySecond.getAttribute('aria-label') ?? ''
    expect(name.startsWith(DAY('2026-09-22T00:00:00+03:00'))).toBe(true)
    expect(name).toContain('25 succeeded, 8 failed')
    expect(name).toContain('305 cancelled, 8 after a failure')
    expect(name).toContain('338 submitted')
  })

  it('keeps 22 Sep’s 8 failures visible beside its 305 cancels: failures hang from the decided lane, cancels have their own scale', async () => {
    const root = await timeline()
    const marks = bucketMarks(root, 10)!
    const bad = marks.querySelector('.ol-m-bad')!
    expect(Number(bad.getAttribute('height')), 'the failed column is a sliver').toBeGreaterThanOrEqual(4)
    // The cut is TS-4's failed form: solid `--bad` with the 2px ground cut.
    expect(marks.querySelector('.ol-m-cut')).not.toBeNull()
    // Cancels are in lane 3, with its own max printed.
    expect(marks.querySelector('.ol-m-ended')).not.toBeNull()
    expect(marks.querySelector('.ol-m-after'), 'the cancels a failure caused have no outline mark').not.toBeNull()
    const labels = [...drawing(root).querySelectorAll('.ol-lane-label')].map((t) => t.textContent ?? '')
    expect(labels[2]).toBe('Cancelled · own scale · max 305')
  })

  it('draws succeeded in TS-4’s solid form up, and failed down from the same zero', async () => {
    const root = await timeline()
    const marks = bucketMarks(root, 13)!
    const okBar = marks.querySelector('.ol-m-ok')!
    const bad = marks.querySelector('.ol-m-bad')!
    const top = Number(okBar.getAttribute('y')) + Number(okBar.getAttribute('height'))
    expect(Number(bad.getAttribute('y')), 'succeeded and failed do not share a baseline').toBeCloseTo(top, 0)
  })

  it('draws a point hollow under five decided, filled at five or more, and the current bucket hollow at a dash', async () => {
    const root = await timeline()
    const pt = (i: number) => drawing(root).querySelector(`.ol-m-pt[data-i="${i}"]`)!
    expect(pt(4).classList.contains('is-hollow'), '16 Sep decided 1: its point reads as a measurement').toBe(true)
    expect(pt(6).classList.contains('is-hollow'), '18 Sep decided 18').toBe(false)
    // 25 Sep is in progress: hollow, reached by a dashed segment.
    expect(pt(13).classList.contains('is-hollow')).toBe(true)
    expect(drawing(root).querySelector('.ol-m-rate.is-so-far')).not.toBeNull()
    expect(bucketMarks(root, 13)!.querySelector('.ol-so-far'), 'the current bucket has no dashed edge').not.toBeNull()
  })

  it('draws the throughput lane as submitted against finished, labelled as the only submission-time lane', async () => {
    const root = await timeline()
    const labels = [...drawing(root).querySelectorAll('.ol-lane-label')].map((t) => t.textContent ?? '')
    expect(labels[3]).toMatch(/submitted \(by created_at, the only lane on submission time\) vs finished · max 338/)
    expect(drawing(root).querySelector('.ol-lane.is-flow .ol-m-sub'), 'no submitted line').not.toBeNull()
    expect(bucketMarks(root, 10)!.querySelector('.ol-m-fin'), 'no finished column on 22 Sep').not.toBeNull()
    // The narrow drawing carries a short label that still names its basis.
    const narrow = [...drawing(root, 'narrow').querySelectorAll('.ol-lane-label')].map((t) => t.textContent ?? '')
    expect(narrow[3]).toContain('by created_at')
  })

  it('draws a bucket that was not read as one hatched band over every lane, with no digit and no marks', async () => {
    const d = ledgerFixture()
    const b = d.buckets[8]!
    d.buckets[8] = {
      ...b, state: 'unread', unread_reason: 'derive_budget', submitted: null, ended: null, succeeded: null,
      failed: null, dead_lettered: null, cancelled: null, rate: null, failure_classes: null, cost: null,
    } satisfies OutcomeBucket
    d.totals = { ...d.totals, complete: false, buckets_read: 13 }
    const root = await timeline(d)
    const band = drawing(root).querySelector('.ol-unread[data-i="8"]')!
    expect(band, 'the unread bucket is not hatched').not.toBeNull()
    expect(band.getAttribute('fill')).toMatch(/^url\(#/)
    expect(bucketMarks(root, 8), 'an unread bucket drew marks').toBeNull()
    const name = cols(root)[8]!.getAttribute('aria-label') ?? ''
    expect(name).toContain('not read')
    expect(name.replace(DAY(b.start), '')).not.toMatch(/\d/)
    // The rate line breaks around it.
    expect(drawing(root).querySelectorAll('.ol-lane.is-rate [data-run]').length).toBeGreaterThanOrEqual(2)
  })

  it('draws three drawings at their own widths, the narrow one never under its 26px column floor', async () => {
    const root = await timeline()
    const widths = ['wide', 'mid', 'narrow'].map((k) => Number(drawing(root, k as 'wide').getAttribute('data-drawn')))
    expect(widths).toEqual([1080, 640, 300])
    // 14 day buckets at the phone's 26px floor do not fit in 300: it grows and scrolls instead of shrinking.
    const narrowSvg = drawing(root, 'narrow').querySelector('svg')!
    expect(Number(narrowSvg.getAttribute('width'))).toBeGreaterThanOrEqual(38 + 14 * 26 + 8)
    const pitch = parseFloat(cols(root, 'narrow')[1]!.style.left) - parseFloat(cols(root, 'narrow')[0]!.style.left)
    expect(pitch).toBeGreaterThanOrEqual(26)
    // Every drawing is presentational; the columns carry the names.
    for (const k of ['wide', 'mid', 'narrow'] as const) {
      expect(drawing(root, k).querySelector('svg')!.getAttribute('aria-hidden')).toBe('true')
      expect(drawing(root, k).querySelector('.ol-cols')!.getAttribute('role')).toBe('group')
    }
  })

  it('labels a phone’s hourly axis every third hour on the clock and every day, and nothing between', async () => {
    const d = ledgerFixture()
    const start = Date.parse('2026-09-24T10:00:00+03:00')
    d.bucket = 'hour'
    d.buckets = Array.from({ length: 30 }, (_, i) => {
      const s = new Date(start + i * 3_600_000).toISOString()
      const e = new Date(start + (i + 1) * 3_600_000).toISOString()
      return { ...d.buckets[13]!, start: s, end: e, in_progress: false, state: 'sealed' as const }
    })
    const root = await timeline(d)
    const labels = [...drawing(root, 'narrow').querySelectorAll('.ol-axis-label')]
    expect(labels.length).toBeGreaterThanOrEqual(8)
    for (const l of labels) {
      const i = Math.round((Number(l.getAttribute('x')) - 38 - 13) / 26)
      const at = new Date(start + i * 3_600_000)
      const hour = Number(at.toLocaleString('en-GB', { timeZone: 'Europe/Bucharest', hour: '2-digit', hourCycle: 'h23' }))
      const isDate = l.textContent === DAY(at.toISOString())
      expect(isDate || hour % 3 === 0, `column ${i} (${hour}:00) is labelled "${l.textContent}" on a phone`).toBe(true)
    }
  })
})

// ---------------------------------------------------------------------------
// The readout is the legend (TS-9)
// ---------------------------------------------------------------------------

describe('the readout', () => {
  it('reads out the span’s totals by default, with every cancel cause and the submitted count', async () => {
    const root = await timeline()
    expect(readout(root)).toEqual(['90.7 %', '272', '28', '0', '416', '12', '730', '716'])
    const legend = root.querySelector<HTMLElement>('.ol-legend')!
    expect(legend.textContent).toContain('272 of 300 · 95 % 86.8–93.5 %')
    expect(legend.textContent).toContain('requested 401 · other 0')
    expect(legend.textContent).toContain('workflow sweep 3')
    expect(legend.textContent).toContain('by created_at')
    // Not a live region: the focused column's name already says the counts.
    expect(legend.getAttribute('aria-live')).toBeNull()
    expect(root.querySelector('.ol-at')!.textContent).toBe('all 14 days')
  })

  it('swaps to one bucket on hover, tap or focus, and back on "all", Escape or leaving', async () => {
    const root = await timeline()
    const c = cols(root)
    fireEvent.mouseEnter(c[10]!)
    expect(readout(root)).toEqual(['75.8 %', '25', '8', '0', '305', '5', '338', '338'])
    expect(root.querySelector('.ol-at')!.textContent).toContain(DAY('2026-09-22T00:00:00+03:00'))
    expect(c[10]!.classList.contains('is-picked')).toBe(true)
    fireEvent.click(within(root.querySelector<HTMLElement>('.ol-actions')!).getByRole('button', { name: 'all' }))
    expect(readout(root)[1]).toBe('272')

    fireEvent.click(c[6]!)
    expect(readout(root)[1]).toBe('18')
    fireEvent.keyDown(c[6]!, { key: 'Escape' })
    expect(readout(root)[1]).toBe('272')

    act(() => c[4]!.focus())
    expect(readout(root)[1]).toBe('1')
    fireEvent.mouseLeave(root.querySelector('.ol-chart-readout')!)
    expect(readout(root)[1]).toBe('272')
  })

  it('is one tab stop, starting on the newest bucket, moved by the arrows, Home and End', async () => {
    const root = await timeline()
    const c = cols(root)
    const stops = () => c.filter((x) => x.getAttribute('tabindex') === '0')
    expect(stops()).toEqual([c[13]])
    act(() => c[13]!.focus())
    fireEvent.keyDown(c[13]!, { key: 'ArrowLeft' })
    expect(document.activeElement).toBe(c[12])
    fireEvent.keyDown(c[12]!, { key: 'Home' })
    expect(document.activeElement).toBe(c[0])
    fireEvent.keyDown(c[0]!, { key: 'End' })
    expect(document.activeElement).toBe(c[13])
  })

  it('says "so far" on the current bucket and "settling" on one that has only just ended', async () => {
    const d = ledgerFixture()
    d.buckets[12] = { ...d.buckets[12]!, state: 'open', in_progress: false }
    const root = await timeline(d)
    fireEvent.mouseEnter(cols(root)[13]!)
    expect(root.querySelector('.ol-at')!.textContent).toContain('so far')
    fireEvent.mouseEnter(cols(root)[12]!)
    expect(root.querySelector('.ol-legend')!.textContent).toContain('settling, final 15 min after the bucket')
  })

  it('reads out an unread bucket as not read, with no count at all', async () => {
    const d = ledgerFixture()
    d.buckets[8] = {
      ...d.buckets[8]!, state: 'unread', unread_reason: 'read_failed', submitted: null, ended: null, succeeded: null,
      failed: null, dead_lettered: null, cancelled: null, rate: null, failure_classes: null, cost: null,
    }
    const root = await timeline(d)
    fireEvent.mouseEnter(cols(root)[8]!)
    const legend = root.querySelector('.ol-legend')!
    expect(legend.querySelector('.ctl-mark.is-unread')).not.toBeNull()
    expect(legend.querySelectorAll('.ol-n')).toHaveLength(0)
    expect(legend.textContent).not.toMatch(/\d/)
  })

  it('zooms the page to a picked day, and a chip goes back to the span it came from', async () => {
    const onView = vi.fn()
    const root = await timeline(ledgerFixture(), { view: '', onView })
    fireEvent.mouseEnter(cols(root)[10]!)
    const zoom = within(root.querySelector<HTMLElement>('.ol-actions')!).getByRole('button', { name: /^zoom to / })
    fireEvent.click(zoom)
    const q = new URLSearchParams(onView.mock.calls[onView.mock.calls.length - 1]![0] as string)
    expect(q.get('since')).toBe('2026-09-22T00:00:00+03:00')
    expect(q.get('until')).toBe('2026-09-23T00:00:00+03:00')
    expect(q.has('back')).toBe(true)
  })

  it('links a day’s failures to the Agents list, saying it is not limited to that day', async () => {
    const root = await timeline()
    fireEvent.mouseEnter(cols(root)[10]!)
    const link = root.querySelector<HTMLAnchorElement>('.ol-drill')!
    expect(link.getAttribute('href')).toBe('#work/running/recent/failed')
    expect(link.textContent).toContain('8 failed that day')
    expect(link.textContent).toContain(`not limited to ${DAY('2026-09-22T00:00:00+03:00')}`)
  })
})

// ---------------------------------------------------------------------------
// The Table twin
// ---------------------------------------------------------------------------

describe('the Table toggle', () => {
  it('swaps the drawing for the same buckets as a scrolling table, and writes it to the hash', async () => {
    const onView = vi.fn()
    const root = await timeline(ledgerFixture(), { view: 'table=1', onView })
    expect(root.querySelector('.ol-drawing'), 'the drawing is still shown under Table').toBeNull()
    const table = root.querySelector('.ol-table')!
    expect(table.classList.contains('ctl-table') && table.classList.contains('is-scroll')).toBe(true)
    expect(table.querySelectorAll('tbody tr')).toHaveLength(14)
    const heads = [...table.querySelectorAll('thead th')].map((th) => th.textContent)
    expect(heads).toContain('Submitted')
    expect(heads.some((h) => /Rate · k of n · 95 %/.test(h ?? ''))).toBe(true)
    const toggle = root.querySelector<HTMLButtonElement>('.ol-table-toggle')!
    expect(toggle.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(toggle)
    expect(onView).toHaveBeenLastCalledWith('')
  })

  it('says "nothing decided" rather than 0 % for a day with nothing decided', async () => {
    const root = await timeline(ledgerFixture(), { view: 'table=1' })
    const first = root.querySelector('.ol-table tbody tr')!
    expect(first.textContent).toContain('nothing decided')
    expect(first.textContent).not.toMatch(/0\.0 %/)
  })
})

// ---------------------------------------------------------------------------
// The toolbar's scope, tenant and profile filters
// ---------------------------------------------------------------------------

describe('scope and filters', () => {
  it('gives a non-admin no scope control, and names their tenant in the facts line', async () => {
    const root = await timeline()
    await waitFor(() => expect(api.loadMe).toHaveBeenCalled())
    expect(root.querySelector('.ol-scope')).toBeNull()
    const facts = [...root.querySelectorAll('.ol-facts .ctl-fact')].map((f) => f.textContent)
    expect(facts).toContain('tenanteng')
    expect(facts).toContain('bycompleted_at')
  })

  it('gives an admin the scope, and excludes the verify tenant in one click', async () => {
    const onView = vi.fn()
    const root = await timeline(ledgerFixture(), { view: 'scope=platform', onView }, { admin: true })
    await waitFor(() => expect(root.querySelector('.ol-scope')).not.toBeNull())
    expect(lastQuery().get('scope')).toBe('platform')
    const verify = await waitFor(() => {
      const b = root.querySelector<HTMLButtonElement>('.ol-verify')
      expect(b).not.toBeNull()
      return b!
    })
    expect(verify.textContent).toBe('exclude verify')
    fireEvent.click(verify)
    // An exclusion, not an include list: a tenant created tomorrow is still counted.
    expect(onView).toHaveBeenLastCalledWith('exclude_tenant=verify')
  })

  it('offers every runner profile the catalogue serves, never a hardcoded list', async () => {
    const onView = vi.fn()
    const root = await timeline(ledgerFixture(), { view: '', onView })
    await waitFor(() => expect(api.loadRunnerProfiles).toHaveBeenCalled())
    const pick = [...root.querySelectorAll<HTMLElement>('.ol-pick')].find((p) => p.textContent?.startsWith('Profile'))!
    await waitFor(() => expect(pick.querySelectorAll('input[type="checkbox"]')).toHaveLength(5))
    // By its label, not by role: the list sits in a closed <details>, which a
    // role query may treat as hidden depending on the DOM's UA sheet.
    const box = [...pick.querySelectorAll('label')].find((l) => l.textContent?.trim() === 'claude-code')!.querySelector('input')!
    fireEvent.click(box)
    expect(onView).toHaveBeenLastCalledWith('profile=claude-code')
  })

  it('renders a 403 on platform scope as the admin state, never as a failure', async () => {
    const root = await timeline({
      status: 'error',
      error: { kind: 'admin_required', httpStatus: 403, code: 'forbidden', message: 'admin group membership is required for this operation' },
    }, { view: 'scope=platform' })
    const empty = root.querySelector('.ctl-empty')!
    expect(empty.classList.contains('is-admin')).toBe(true)
    expect(empty.classList.contains('is-failed')).toBe(false)
    expect(root.querySelector('.ol-ledger')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The states the page must never collapse
// ---------------------------------------------------------------------------

describe('reading, failing and re-reading', () => {
  it('holds the chart’s geometry with the moving pending sweep while the first read is in flight', async () => {
    serve(ledgerFixture())
    api.loadOutcomes.mockReturnValue(new Promise(() => {}))
    const { container } = render(<ActivityScreen />)
    expect(container.querySelector('.ol-pending.ctl-pending')).not.toBeNull()
    expect(container.querySelector('.ol-figure')).toBeNull()
  })

  it('draws no number anywhere when the read fails', async () => {
    const root = await timeline({
      status: 'error',
      error: { kind: 'server_error', httpStatus: 500, code: 'internal', message: 'the aggregate could not be read' },
    })
    const empty = root.querySelector('.ctl-empty.is-failed')
    expect(empty, 'a failed read is not the failed empty state').not.toBeNull()
    expect(root.querySelector('.ol-ledger, .ol-cards')).toBeNull()
    expect(root.querySelector('.ol-body')).toBeNull()
  })

  it('dims the last drawing rather than blanking it while a new filter is read', async () => {
    const root = await timeline(ledgerFixture(), { view: '' })
    api.loadOutcomes.mockReturnValue(new Promise(() => {}))
    fireEvent.click(within(root.querySelector<HTMLElement>('.ol-span')!).getByRole('button', { name: '30d' }))
    await waitFor(() => expect(root.querySelector('.ol-body.ctl-stale-body')).not.toBeNull())
    expect(root.querySelector('.ol-figure')!.textContent).toBe('90.7 %')
  })

  it('carries its read cost and age in a provenance foot, honestly on a cache hit', async () => {
    const d = ledgerFixture()
    const root = await timeline(d)
    expect(root.querySelector('.ol-prov')!.textContent).toContain('34 reads')
    expect(root.querySelector('.ol-prov')!.textContent).toContain('cached 60 s')
    const hit = await timeline({ ...ledgerFixture(), cached: true })
    const provs = [...hit.querySelectorAll('.ol-prov')]
    expect(provs[provs.length - 1]!.textContent).toContain('0 reads this request')
  })
})

// ---------------------------------------------------------------------------
// The eight cards
// ---------------------------------------------------------------------------

describe('the eight cards', () => {
  it('draws the eight cards the owner decided, and no Token spend', async () => {
    const root = await timeline()
    const titles = [...root.querySelectorAll('.ol-card .ctl-card-title')].map((t) => t.firstChild?.textContent)
    expect(titles).toEqual([
      'Why tasks failed',
      'Retries and attempts',
      'Time to result, by profile',
      'Reliability by runner profile',
      'Workflows that failed, and where',
      'Reported cost',
      'Not finished yet',
      'Why tasks were cancelled',
    ])
    expect(root.textContent, '"Token spend" survived the rename').not.toMatch(/Token spend/)
  })

  it('Why tasks failed: every class in the server’s fixed order, zeroes drawn as real zeroes', async () => {
    const root = await timeline()
    const c = card(root, /^Why tasks failed/)
    const rows = [...c.querySelectorAll('.ol-row')]
    expect(rows.map((r) => r.getAttribute('data-row'))).toEqual([
      'runner error', 'timeout', 'lost worker', 'could not start', 'outputs missing', 'dispatch failed', 'other', 'no reason recorded',
    ])
    expect(rows.map((r) => r.querySelector('.ol-row-n')!.textContent)).toEqual(['1', '0', '7', '0', '6', '8', '6', '0'])
    const timeout = rows[1]!
    expect(timeout.querySelector('.ctl-util-track.is-zero'), 'a zero class is an empty bar, not a real zero').not.toBeNull()
    expect(c.querySelector('.ctl-card-note')!.textContent).toBe('28 · by class')
  })

  it('Workflows that failed: the not-read mark in Steps keeps its row, and a cut list says so', async () => {
    const d = ledgerFixture()
    d.workflows_failed = { ...d.workflows_failed, rows_total: 27 }
    const root = await timeline(d)
    const c = card(root, /^Workflows that failed/)
    expect(c.querySelector('caption')!.textContent).toBe('showing 3 of 27')
    const rows = [...c.querySelectorAll('tbody tr')]
    expect(rows).toHaveLength(3)
    expect(rows[2]!.querySelector('.ctl-mark.is-unread'), 'an unreadable workflow lost its mark').not.toBeNull()
    expect(c.querySelector('.ctl-card-foot')!.textContent).toContain('most-failing steps: synthesis 4 · review 2 · build 1')
  })

  it('Workflows that failed: says "standalone tasks only" when the kind filter leaves no step', async () => {
    const d = ledgerFixture()
    d.workflows_failed = { ...d.workflows_failed, applicable: false, rows: [], rows_total: 0 }
    const root = await timeline(d)
    expect(card(root, /^Workflows that failed/).textContent).toContain('standalone tasks only')
  })

  it('Retries and attempts: admissions, not runs, and a null exit is its own row', async () => {
    const root = await timeline()
    const c = card(root, /^Retries and attempts/)
    expect(c.querySelector('.ctl-card-note')!.textContent).toBe('admissions, not runs')
    expect(c.textContent).toContain('needed a retry · 18 of 318 that ran (6 %)')
    expect(c.textContent).toContain('no exit recorded 9')
  })

  it('Time to result: no all-profiles row, a dash for nothing decided, max under 20 and the values under 5', async () => {
    const root = await timeline()
    const c = card(root, /^Time to result/)
    const profiles = [...c.querySelectorAll('.ol-lat-row')].map((r) => r.getAttribute('data-profile'))
    expect(profiles).toEqual(['mock', 'claude-code', 'codex'])
    expect(c.textContent).not.toMatch(/all profiles/i)
    const codex = c.querySelector('.ol-lat-row[data-profile="codex"]')!
    expect(codex.querySelectorAll('.ol-lat-none .ctl-em')).toHaveLength(2)
    expect(codex.textContent).toContain('timeout varies')
    const mockFailed = c.querySelector('.ol-lat-row[data-profile="mock"] .ol-lat-line[data-outcome="failed"]')!
    expect(mockFailed.textContent).toContain('wait 3s, 4s')
    const ccFailed = c.querySelector('.ol-lat-row[data-profile="claude-code"] .ol-lat-line[data-outcome="succeeded"]')!
    expect(ccFailed.textContent).toContain('p95')
    expect(c.querySelector('.ol-lat-row[data-profile="claude-code"] .ol-lat-timeout')).not.toBeNull()
    expect(codex.querySelector('.ol-lat-timeout')).toBeNull()
  })

  it('Reliability: all-cancelled and nothing-ended are phrases, low n is a hollow ring, cost never invents $0.00', async () => {
    const root = await timeline()
    const c = card(root, /^Reliability/)
    const row = (k: string) => c.querySelector(`tbody tr[data-key="${k}"]`)!
    expect(row('codex').textContent).toContain('— all cancelled')
    expect(row('generic').querySelector('.ol-ring'), 'two decided draws no hollow ring').not.toBeNull()
    expect(row('generic').textContent).not.toContain('$0.00')
    expect(row('generic').querySelector('.ol-cost.is-absent .ctl-em')).not.toBeNull()
    expect(row('claude-code').querySelector('.ol-cost .ctl-mark.is-partial'), '181 of 190 reported is drawn as a total').not.toBeNull()
    expect(row('mock').textContent).toContain('declared')
  })

  it('Reliability: the group-by writes the hash, and a tenant grouping is offered only in platform scope', async () => {
    const onView = vi.fn()
    const root = await timeline(ledgerFixture(), { view: '', onView })
    const seg = card(root, /^Reliability/).querySelector<HTMLElement>('.ol-group')!
    expect([...seg.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['runner profile', 'person'])
    fireEvent.click(within(seg).getByRole('button', { name: 'person' }))
    expect(onView).toHaveBeenLastCalledWith('group=submitted_by')
  })

  it('Reported cost: the partial figure, by task end, k of n attempts, and "Reported cost · not a bill"', async () => {
    const root = await timeline()
    const c = card(root, /^Reported cost/)
    expect(c.querySelector('.ctl-figure')!.textContent).toBe('$383.84')
    expect(c.querySelector('.ol-figure-line .ctl-mark.is-partial')).not.toBeNull()
    expect(c.querySelector('.ctl-card-note')!.textContent).toBe('by task end · 209 of 612 attempts')
    expect(c.querySelector('.ctl-card-foot')!.textContent).toBe('Reported cost · not a bill')
    expect(c.textContent).toContain('p50 $1.62 · p95 $5.80')
  })

  it('Reported cost: says "not reported", never $0.00, when no attempt reported', async () => {
    const d = ledgerFixture()
    d.totals = { ...d.totals, cost: { ...d.totals.cost, sum_usd: null, reporting: 0 } }
    const root = await timeline(d)
    const fig = card(root, /^Reported cost/).querySelector('.ol-figure-line')!
    expect(fig.textContent).toContain('not reported')
    expect(fig.textContent).not.toMatch(/\$/)
    expect(fig.querySelector('.ctl-mark.is-absent')).not.toBeNull()
  })

  it('Not finished yet: live, without the span, with the parks that need a person in warn ink', async () => {
    const root = await timeline()
    const c = card(root, /^Not finished yet/)
    expect(c.querySelector('.ctl-card-note')!.textContent).toBe('now · span not applied')
    await waitFor(() => expect(c.querySelector('.ol-open-counts')).not.toBeNull())
    expect(c.querySelector('.ol-open-counts')!.textContent).toContain('3 parked · 3 running · 2 queued · 1 ready')
    const person = c.querySelector('.ol-person')!
    expect(person.classList.contains('is-warn')).toBe(true)
    expect(person.textContent).toContain('CREDENTIAL_MISSING 2')
    expect(c.textContent).toContain('clears itself PROVIDER_QUOTA_EXHAUSTED 1')
  })

  it('Not finished yet: says its counts are not filtered by profile when a profile filter is set', async () => {
    const root = await timeline(ledgerFixture(), { view: 'profile=mock' })
    const c = card(root, /^Not finished yet/)
    await waitFor(() => expect(c.querySelector('.ol-open-counts')).not.toBeNull())
    expect(c.textContent).toContain('not filtered by profile')
  })

  it('Why tasks were cancelled: every cause in the server’s order', async () => {
    const root = await timeline()
    const c = card(root, /^Why tasks were cancelled/)
    const rows = [...c.querySelectorAll('.ol-row')]
    expect(rows.map((r) => r.getAttribute('data-row'))).toEqual(['requested', 'after a failure', 'workflow sweep', 'other'])
    expect(rows.map((r) => r.querySelector('.ol-row-n')!.textContent)).toEqual(['401', '12', '3', '0'])
  })
})

// ---------------------------------------------------------------------------
// The help it links, which has to be true of the page it is linked from
// ---------------------------------------------------------------------------

describe('the help the Timeline links', () => {
  it('has a success-rate topic that says cancels are left out and nothing decided is a gap', () => {
    const t = HELP['success-rate']
    const all = [t.short, ...t.long].join(' ')
    expect(all).toMatch(/Cancels are counted/)
    expect(all).toMatch(/Wilson/)
    expect(all).toMatch(/gap/)
  })

  it('no longer tells a reader the Timeline reads a window of Rows (AG-19 retired with the window)', () => {
    const t = HELP['event-paging']
    const all = [t.title, t.short, ...t.long].join(' ')
    expect(all).not.toMatch(/Rows/)
    expect(all).not.toMatch(/Timeline screen/)
  })

  it('describes Reported cost as per attempt, by task end, and not a bill', () => {
    const all = HELP['tokens-reported'].long.join(' ')
    expect(all).toMatch(/Reported cost/)
    expect(all).toMatch(/where the task ended/)
    expect(all).toMatch(/not a bill/)
    expect(all).not.toMatch(/Token spend/)
  })
})

// ---------------------------------------------------------------------------
// The tenants table (unchanged by #185)
// ---------------------------------------------------------------------------

describe('the tenants table', () => {
  it('AH-20: wraps the credential tags so they do not run together', async () => {
    const tenant: Tenant = {
      tenant_id: 'eng',
      kind: 'group',
      principal: 'eng@saga.xyz',
      display_name: null,
      created_at: '2026-09-24T09:00:00Z',
      max_active: 10,
      capacity_units: 20,
      monthly_budget_usd: null,
      enabled: true,
      credentials: ['anthropic', 'openai'],
      service_account: 'swarm-eng@example.iam.gserviceaccount.com',
      gcs_prefix: null,
      namespace: null,
    }
    api.loadTenants.mockResolvedValue({
      status: 'ok',
      data: { tenants: [tenant] },
      fetchedAt: Date.now(),
    } satisfies Result<{ tenants: Tenant[] }>)
    const { container } = render(<TenantsScreen />)
    await screen.findByText('eng@saga.xyz')
    const cell = container.querySelector('td[data-label="Credentials"]')!
    expect(cell.querySelectorAll('.tags > .tag'), 'two tags with nothing spacing them').toHaveLength(2)
  })
})
