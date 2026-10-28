// ONE SCHEDULE IN THE SIDE SHEET OVER THE LIST (proposal A, frame A2, with
// A6's never-fired state and A7's phone for this sheet; lane AUTO-A2, part of
// #2417).
//
// WHAT EACH CASE HOLDS:
//   * a list row opens the sheet beside the list, which stays drawn with the
//     open row marked; Close and Escape go back to the list;
//   * the sheet's state word is the API's `state_word` in the reader's words,
//     a disabled schedule reading paused (BGD, 2026-10-26);
//   * each firing names its runs and the pull request each opened, with that
//     pull request's state as served (`work[].pull_request`);
//   * the largest run is drawn against the per-run tripwire, over it said in
//     words, and an unknown one is a dash with the server's reason, never $0;
//   * the day bar's four parts are `budget_use`'s, the day budget and the
//     tripwire beside it;
//   * the gate says who approves, and the sheet never says Deny;
//   * Settings and Audit are folded, the audit drawn only once its fold opens;
//   * a schedule that never fired is the Absent state, not "0 runs";
//   * at 1440 the sheet is fixed to the right and the list stays drawn; at
//     390 the sheet is the page and `‹ Schedules` goes back. Asked of
//     `painted` (cssgate's cascade), because jsdom applies no `@media` block.
//
// The bodies are `routes/schedules.py::get_schedule`'s and `list_schedules`'s
// shapes. Placeholders only; every id is built at runtime and nothing is
// token-shaped.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { holdScheduleClock, minutesAgo } from './scheduleclock'

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

type Json = Record<string, unknown>

const runId = (n: number) => `run_${String(n).padStart(4, '0')}`

function schedule(id: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name: `Sweep ${id}`,
    type: 'issue-sweep',
    scope: { mode: 'all' },
    cron: '0 2 * * *',
    timezone: 'UTC',
    params: { labels: ['ready'] },
    gate: { run: 'auto', plan: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 4 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false, max_consecutive_failures: 3 },
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: minutesAgo(10_000),
    next_run_at: minutesAgo(-300),
    last_firing: { firing_id: `${id}:1`, slot: minutesAgo(420), outcome: 'succeeded', ended_at: minutesAgo(400) },
    consecutive_failures: 0,
    revision: 14,
    words: 'every day at 02:00',
    tier: 'R2',
    spend_today: { day: '2026-10-27', reported_usd: 22.6, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 2,
    state_word: 'waiting_on_approval',
    budget_use: {
      day: '2026-10-27', per_day_usd: 120, per_run_usd: 15, reported_usd: 22.6, unreported_attempts: 1, unreported_usd: 15,
      live_firings: 1, live_items: 1, live_usd: 15, free_usd: 67.4,
    },
    ...over,
  }
}

function firing(n: number, over: Json = {}): Json {
  return {
    firing_id: `sch_a:${n}`,
    schedule_id: 'sch_a',
    type: 'issue-sweep',
    slot: minutesAgo(420 + n * 1440),
    fired_at: minutesAgo(417 + n * 1440),
    trigger: 'cron',
    state: 'done',
    work: [],
    skip: null,
    dry_run: null,
    approval_id: null,
    outcome: 'succeeded',
    cost: { reported_usd: 5.9, unreported_attempts: 0 },
    ended_at: minutesAgo(400 + n * 1440),
    largest_run_usd: 5.9,
    largest_run_unread: null,
    ...over,
  }
}

/** The newest firing: three runs, one PR waiting to merge, one merged, one plan held with no PR. */
const NEWEST = firing(0, {
  work: [
    { kind: 'issue_run', id: runId(1), repo_id: 'repo_a', pull_request: { number: 2398, state: 'waiting_on_approval' } },
    { kind: 'issue_run', id: runId(2), repo_id: 'repo_a', pull_request: { number: 2404, state: 'merged' } },
    { kind: 'issue_run', id: runId(3), repo_id: 'repo_a', outcome: null },
  ],
  cost: { reported_usd: 22.6, unreported_attempts: 0 },
  largest_run_usd: 9.8,
})
const OVER = firing(1, {
  outcome: 'failed',
  work: [{ kind: 'issue_run', id: runId(4), repo_id: 'repo_a', pull_request: { number: 2387, state: 'closed' } }],
  cost: { reported_usd: 16.2, unreported_attempts: 0 },
  largest_run_usd: 16.2,
})
const UNKNOWN = firing(2, {
  work: [{ kind: 'issue_run', id: runId(5), repo_id: 'repo_a' }],
  cost: { reported_usd: 3, unreported_attempts: 1 },
  largest_run_usd: null,
  largest_run_unread: 'a run has attempts that reported no cost, so the largest is not known',
})
const NOTHING = firing(3, { outcome: 'skipped', work: [], cost: null, largest_run_usd: null, largest_run_unread: 'it started no run' })

type Routes = Record<string, { status: number; body: unknown }>

function routes(s: Json, firings: Json[], over: Partial<Routes> = {}): Routes {
  const id = String(s.schedule_id)
  return {
    '/v1/schedules': {
      status: 200,
      body: { schedules: [s], tenant_id: 'eng', summary: { by_state: { [String(s.state_word)]: 1 }, pending_approvals: 0, oldest_expires_at: null } },
    },
    [`/v1/schedules/${id}`]: { status: 200, body: { schedule: s, firings } },
    [`/v1/schedules/${id}/audit`]: { status: 200, body: { audit: [{ schedule_id: id, action: 'edited', by: 'operator@example.com', at: minutesAgo(60), detail: {} }] } },
    '/v1/schedule-types': {
      status: 200,
      body: { types: [{ name: 'issue-sweep', description: 'Start an issue run for each ready open issue.', floor_gate: { merge: 'approve' } }] },
    },
    '/v1/approvals': { status: 200, body: { approvals: [], tenant_id: 'eng' } },
    '/v1/repositories': { status: 200, body: { repositories: [] } },
    ...over,
  } as Routes
}

const NEXT = ['2026-10-28T02:00:00+00:00', '2026-10-29T02:00:00+00:00', '2026-10-30T02:00:00+00:00', '2026-10-31T02:00:00+00:00', '2026-11-01T02:00:00+00:00']

async function mount(r: Routes, start: string | null) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0]!
    if (m === 'POST' && path === '/v1/schedules:preview') return { status: 200, body: { preview: { words: 'every day at 02:00', next: NEXT, min_gap_minutes: 1440, refusal: null } } }
    return m === 'GET' ? r[path] ?? null : null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={start} spy={go} />)
  return { ...utils, calls, go }
}

/** Mount with one schedule's sheet open, and wait for its body. */
async function open(s: Json, firings: Json[], over: Partial<Routes> = {}) {
  const m = await mount(routes(s, firings, over), `schedule=${String(s.schedule_id)}`)
  await screen.findByRole('heading', { name: String(s.name), level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('[data-sec="gate"]')).not.toBeNull(), WAIT)
  return m
}

const sheet = () => document.querySelector<HTMLElement>('aside.as-sheet')
const sec = (name: string) => sheet()!.querySelector<HTMLElement>(`[data-sec="${name}"]`)!
const row = (fid: string) => sheet()!.querySelector<HTMLElement>(`tr[data-firing="${fid}"]`)!
const cell = (fid: string, label: string) => row(fid).querySelector<HTMLElement>(`td[data-label="${label}"]`)!
/** A `dl.as-kv`'s value for a name. */
function kv(root: HTMLElement, name: string): string {
  const dt = [...root.querySelectorAll('dt')].find((d) => visible(d) === name)
  expect(dt, `no ${name}`).toBeTruthy()
  return visible(dt!.nextElementSibling)
}

beforeEach(() => holdScheduleClock())

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('one schedule in the side sheet (frame A2)', () => {
  it('opens from a list row beside the list, marks the row, and closes back to the list', async () => {
    const s = schedule('sch_a')
    const { go } = await mount(routes(s, [NEWEST]), null)
    const link = await screen.findByRole('link', { name: 'Sweep sch_a' }, WAIT)
    // MUTATION: a row that opens the old full page and the list is gone.
    fireEvent.click(link)
    const h = await screen.findByRole('heading', { name: 'Sweep sch_a', level: 1 }, WAIT)
    expect(sheet()!.contains(h)).toBe(true)
    expect(screen.getByRole('heading', { name: 'Schedules', level: 1 })).toBeTruthy()
    await waitFor(() => expect(document.querySelector('tr[data-schedule="sch_a"]')).not.toBeNull(), WAIT)
    const opened = document.querySelector<HTMLElement>('tr[data-schedule="sch_a"]')!
    expect(opened.classList.contains('is-open')).toBe(true)
    expect(within(opened).getByRole('link', { name: 'Sweep sch_a' }).getAttribute('aria-current')).toBe('true')
    // Close goes back to the list, which is still drawn.
    fireEvent.click(within(sheet()!).getByRole('button', { name: 'Close this schedule' }))
    expect(go).toHaveBeenLastCalledWith('automate/schedules')
    await waitFor(() => expect(sheet()).toBeNull(), WAIT)
    expect(screen.getByRole('heading', { name: 'Schedules', level: 1 })).toBeTruthy()
    // Escape closes it too.
    fireEvent.click(await screen.findByRole('link', { name: 'Sweep sch_a' }, WAIT))
    await screen.findByRole('heading', { name: 'Sweep sch_a', level: 1 }, WAIT)
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(sheet()).toBeNull(), WAIT)
  })

  it('says the state word per state, a disabled schedule reading paused', async () => {
    const cases: [Json, string][] = [
      [{ state_word: 'active' }, 'active'],
      [{ state: 'paused', next_run_at: null, state_word: 'paused' }, 'paused'],
      [{ state: 'disabled', next_run_at: null, state_word: 'paused' }, 'paused'],
      [{ state: 'auto_paused', next_run_at: null, state_word: 'paused_by_tripwire' }, 'paused by its tripwire'],
      [{ state_word: 'waiting_on_approval' }, 'waiting on approval'],
      [{ state_word: 'failing', consecutive_failures: 2 }, 'failing'],
      [{ state_word: 'never_fired', last_firing: null }, 'never fired'],
    ]
    let seen = 0
    for (const [over, words] of cases) {
      const { unmount } = await open(schedule('sch_a', over), [])
      // MUTATION: the stored `state` drawn instead of `state_word`, and disabled reads disabled.
      expect(visible(sheet()!.querySelector('.as-word'))).toBe(words)
      expect(visible(sheet()!.querySelector('.as-head'))).not.toMatch(/\bdisabled\b/)
      unmount()
      seen += 1
    }
    expect(seen).toBe(cases.length)
  })

  it('says a paused schedule has no next firing, and reads none for it', async () => {
    const { calls } = await open(schedule('sch_a', { state: 'paused', next_run_at: null, state_word: 'paused' }), [])
    expect(visible(sheet()!.querySelector('[data-testid="as-next"]'))).toBe('— paused, so no next firing')
    expect(calls.some((c) => c.url.startsWith('/v1/schedules:preview'))).toBe(false)
  })

  it('draws the next five firings of a schedule that fires, from the preview', async () => {
    await open(schedule('sch_a'), [NEWEST])
    const next = await within(sec('cadence')).findByRole('list', { name: 'The next firings' }, WAIT)
    expect(within(next).getAllByRole('listitem')).toHaveLength(5)
    // The slot's own wall time, in the zone the heading names.
    expect(visible(within(next).getAllByRole('listitem')[0]!)).toBe('Oct 28 02:00')
    expect(visible(sec('cadence').querySelector('h3'))).toContain('0 2 * * *')
  })

  it('lists each firing with its runs and the pull request each opened', async () => {
    await open(schedule('sch_a'), [NEWEST, OVER, UNKNOWN, NOTHING])
    const work = cell('sch_a:0', 'Runs and pull requests')
    expect(visible(work)).toContain('3 runs')
    // MUTATION: drop `pull_request` and the runs read as bare ids.
    expect(visible(work.querySelector(`[data-work="${runId(1)}"]`))).toBe(`${runId(1)} → PR #2398 waiting to merge`)
    expect(visible(work.querySelector(`[data-work="${runId(2)}"]`))).toBe(`${runId(2)} → PR #2404 merged`)
    expect(visible(work.querySelector(`[data-work="${runId(3)}"]`))).toBe(runId(3))
    expect(within(work).getByRole('link', { name: runId(1) })).toBeTruthy()
    expect(visible(cell('sch_a:1', 'Runs and pull requests'))).toContain('PR #2387 closed unmerged')
    expect(visible(cell('sch_a:3', 'Runs and pull requests'))).toBe('nothing started')
    expect(visible(cell('sch_a:0', 'Firing'))).toContain('3 min late')
    expect(visible(cell('sch_a:0', 'Outcome'))).toContain('succeeded')
  })

  it('draws the largest run against the per-run tripwire, and an unknown one as a dash with its reason', async () => {
    await open(schedule('sch_a'), [NEWEST, OVER, UNKNOWN, NOTHING])
    const under = cell('sch_a:0', 'Largest run vs tripwire')
    expect(under.querySelector('[data-largest]')!.getAttribute('data-largest')).toBe('under')
    expect(visible(under)).toBe('$9.80 of $15.00')
    // The tick at three quarters of the scale, the bar 9.80 / 20.00 of it.
    expect(under.querySelector<HTMLElement>('.as-trip-bar > i')!.style.width).toBe('49%')
    expect(under.querySelector<HTMLElement>('.as-trip-bar > s')!.style.left).toBe('75%')
    const over = cell('sch_a:1', 'Largest run vs tripwire')
    // MUTATION: no over test and a run past the tripwire reads like any other.
    expect(over.querySelector('[data-largest]')!.getAttribute('data-largest')).toBe('over')
    expect(visible(over)).toBe('$16.20 of $15.00, over')
    expect(visible(cell('sch_a:2', 'Largest run vs tripwire'))).toBe('— a run has attempts that reported no cost, so the largest is not known')
    expect(visible(cell('sch_a:3', 'Largest run vs tripwire'))).toBe('— it started no run')
    expect(visible(sheet()!.querySelector('.as-firings tbody'))).not.toContain('$0.00')
  })

  it('draws the day budget bar in its four served parts, with the day budget and the tripwire', async () => {
    await open(schedule('sch_a'), [NEWEST])
    const bar = sec('spend').querySelector<HTMLElement>('.sc-burn-bar')!
    const width = (part: string) => bar.querySelector<HTMLElement>(`[data-part="${part}"]`)!.style.width
    // MUTATION: a bar of `spend_today` alone drops the unreported and live parts.
    expect([width('reported'), width('unreported'), width('live'), width('free')]).toEqual(['18.83%', '12.5%', '12.5%', '56.17%'])
    const facts = sec('spend').querySelector<HTMLElement>('dl.as-kv')!
    expect(kv(facts, 'Day budget')).toBe('$120.00 a day. When it is spent, the next firing is skipped.')
    expect(kv(facts, 'Per-run tripwire')).toBe('$15.00 a run. A run that costs more fails its firing and pauses this schedule.')
    expect(kv(facts, 'Live runs')).toBe('at most 4 at once')
  })

  it('says the gate and who approves, and never says Deny', async () => {
    await open(schedule('sch_a', { gate: { run: 'auto', plan: 'approve', merge: 'approve', approvers: 'owner_only', approval_ttl_hours: 72 } }), [NEWEST])
    const gate = sec('gate').querySelector<HTMLElement>('dl.as-kv')!
    expect(kv(gate, 'Run')).toBe('starts by itself')
    expect(kv(gate, 'Plan')).toBe('waits for an approval')
    expect(kv(gate, 'Merge')).toBe('waits for an approval at the green head')
    // MUTATION: the raw `owner_only` drawn.
    expect(kv(gate, 'Who approves')).toBe('the platform owner only')
    expect(kv(gate, 'Expiry')).toBe('an approval nobody decides expires after 72 h')
    expect(kv(gate, 'Waiting')).toBe('2 approvals waiting · Open Approvals')
    expect(visible(sheet())).not.toMatch(/\bDen(y|ied)\b/)
  })

  it('folds Settings and Audit at the foot, drawing the audit only when its fold opens', async () => {
    const { go } = await open(schedule('sch_a'), [NEWEST])
    const settings = within(sec('settings')).getByRole('button', { name: 'Settings' })
    const audit = within(sec('audit')).getByRole('button', { name: 'Audit' })
    expect(settings.getAttribute('aria-expanded')).toBe('false')
    expect(audit.getAttribute('aria-expanded')).toBe('false')
    expect(sec('settings').querySelector('.sc-facts')).toBeNull()
    // MUTATION: draw the audit open and every sheet lists it.
    expect(sec('audit').querySelector('[data-audit]')).toBeNull()
    fireEvent.click(audit)
    expect(go).toHaveBeenLastCalledWith('automate/schedules?schedule=sch_a&tab=audit')
    await waitFor(() => expect(sec('audit').querySelector('[data-audit="edited"]')).not.toBeNull(), WAIT)
    expect(within(sec('audit')).getByRole('button', { name: 'Audit' }).getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(within(sec('settings')).getByRole('button', { name: 'Settings' }))
    await waitFor(() => expect(sec('settings').querySelector('.sc-facts')).not.toBeNull(), WAIT)
    expect(visible(sec('settings'))).toContain('Revision14')
  })

  it('shows Absent for a schedule that never fired, not a table or "0 runs"', async () => {
    await open(schedule('sch_a', { last_firing: null, state_word: 'never_fired', pending_approvals: 0 }), [])
    const f = sec('firings')
    // MUTATION: an empty table, or "0 runs", reads as a count of something that ran.
    expect(f.querySelector('table')).toBeNull()
    expect(visible(f)).toContain('Never fired')
    expect(visible(f)).not.toMatch(/\b0 runs?\b/)
  })

  it('lies over the list at 1440 and is the page at 390', async () => {
    await open(schedule('sch_a'), [NEWEST])
    const aside = sheet()!
    const under = document.querySelector<HTMLElement>('.as-under')!
    const back = aside.querySelector<HTMLElement>('.as-back')!
    const close = aside.querySelector<HTMLElement>('.as-close')!
    // MUTATION: drop the 561px block and the sheet pushes the list off the page.
    expect(painted(aside, 'position', WIDE)).toBe('fixed')
    expect(painted(aside, 'width', WIDE)).toBe('min(680px, calc(100vw - 360px))')
    expect(painted(under, 'display', WIDE)).not.toBe('none')
    expect(painted(back, 'display', WIDE)).toBe('none')
    // MUTATION: drop the phone block and a 390 screen draws both, the sheet over the list.
    expect(painted(aside, 'position', PHONE)).not.toBe('fixed')
    expect(painted(under, 'display', PHONE)).toBe('none')
    expect(painted(back, 'display', PHONE)).toBe('inline-flex')
    expect(painted(close, 'display', PHONE)).toBe('none')
  })
})
