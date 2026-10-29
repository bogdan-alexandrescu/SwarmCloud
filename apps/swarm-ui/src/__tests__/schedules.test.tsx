// AUTOMATE › SCHEDULES (docs/schedules.md §6, lane S7): the section, the list,
// one schedule's page, the section's badge and Overview's "Waiting on you".
//
// WHAT EACH CASE HOLDS (§9's S7 acceptance, for the screens this lane draws):
//   * `automate` is a literal section between Work and Capacity, its first tab
//     routes to the list, and `/automate/schedules/<id>` to one schedule;
//   * the list draws the columns the owner listed; a spend some attempt never
//     reported carries `Mark kind="partial"` and the count unreported, never
//     the floor alone; a tenant with no schedule gets the `Absent` state, not a
//     table of nothing; each registration's index cadence is a read-only
//     built-in row linking to its Settings;
//   * a schedule that never fired shows `Absent`, not "0 runs"; its history
//     links each firing's work; the audit tab reads the audit route;
//   * at 390px a row is a record, the header hidden, on the list and on one
//     schedule's firings, and the schedule's facts are one column;
//   * the Gate tab words §4.4's hard stops as the doc does: workflow files
//     refused, the rest held for a second member (the owner in a platform
//     repository), the failure count the schedule's own;
//   * the spine's Automate badge counts the inbox, draws nothing for a
//     measured zero, and a dash -- never 0 -- for a failed read;
//   * the Overview card appears only when something waits.
//
// The bodies are `routes/schedules.py::schedule_to_api`'s and
// `approvals.to_api`'s shapes. Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { SECTIONS, fromAddress, canonical, spineOf } from '../App'
import { addressToPath, pathToAddress } from '../paths'
import { PANEL_PAGES, approvalsBadge } from '../Spine'
import { HISTORY_VIEW_KEY } from '../ScheduleHistoryTimeline'
import SCHEDULE_DETAIL_TSX from '../ScheduleDetail.tsx?raw'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }

type Json = Record<string, unknown>

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

function schedule(id: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name: `Schedule ${id}`,
    type: 'issue-sweep',
    scope: { mode: 'repos', repo_ids: ['repo_a'] },
    cron: '0 9 * * 1-5',
    timezone: 'Europe/London',
    params: { max_new_per_firing: 3 },
    gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: hoursFromNow(-48),
    next_run_at: hoursFromNow(3),
    last_firing: { firing_id: `${id}:1`, slot: hoursFromNow(-21), outcome: 'succeeded', ended_at: hoursFromNow(-20) },
    consecutive_failures: 0,
    revision: 2,
    words: 'at 09:00, Monday through Friday',
    tier: 'R2',
    spend_today: { day: '2026-10-11', reported_usd: 3.1, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    state_word: 'active',
    ...over,
  }
}

const REPO = {
  repo_id: 'repo_a',
  owner: 'example-org',
  repo: 'example-api',
  default_branch: 'main',
  index: { interval_hours: 24, on_change: 'poll', min_change_interval_minutes: 30, paused: false, next_run_at: hoursFromNow(5) },
}

function firing(id: string, over: Json = {}): Json {
  return {
    firing_id: id,
    schedule_id: 'sch_a',
    type: 'issue-sweep',
    slot: hoursFromNow(-21),
    fired_at: hoursFromNow(-21),
    trigger: 'cron',
    state: 'done',
    work: [{ kind: 'issue_run', id: 'run_8f2a', repo_id: 'repo_a' }],
    skip: null,
    dry_run: null,
    approval_id: null,
    outcome: 'succeeded',
    cost: { reported_usd: 11.3, unreported_attempts: 0 },
    ended_at: hoursFromNow(-20),
    ...over,
  }
}

type Routes = Record<string, { status: number; body: unknown }>

async function mountList(routes: Routes) {
  const calls = serve((m, url) => (m === 'GET' ? routes[url.split('?')[0]!] ?? null : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={null} spy={go} />)
  await screen.findByRole('heading', { name: 'Schedules', level: 1 }, WAIT)
  return { ...utils, calls, go }
}

async function mountDetail(routes: Routes) {
  const calls = serve((m, url) => (m === 'GET' ? routes[url.split('?')[0]!] ?? null : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start="schedule=sch_a" spy={go} />)
  return { ...utils, calls, go }
}

/** Stub `matchMedia`, which jsdom does not have. */
function media(phone: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('560'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

const rowOf = (id: string) => document.querySelector<HTMLElement>(`tr[data-schedule="${id}"]`)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.localStorage.removeItem(HISTORY_VIEW_KEY)
})

/** The firings' table, behind the History tab's toggle since #1109 (Timeline is the default). */
const tableFirst = () => window.localStorage.setItem(HISTORY_VIEW_KEY, 'table')

describe('the Automate section', () => {
  it('is a literal section between Work and Capacity, with Schedules and Approvals as its tabs', () => {
    const ids = SECTIONS.map((s) => s.id)
    expect(ids.indexOf('automate')).toBe(ids.indexOf('work') + 1)
    expect(ids.indexOf('capacity')).toBe(ids.indexOf('automate') + 1)
    const automate = SECTIONS.find((s) => s.id === 'automate')!
    expect(automate.label).toBe('Automate')
    expect(automate.question.trim().endsWith('?')).toBe(true)
    expect(automate.tabs).toEqual([
      { id: 'schedules', label: 'Schedules' },
      { id: 'approvals', label: 'Approvals' },
    ])
    expect(PANEL_PAGES.automate.map((p) => p.to)).toEqual(['automate/schedules', 'automate/approvals'])
  })

  it('routes the list and one schedule, both ways', () => {
    expect(spineOf('automate', 'schedules')).toBe('automate')
    expect(addressToPath('automate/schedules')).toBe('/automate/schedules')
    expect(pathToAddress('/automate')?.address).toBe('automate/schedules')
    // MUTATION: drop `schedule` from keptKeys and the id is lost on the way in.
    const one = fromAddress('automate/schedules?schedule=sch_a')
    expect(one).toMatchObject({ sectionId: 'automate', tab: 'schedules', view: 'schedule=sch_a' })
    expect(canonical(one)).toBe('automate/schedules?schedule=sch_a')
    expect(addressToPath(canonical(one))).toBe('/automate/schedules/sch_a')
    expect(pathToAddress('/automate/schedules/sch_a')?.address).toBe('automate/schedules?schedule=sch_a')
  })
})

describe('the list', () => {
  it('draws one row per schedule with the columns the owner listed', async () => {
    await mountList({
      '/v1/schedules': { status: 200, body: { schedules: [schedule('sch_a', { pending_approvals: 2 })], tenant_id: 'eng' } },
      '/v1/repositories': { status: 200, body: { repositories: [REPO] } },
    })
    await waitFor(() => expect(rowOf('sch_a')).not.toBeNull(), WAIT)
    // Frame A1's columns (AUTO-A1, part of #2417): the actions moved to the schedule's own page.
    const heads = [...document.querySelectorAll('.al-table:not(.is-inbox) thead th')].map((th) => th.textContent)
    expect(heads).toEqual(['Schedule', 'Target', 'When', 'Next firing', 'Last firing', 'Spend today', 'Waiting', 'State'])
    const row = rowOf('sch_a')!
    expect(visible(row)).toContain('Schedule sch_a')
    expect(visible(row)).toContain('issue-sweep · R2')
    expect(visible(row)).toContain('at 09:00, Monday through Friday')
    expect(visible(row)).toContain('Europe/London')
    expect(visible(row)).toContain('$3.10')
    expect(visible(row.querySelector('td[data-label="Waiting"] .al-fig'))).toBe('2')
    expect(visible(row.querySelector('.al-word'))).toBe('active')
    await waitFor(() => expect(visible(row)).toContain('example-org/example-api'), WAIT)
  })

  it('marks a spend some attempt never reported as partial, with the count', async () => {
    // MUTATION: draw `reported_usd` alone when coverage is partial.
    await mountList({
      '/v1/schedules': {
        status: 200,
        body: { schedules: [schedule('sch_a', { spend_today: { day: '2026-10-11', reported_usd: 3.1, unreported_attempts: 2, coverage: 'partial' } })], tenant_id: 'eng' },
      },
      '/v1/repositories': { status: 200, body: { repositories: [] } },
    })
    await waitFor(() => expect(rowOf('sch_a')).not.toBeNull(), WAIT)
    const cell = rowOf('sch_a')!.querySelector('td[data-label="Spend today"]')!
    expect(cell.querySelector('.ctl-mark.is-partial')).not.toBeNull()
    expect(visible(cell)).toContain('$3.10')
    expect(visible(cell)).toContain('2 attempts unreported')
  })

  it('says a never-fired, paused schedule has no last run and no next run, rather than 0', async () => {
    await mountList({
      '/v1/schedules': {
        status: 200,
        body: {
          schedules: [schedule('sch_b', { state: 'paused', next_run_at: null, last_firing: null, pause: { by: 'a@example.com', reason: 'holiday' }, state_word: 'paused' })],
          tenant_id: 'eng',
        },
      },
      '/v1/repositories': { status: 200, body: { repositories: [] } },
    })
    await waitFor(() => expect(rowOf('sch_b')).not.toBeNull(), WAIT)
    const row = rowOf('sch_b')!
    expect(visible(row.querySelector('td[data-label="Last firing"]'))).toBe('— never fired')
    expect(visible(row.querySelector('td[data-label="Next firing"]'))).toContain('paused')
    expect(visible(row.querySelector('td[data-label="State"]'))).toContain('holiday')
  })

  it('draws the Absent state for a tenant with no schedule, and still the built-in rows', async () => {
    await mountList({
      '/v1/schedules': { status: 200, body: { schedules: [], tenant_id: 'eng' } },
      '/v1/repositories': { status: 200, body: { repositories: [REPO] } },
    })
    await screen.findByText('No schedules yet', undefined, WAIT)
    expect(document.querySelector('.sc-table:not(.is-builtin)')).toBeNull()
    const builtIn = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('tr[data-builtin="repo_a"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(visible(builtIn)).toContain('Index · example-org/example-api')
    expect(visible(builtIn)).toContain('24 h + on change')
    expect(visible(builtIn)).toContain('at most every 30 min')
    expect(within(builtIn).getByRole('link', { name: /Settings/ }).getAttribute('href')).toBe('/config/repositories/repo_a/settings')
  })

  it('says the built-in rows were not read when the repositories read fails', async () => {
    await mountList({
      '/v1/schedules': { status: 200, body: { schedules: [schedule('sch_a')], tenant_id: 'eng' } },
      '/v1/repositories': { status: 500, body: { code: 'internal', message: 'boom' } },
    })
    await screen.findByText('Built-in rows not read', undefined, WAIT)
  })

  it('opens a schedule by its address', async () => {
    const { go } = await mountList({
      '/v1/schedules': { status: 200, body: { schedules: [schedule('sch_a')], tenant_id: 'eng' } },
      '/v1/repositories': { status: 200, body: { repositories: [] } },
    })
    const link = await screen.findByRole('link', { name: 'Schedule sch_a' }, WAIT)
    expect(link.getAttribute('href')).toBe('/automate/schedules/sch_a')
    fireEvent.click(link)
    expect(go).toHaveBeenCalledWith('automate/schedules?schedule=sch_a')
  })

  it('keeps the built-in rows a record on a phone, every cell named', async () => {
    media(true)
    await mountList({
      '/v1/schedules': { status: 200, body: { schedules: [], tenant_id: 'eng' } },
      '/v1/repositories': { status: 200, body: { repositories: [REPO] } },
    })
    const row = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('tr[data-builtin="repo_a"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(painted(row, 'display', PHONE)).toBe('block')
    for (const td of row.querySelectorAll<HTMLElement>('td')) {
      expect(td.getAttribute('data-label'), 'a cell with no name').toBeTruthy()
      expect(painted(td, 'display', PHONE)).toBe('block')
    }
  })
})

// ---------------------------------------------------------------------------
// The actions (owner decisions 2026-10-11)
// ---------------------------------------------------------------------------

type Handler = (method: string, url: string, body: unknown) => { status: number; body: unknown } | null

/** Mount a Schedules address with a handler that sees every method; GETs fall back to `routes`. */
async function mountActing(view: string | null, routes: Routes, handler: Handler) {
  const calls = serve((m, url, body) => handler(m, url, body) ?? (m === 'GET' ? routes[url.split('?')[0]!] ?? null : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={view} spy={go} />)
  return { ...utils, calls, go }
}

const posts = (calls: { method: string; url: string; body: unknown }[], path: string) =>
  calls.filter((c) => c.method === 'POST' && c.url.split('?')[0] === path)
const reads = (calls: { method: string; url: string }[], path: string) =>
  calls.filter((c) => c.method === 'GET' && c.url.split('?')[0] === path).length

const created = (id: string) => firing(`${id}:now`, { trigger: 'run_now', state: 'created', outcome: null, cost: null, ended_at: null })

describe("a schedule's page head", () => {
  const ONE = (over: Json = {}): Routes => ({
    '/v1/schedules/sch_a': { status: 200, body: { schedule: schedule('sch_a', over), firings: [] } },
  })
  const head = () => screen.getByTestId('sc-acts-head')

  it('offers Run now, Dry run, Pause and Delete when enabled; Resume, not Pause, when paused', async () => {
    const first = await mountActing('schedule=sch_a', ONE(), () => null)
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    expect(within(head()).getAllByRole('button').map((b) => visible(b))).toEqual(['Run now', 'Dry run', 'Pause', 'Delete…'])
    expect(screen.getByRole('button', { name: 'Edit' })).toBeTruthy()
    first.unmount()
    await mountActing('schedule=sch_a', ONE({ state: 'auto_paused', next_run_at: null }), () => null)
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    expect(within(head()).getAllByRole('button').map((b) => visible(b))).toEqual(['Run now', 'Dry run', 'Resume', 'Delete…'])
  })

  it('dry-runs without asking and pauses after its reason dialog, through :run and :pause, then reads the schedule again', async () => {
    // MUTATION: route Dry run through the confirmation and the first click sends nothing.
    const { calls } = await mountActing('schedule=sch_a', ONE(), (m, url) => {
      if (m === 'POST' && url === '/v1/schedules/sch_a:run') {
        return { status: 200, body: { firing: firing('sch_a:dry', { trigger: 'run_now', work: [], skip: { code: 'DRY_RUN' }, cost: null }) } }
      }
      if (m === 'POST' && url === '/v1/schedules/sch_a:pause') {
        return { status: 200, body: { schedule: schedule('sch_a', { state: 'paused', next_run_at: null }) } }
      }
      return null
    })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    fireEvent.click(within(head()).getByRole('button', { name: 'Dry run' }))
    await waitFor(() => expect(posts(calls, '/v1/schedules/sch_a:run')).toHaveLength(1), WAIT)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(posts(calls, '/v1/schedules/sch_a:run')[0]!.body).toEqual({ dry_run: true })
    expect(visible(await screen.findByTestId('sc-said', undefined, WAIT))).toContain('Schedule sch_a recorded a dry run: nothing was created')
    await waitFor(() => expect(reads(calls, '/v1/schedules/sch_a')).toBe(2), WAIT)
    await screen.findByTestId('sc-acts-head', undefined, WAIT)
    fireEvent.click(within(head()).getByRole('button', { name: 'Pause' }))
    // Pause asks for an optional reason (QA1 automate-12); none written sends none.
    fireEvent.click(within(await screen.findByRole('dialog', undefined, WAIT)).getByRole('button', { name: 'Pause' }))
    await waitFor(() => expect(posts(calls, '/v1/schedules/sch_a:pause')).toHaveLength(1), WAIT)
    expect(posts(calls, '/v1/schedules/sch_a:pause')[0]!.body).toEqual({})
    expect(screen.queryByRole('dialog')).toBeNull()
    await waitFor(() => expect(visible(screen.getByTestId('sc-said'))).toContain('Paused: no new firings'), WAIT)
    await waitFor(() => expect(reads(calls, '/v1/schedules/sch_a')).toBe(3), WAIT)
  })

  it('asks before Run now on the page too, when it spends capacity', async () => {
    const { calls } = await mountActing('schedule=sch_a', ONE(), (m, url) =>
      m === 'POST' && url === '/v1/schedules/sch_a:run' ? { status: 200, body: { firing: created('sch_a') } } : null,
    )
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    fireEvent.click(within(head()).getByRole('button', { name: 'Run now' }))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Not now' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(posts(calls, '/v1/schedules/sch_a:run')).toHaveLength(0)
  })

  it('deletes only with the name typed, through DELETE with confirm, and goes back to the list', async () => {
    // MUTATION: drop the `disabledReason` on Delete schedule and it sends with any text.
    const { calls, go } = await mountActing('schedule=sch_a', ONE(), (m, url) =>
      m === 'DELETE' && url.split('?')[0] === '/v1/schedules/sch_a' ? { status: 200, body: { deleted: 'sch_a' } } : null,
    )
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    fireEvent.click(within(head()).getByRole('button', { name: 'Delete…' }))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    const confirm = () => within(dialog).getByRole('button', { name: 'Delete schedule' })
    expect(confirm()).toHaveProperty('disabled', true)
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'Schedule sch' } })
    expect(confirm()).toHaveProperty('disabled', true)
    fireEvent.click(confirm())
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0)
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: '  Schedule sch_a ' } })
    expect(confirm()).toHaveProperty('disabled', false)
    fireEvent.click(confirm())
    await waitFor(() => expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(1), WAIT)
    const sent = new URL(calls.find((c) => c.method === 'DELETE')!.url, 'http://x')
    expect(sent.pathname).toBe('/v1/schedules/sch_a')
    expect(sent.searchParams.get('confirm')).toBe('Schedule sch_a')
    await waitFor(() => expect(go).toHaveBeenCalledWith('automate/schedules'), WAIT)
  })

  it('keeps the dialog open and says so when the server refuses the delete', async () => {
    const { go } = await mountActing('schedule=sch_a', ONE(), (m) =>
      m === 'DELETE' ? { status: 422, body: { code: 'confirmation_required', message: 'type the name' } } : null,
    )
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    fireEvent.click(within(head()).getByRole('button', { name: 'Delete…' }))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'Schedule sch_a' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete schedule' }))
    expect(visible(await within(dialog).findByRole('alert', undefined, WAIT))).toContain('nothing was deleted')
    expect(go).not.toHaveBeenCalled()
  })
})

describe("the sheet's Spend today", () => {
  const USE = {
    day: '2026-10-11',
    per_day_usd: 120,
    per_run_usd: 15,
    reported_usd: 41.2,
    unreported_attempts: 1,
    unreported_usd: 15,
    live_firings: 1,
    live_items: 2,
    live_usd: 30,
    free_usd: 33.8,
  }

  async function budgetTab(use: unknown) {
    const over: Json = use === undefined ? {} : { budget_use: use }
    await mountDetail({ '/v1/schedules/sch_a': { status: 200, body: { schedule: schedule('sch_a', over), firings: [] } } })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    // Frame A2 draws Spend today as a section of the sheet, not a tab to open.
    await waitFor(() => expect(document.querySelector('aside.as-sheet [data-sec="spend"]')).not.toBeNull(), WAIT)
  }

  /** A part's width, as a share of the bar, to the hundredth of a percent. */
  const width = (part: string) => parseFloat(document.querySelector<HTMLElement>(`.sc-burn-bar [data-part="${part}"]`)!.style.width)
  const pct = (part: number, whole: number) => Number(((part / whole) * 100).toFixed(2))

  it("draws the burn bar from the served parts: reported, unreported at the cap, live, free", async () => {
    // MUTATION: draw `free` as per_day minus reported, and the $33.80 and its width go.
    await budgetTab(USE)
    const bar = await screen.findByTestId('sc-burn', undefined, WAIT)
    expect(width('reported')).toBe(pct(41.2, 120))
    expect(width('unreported')).toBe(12.5)
    expect(width('live')).toBe(25)
    expect(width('free')).toBe(pct(33.8, 120))
    const key = [...bar.querySelectorAll<HTMLElement>('.sc-burn-key li')].map((li) => visible(li))
    expect(key).toEqual([
      'Reported $41.20',
      '1 attempt unreported, counted at the $15.00 per-run cap: $15.00',
      '2 live runs reserved at $15.00 each: $30.00',
      'Free $33.80',
    ])
    expect(visible(bar)).toContain('$33.80 free of $120.00 today')
    expect(bar.querySelector('.ctl-mark.is-partial')).not.toBeNull()
    expect(bar.querySelector('.sc-burn-tick')).toBeNull()
  })

  it('draws the parts against their total, with the day marked, when they pass the day', async () => {
    await budgetTab({ ...USE, reported_usd: 100, free_usd: 0 })
    const bar = await screen.findByTestId('sc-burn', undefined, WAIT)
    expect(width('reported')).toBe(pct(100, 145))
    expect(width('free')).toBe(0)
    expect(parseFloat(bar.querySelector<HTMLElement>('.sc-burn-tick')!.style.left)).toBe(pct(120, 145))
    expect(visible(bar)).toContain('a new firing is skipped')
  })

  it('says its day as the console writes a day, "Oct 11", not as served (QA1 automate-21)', async () => {
    await budgetTab(USE)
    const sub = document.querySelector<HTMLElement>('aside.as-sheet [data-sec="spend"] small')!
    // MUTATION: put back `${s.spend_today.day}` in ScheduleDetail.tsx's Spend and it reads "2026-10-11".
    expect(visible(sub)).toBe('Oct 11 · the schedule’s own day')
  })

  it('draws no bar and invents no figure when the read did not serve the reservation', async () => {
    await budgetTab(undefined)
    await screen.findByTestId('sc-budget', undefined, WAIT)
    expect(document.querySelector('[data-testid="sc-burn"]')).toBeNull()
    expect(visible(screen.getByTestId('sc-burn-absent'))).toContain('did not serve')
  })
})

describe('one schedule', () => {
  it('shows Absent for a schedule that never fired, not "0 runs"', async () => {
    await mountDetail({
      '/v1/schedules/sch_a': { status: 200, body: { schedule: schedule('sch_a', { last_firing: null }), firings: [] } },
    })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    await screen.findByText('Never fired', undefined, WAIT)
    expect(document.body.textContent).not.toMatch(/\b0 runs\b/)
  })

  it('lists its firings, newest first, linking the work each made', async () => {
    tableFirst()
    const { go } = await mountDetail({
      '/v1/schedules/sch_a': {
        status: 200,
        body: {
          schedule: schedule('sch_a'),
          firings: [
            firing('sch_a:2', { cost: { reported_usd: 9.8, unreported_attempts: 1 } }),
            firing('sch_a:1', { work: [], state: 'done', outcome: 'skipped', skip: { code: 'OVERLAP' }, cost: null }),
          ],
        },
      },
    })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    const rows = document.querySelectorAll<HTMLElement>('tr[data-firing]')
    expect([...rows].map((r) => r.dataset.firing)).toEqual(['sch_a:2', 'sch_a:1'])
    const run = within(rows[0]!).getByRole('link', { name: 'run_8f2a' })
    expect(run.getAttribute('href')).toBe('/runs/run_8f2a')
    fireEvent.click(run)
    expect(go).toHaveBeenCalledWith('work/runs?run=run_8f2a')
    const cost = rows[0]!.querySelector('td[data-label="Cost"]')!
    expect(cost.querySelector('.ctl-mark.is-partial')).not.toBeNull()
    expect(visible(cost)).toContain('1 attempt unreported')
    // In words since QA2-FIX-21 (automate2-03), never the raw code.
    expect(visible(rows[1]!)).toContain('skipped: the last firing was still running')
  })

  it('says in plain words why a refused run was not started, under a firing that still succeeded', async () => {
    tableFirst()
    await mountDetail({
      '/v1/schedules/sch_a': {
        status: 200,
        body: {
          schedule: schedule('sch_a'),
          firings: [
            firing('sch_a:3', {
              outcome: 'succeeded',
              refused_runs: [{ run_id: 'run_8f2a', issue: 'acme/example-api#1030', code: 'WORKFLOWS_PATH' }],
            }),
          ],
        },
      },
    })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    const row = document.querySelector<HTMLElement>('tr[data-firing="sch_a:3"]')!
    const outcome = row.querySelector<HTMLElement>('td[data-label="Outcome"]')!
    expect(visible(outcome)).toContain('succeeded')
    expect(visible(outcome)).toContain('acme/example-api#1030 not started')
    expect(visible(outcome)).toContain('.github/workflows/ files, which SwarmCloud cannot push')
    expect(outcome.querySelector('[data-refused="WORKFLOWS_PATH"]')).not.toBeNull()
  })

  it('says in plain words why a firing started nothing, on each row (SCHED)', async () => {
    tableFirst()
    await mountDetail({
      '/v1/schedules/sch_a': {
        status: 200,
        body: {
          schedule: schedule('sch_a'),
          firings: [
            firing('sch_a:3', {
              work: [], cost: null,
              skip: {
                code: 'NOTHING_STARTED',
                detail: { reason: 'no_room', bound: 'max_live_runs', own_live: { running: 8, limit: 8, bound: 'max_live_runs' } },
              },
            }),
            firing('sch_a:2', {
              work: [], cost: null,
              skip: {
                code: 'NOTHING_STARTED',
                detail: { reason: 'repositories_not_read', failures: [{ repository: 'example-org/example-api', error: 'credential_missing' }] },
              },
            }),
            firing('sch_a:1', {
              work: [], cost: null,
              skip: { code: 'NOTHING_STARTED', detail: { reason: 'no_eligible_issue', passed_over_by_reason: { live_run: 2, in_flight_workflow: 1 } } },
            }),
            firing('sch_a:0', {
              selection: { own_live: { running: 3, limit: 8, bound: 'max_live_runs' } },
            }),
          ],
        },
      },
    })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    const outcome = (id: string) => visible(document.querySelector<HTMLElement>(`tr[data-firing="${id}"] td[data-label="Outcome"]`)!)
    expect(outcome('sch_a:3')).toContain('nothing started')
    expect(outcome('sch_a:3')).toContain('its live-runs limit is reached')
    expect(outcome('sch_a:3')).toContain('8 of 8 running from this schedule')
    expect(outcome('sch_a:3')).not.toContain('NOTHING_STARTED')
    expect(outcome('sch_a:2')).toContain('its repositories could not be read')
    expect(outcome('sch_a:2')).toContain('example-org/example-api: credential missing')
    expect(outcome('sch_a:1')).toContain('every open issue was passed over')
    expect(outcome('sch_a:1')).toContain('2 already have a live run')
    expect(outcome('sch_a:1')).toContain('1 in flight in a workflow')
    expect(outcome('sch_a:0')).toContain('3 of 8 running from this schedule')
  })

  it('draws the audit when its fold is opened, newest first as served', async () => {
    const { calls } = await mountDetail({
      '/v1/schedules/sch_a': { status: 200, body: { schedule: schedule('sch_a'), firings: [] } },
      '/v1/schedules/sch_a/audit': {
        status: 200,
        body: {
          audit: [
            { schedule_id: 'sch_a', action: 'pause', by: 'operator@example.com', at: hoursFromNow(-1), detail: { reason: 'holiday' } },
            { schedule_id: 'sch_a', action: 'create', by: 'operator@example.com', at: hoursFromNow(-40), detail: {} },
          ],
        },
      },
    })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    // Frame A2 folds the audit at the sheet's foot. The merge switch in the
    // gate section reads the audit for its last-editor rule as the sheet
    // opens, so what waits for the fold is the list, not the read.
    // MUTATION: draw the audit unfolded and entries are here before the click.
    await waitFor(() => expect(document.querySelector('aside.as-sheet [data-sec="audit"]')).not.toBeNull(), WAIT)
    expect(document.querySelectorAll('li[data-audit]')).toHaveLength(0)
    fireEvent.click(within(document.querySelector<HTMLElement>('[data-sec="audit"]')!).getByRole('button', { name: 'Audit' }))
    await waitFor(() => expect(document.querySelectorAll('li[data-audit]')).toHaveLength(2), WAIT)
    expect(calls.some((c) => c.url.startsWith('/v1/schedules/sch_a/audit'))).toBe(true)
    const entries = [...document.querySelectorAll<HTMLElement>('li[data-audit]')]
    expect(entries.map((e) => e.dataset.audit)).toEqual(['pause', 'create'])
    expect(visible(entries[0]!)).toContain('holiday')
  })

  it('shows the gate and the budget as read, with the hard stops beside them', async () => {
    await mountDetail({
      '/v1/schedules/sch_a': { status: 200, body: { schedule: schedule('sch_a'), firings: [] } },
    })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    // Frame A2 draws the gate and the budget as sections of the sheet, not tabs.
    const gate = await screen.findByTestId('sc-gate', undefined, WAIT)
    const facts = [...gate.querySelectorAll('dt')].map((d) => `${visible(d)}: ${visible(d.nextElementSibling)}`)
    expect(facts).toContain('Merge: waits for an approval at the green head')
    expect(facts).toContain('Expiry: an approval nobody decides expires after 72 h')
    expect(visible(gate)).toContain('.github/workflows/')
    // §4.4: outside a platform repository the holds are a second member's, not the owner's.
    // MUTATION: say "held for the owner" for every repository and this fails.
    const stops = facts.find((f) => f.startsWith('Hard stops: '))!
    expect(stops).toMatch(/\.github\/workflows\/ is refused/)
    expect(stops).toMatch(/held for a second member, or in a platform repository for the owner/)
    expect(stops).toMatch(/protected path/)
    expect(stops).toMatch(/frozen contract/)
    expect(stops).toMatch(/\b3 failed runs in a row pause/)
    // QA1 automate-09: the sheet draws the form's own `HardStops`, so the two cannot drift apart.
    // MUTATION: write the sentence out again in ScheduleDetail.tsx.
    expect(SCHEDULE_DETAIL_TSX).toContain('<HardStops />')
    expect(SCHEDULE_DETAIL_TSX).not.toMatch(/held for a second member, or in a platform/)
    const budget = await screen.findByTestId('sc-budget', undefined, WAIT)
    expect(visible(budget)).toContain('$15.00')
    expect(visible(budget)).toContain('$120.00')
  })
})

describe('one schedule on a phone', () => {
  it('draws each firing as a record, every cell named, and its facts in one column', async () => {
    // MUTATION: drop the 560px block from schedules.css.
    media(true)
    tableFirst()
    await mountDetail({
      '/v1/schedules/sch_a': { status: 200, body: { schedule: schedule('sch_a'), firings: [firing('sch_a:1')] } },
    })
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    const table = document.querySelector<HTMLElement>('.sc-table.is-firings')!
    expect(painted(table.querySelector<HTMLElement>('thead')!, 'display', PHONE)).toBe('none')
    const row = document.querySelector<HTMLElement>('tr[data-firing="sch_a:1"]')!
    expect(painted(row, 'display', PHONE)).toBe('block')
    for (const td of row.querySelectorAll<HTMLElement>('td')) {
      expect(td.getAttribute('data-label'), 'a cell with no name').toBeTruthy()
      expect(painted(td, 'display', PHONE)).toBe('block')
    }
    // The sheet's facts are one column on a phone (frame A7b), a name over its value.
    // MUTATION: drop the 560px block from automate-sheet.css and the names sit beside their values.
    const facts = document.querySelector<HTMLElement>('aside.as-sheet [data-sec="what"] dl.as-kv')!
    expect(painted(facts, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
  })
})

describe('the Automate badge', () => {
  const ok = (n: number) => ({ status: 'ok' as const, fetchedAt: 0, data: { approvals: Array.from({ length: n }, () => ({})) } })

  it('counts the inbox, draws nothing for none, and a dash for a failed read', () => {
    expect(approvalsBadge(ok(4))).toMatchObject({ n: 4, alert: true })
    expect(approvalsBadge({ status: 'empty', fetchedAt: 0 })).toBeNull()
    expect(approvalsBadge({ status: 'loading', since: 0 })).toBeNull()
    // MUTATION: map a failure to `{ n: 0 }`.
    const failed = approvalsBadge({ status: 'error', error: { kind: 'server', message: 'boom', httpStatus: 500, code: null } as never })
    expect(failed?.n).toBeNull()
    expect(failed?.why).toMatch(/not read/)
  })

  it('is drawn on the spine beside Automate', async () => {
    serve((m, url) => (m === 'GET' && url.startsWith('/v1/approvals') ? { status: 200, body: { approvals: [{ approval_id: 'a1' }, { approval_id: 'a2' }] } } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SkyShell } = await import('../Spine')
    render(
      <SkyShell section="automate" tab="schedules" title="Schedules" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
    const link = document.querySelector<HTMLElement>('.sk-spine a[data-sec="automate"]')!
    await waitFor(() => expect(link.querySelector('.sk-badge')?.textContent).toBe('2'), WAIT)
    expect(document.querySelector('.sk-panel .sk-pk.is-on .sk-pl')?.textContent).toBe('Schedules')
  })
})

describe('Waiting on you', () => {
  async function mountCard(body: unknown, status = 200) {
    serve((m, url) => (m === 'GET' && url.startsWith('/v1/approvals') ? { status, body } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { WaitingOnYouCard } = await import('../Schedules')
    const go = vi.fn()
    const utils = render(<WaitingOnYouCard go={go} />)
    return { ...utils, go }
  }

  it('draws nothing when nothing waits', async () => {
    const { container } = await mountCard({ approvals: [] })
    await new Promise((r) => setTimeout(r, 50))
    expect(container.innerHTML).toBe('')
  })

  it('lists what waits, each linking to its subject', async () => {
    const { go } = await mountCard({
      approvals: [
        { approval_id: 'run:run_1', kind: 'plan', subject: { run_id: 'run_1', issue: 'example-api#12' }, summary: 'example-api#12: fix it', state: 'pending', requested_at: hoursFromNow(-2), expires_at: null, projected: true },
        { approval_id: 'merge:x', kind: 'merge', subject: { run_id: 'run_2', schedule_id: 'sch_a' }, summary: 'merge run_2', state: 'pending', requested_at: hoursFromNow(-1), expires_at: hoursFromNow(70) },
        { approval_id: 'run:sch_a:9', kind: 'run', subject: { schedule_id: 'sch_a', firing_id: 'sch_a:9' }, summary: 'Weekday sweep', state: 'pending', requested_at: hoursFromNow(-1), expires_at: null },
      ],
    })
    const card = await screen.findByRole('region', { name: /Waiting on you/ }, WAIT)
    expect(visible(card)).toContain('3')
    expect(visible(card)).toContain('hold no capacity')
    const links = within(card).getAllByRole('link')
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/runs/run_1', '/runs/run_2', '/automate/schedules/sch_a'])
    fireEvent.click(links[2]!)
    expect(go).toHaveBeenCalledWith('automate/schedules?schedule=sch_a')
  })
})

// LIVE QA, AUTOMATE (2026-10-11): the list and one schedule's page.
describe('live QA: automate, the list and the page', () => {
  const refresh = () => fireEvent.click(document.querySelector<HTMLButtonElement>('button.c-refresh')!)

  it('labels a dry-run schedule as one in the list and in the head (AUT-3)', async () => {
    const dry = schedule('sch_a', { name: 'Dry sweep', state: 'paused', state_word: 'paused', next_run_at: null, policy: { dry_run: true } })
    const list = await mountList({ '/v1/schedules': { status: 200, body: { schedules: [dry, schedule('sch_b')], tenant_id: 'eng' } }, '/v1/repositories': { status: 200, body: { repositories: [REPO] } } })
    await waitFor(() => expect(document.querySelector('tr[data-schedule="sch_a"]')).not.toBeNull(), WAIT)
    // MUTATION: drop `DryRunChip` from the State cell and the row says only "paused".
    expect(visible(document.querySelector('tr[data-schedule="sch_a"] td[data-label="State"]'))).toContain('dry run')
    expect(document.querySelector('tr[data-schedule="sch_b"] [data-testid="sc-dry"]')).toBeNull()
    list.unmount()
    await mountDetail({ '/v1/schedules/sch_a': { status: 200, body: { schedule: dry, firings: [] } } })
    await screen.findByRole('heading', { name: 'Dry sweep', level: 1 }, WAIT)
    // The sheet's head line (frame A2) carries the state word and the chip.
    const state = await screen.findByTestId('as-meta', undefined, WAIT)
    expect(visible(state.querySelector('.as-word'))).toBe('paused')
    expect(visible(state)).toContain('dry run')
  })

  it('keeps the open fold and the editor in the address (AUT-11)', async () => {
    expect(addressToPath('automate/schedules?schedule=sch_a&tab=settings')).toBe('/automate/schedules/sch_a?tab=settings')
    expect(pathToAddress('/automate/schedules/sch_a', '?tab=settings')?.address).toBe('automate/schedules?schedule=sch_a&tab=settings')
    expect(addressToPath('automate/schedules?schedule=sch_a&edit=1')).toBe('/automate/schedules/sch_a/edit')
    expect(pathToAddress('/automate/schedules/sch_a/edit')?.address).toBe('automate/schedules?schedule=sch_a&edit=1')
    // MUTATION: leave `tab` out of keptKeys and a reload drops it.
    expect(canonical(fromAddress('automate/schedules?schedule=sch_a&tab=settings'))).toBe('automate/schedules?schedule=sch_a&tab=settings')
    const { go } = await mountActing('schedule=sch_a&tab=settings', { '/v1/schedules/sch_a': { status: 200, body: { schedule: schedule('sch_a'), firings: [] } } }, () => null)
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    // Opened with Settings unfolded: the fold the address names (frame A2 folds Settings and Audit).
    const settings = () => document.querySelector<HTMLElement>('aside.as-sheet [data-sec="settings"]')!
    await waitFor(() => expect(settings()).not.toBeNull(), WAIT)
    expect(within(settings()).getByRole('button', { name: 'Settings' }).getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(within(document.querySelector<HTMLElement>('aside.as-sheet [data-sec="audit"]')!).getByRole('button', { name: 'Audit' }))
    expect(go).toHaveBeenLastCalledWith('automate/schedules?schedule=sch_a&tab=audit')
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(go).toHaveBeenLastCalledWith('automate/schedules?schedule=sch_a&edit=1')
  })

  it('says a schedule deleted while its page was open was deleted, not "showing older data" (AUT-9)', async () => {
    let gone = false
    await mountActing('schedule=sch_a', {}, (m, url) =>
      m === 'GET' && url.split('?')[0] === '/v1/schedules/sch_a'
        ? gone
          ? { status: 404, body: { code: 'not_found', message: 'schedule sch_a not found' } }
          : { status: 200, body: { schedule: schedule('sch_a'), firings: [] } }
        : m === 'POST' && url === '/v1/schedules/sch_a:pause'
          ? { status: 200, body: { schedule: schedule('sch_a', { state: 'paused', next_run_at: null }) } }
          : null,
    )
    await screen.findByRole('heading', { name: 'Schedule sch_a', level: 1 }, WAIT)
    await screen.findByTestId('sc-acts-head', undefined, WAIT)
    gone = true
    // The sheet reads itself again after an action (frame A2); the list's ⟳ re-reads the list.
    fireEvent.click(within(screen.getByTestId('sc-acts-head')).getByRole('button', { name: 'Pause' }))
    fireEvent.click(within(await screen.findByRole('dialog', undefined, WAIT)).getByRole('button', { name: 'Pause' }))
    // MUTATION: drop `setGone` and the stale record stays, Edit enabled.
    await screen.findByText('This schedule was deleted', undefined, WAIT)
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(screen.getByRole('link', { name: '‹ Schedules' }).getAttribute('href')).toBe('/automate/schedules')
  })

  it('still says Not found for a schedule that was never read (AUT-9 control)', async () => {
    await mountDetail({ '/v1/schedules/sch_a': { status: 404, body: { code: 'not_found', message: 'no such schedule' } } })
    await waitFor(() => expect(document.body.textContent).toMatch(/not.found/i), WAIT)
    expect(screen.queryByText('This schedule was deleted')).toBeNull()
  })

  it('re-reads the built-in rows with the list on ⟳ (AUT-19)', async () => {
    const { calls } = await mountList({
      '/v1/schedules': { status: 200, body: { schedules: [], tenant_id: 'eng' } },
      '/v1/repositories': { status: 200, body: { repositories: [REPO] } },
    })
    await waitFor(() => expect(document.querySelector('tr[data-builtin="repo_a"]')).not.toBeNull(), WAIT)
    const before = reads(calls, '/v1/repositories')
    refresh()
    // MUTATION: read the repositories in the body's own effect again and ⟳ never asks for them.
    await waitFor(() => expect(reads(calls, '/v1/repositories')).toBe(before + 1), WAIT)
  })
})
