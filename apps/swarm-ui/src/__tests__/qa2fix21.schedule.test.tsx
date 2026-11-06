// QA2-FIX-21 (part of #2694): the schedule sheet's pass-2 findings.
//
//   automate2-02  an empty firings list beside a served `last_firing` read
//                 "Never fired" although the list said it failed 5h ago;
//   automate2-03  a skipped firing printed its raw code (`skipped: OVERLAP`),
//                 and one served without a code `skipped: undefined`;
//   automate2-10  a type that never merges was told to edit its gate to
//                 approve each merge;
//   automate2-18  a budget field left to the type read "— not set";
//   automate2-19  a late firing read `300 min late`.
//
// The bodies are `routes/schedules.py::get_schedule`'s shapes, as in
// automate.sheet.test.tsx. Placeholders only; nothing is token-shaped.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import { holdScheduleClock, minutesAgo } from './scheduleclock'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

function schedule(id: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name: `Sweep ${id}`,
    type: 'issue-sweep',
    scope: { mode: 'all' },
    cron: '0 2 * * *',
    timezone: 'UTC',
    params: {},
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
    spend_today: { day: '2026-10-27', reported_usd: 0, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    state_word: 'enabled',
    budget_use: null,
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
    cost: { reported_usd: 1, unreported_attempts: 0 },
    ended_at: minutesAgo(400 + n * 1440),
    largest_run_usd: 1,
    largest_run_unread: null,
    ...over,
  }
}

type Routes = Record<string, { status: number; body: unknown }>

function routes(s: Json, firings: Json[], types: Json[]): Routes {
  const id = String(s.schedule_id)
  return {
    '/v1/schedules': {
      status: 200,
      body: { schedules: [s], tenant_id: 'eng', summary: { by_state: { [String(s.state_word)]: 1 }, pending_approvals: 0, oldest_expires_at: null } },
    },
    [`/v1/schedules/${id}`]: { status: 200, body: { schedule: s, firings } },
    [`/v1/schedules/${id}/audit`]: { status: 200, body: { audit: [] } },
    '/v1/schedule-types': { status: 200, body: { types } },
    '/v1/approvals': { status: 200, body: { approvals: [], tenant_id: 'eng' } },
    '/v1/repositories': { status: 200, body: { repositories: [] } },
  }
}

const SWEEP_TYPE: Json = {
  name: 'issue-sweep',
  description: 'Start an issue run for each ready open issue.',
  floor_gate: { merge: 'approve' },
  default_gate: { run: 'auto', plan: 'auto', merge: 'approve' },
  budget_caps: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 4 },
}

async function open(s: Json, firings: Json[], types: Json[] = [SWEEP_TYPE]) {
  const r = routes(s, firings, types)
  serve((m, url) => (m === 'GET' ? r[url.split('?')[0]!] ?? null : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={`schedule=${String(s.schedule_id)}`} spy={vi.fn()} />)
  await screen.findByRole('heading', { name: String(s.name), level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('[data-sec="gate"]')).not.toBeNull(), WAIT)
}

const sec = (name: string) => document.querySelector<HTMLElement>(`aside.as-sheet [data-sec="${name}"]`)!
const row = (fid: string) => document.querySelector<HTMLElement>(`aside.as-sheet tr[data-firing="${fid}"]`)!
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

describe('automate2-02: an empty firings list beside a last firing', () => {
  it('says the firings were not served and names the last one, never "Never fired"', async () => {
    const s = schedule('sch_a', {
      state: 'paused',
      next_run_at: null,
      last_firing: { firing_id: 'sch_a:1', slot: minutesAgo(320), outcome: 'failed', ended_at: minutesAgo(300) },
    })
    await open(s, [])
    const f = sec('firings')
    // MUTATION: drop the `s.last_firing !== null` branch in Firings and this
    // reads "Never fired" over a schedule that failed 5h ago.
    expect(visible(f)).not.toContain('Never fired')
    expect(visible(f)).toContain('not served')
    expect(visible(f)).toContain('failed')
    expect(visible(f)).toContain('5h ago')
  })

  it('still says Never fired when there is no last firing', async () => {
    await open(schedule('sch_a', { last_firing: null }), [])
    expect(visible(sec('firings'))).toContain('Never fired')
  })
})

describe('automate2-03: a skipped firing in words', () => {
  it('names an OVERLAP skip in words and a codeless one as plain skipped', async () => {
    await open(schedule('sch_a'), [
      firing(1, { state: 'skipped', outcome: 'skipped', skip: { code: 'OVERLAP' } }),
      firing(2, { state: 'skipped', outcome: 'skipped', skip: {} }),
      firing(3, { state: 'skipped', outcome: 'skipped', skip: { code: 'BUDGET_EXHAUSTED' } }),
    ])
    // MUTATION: put back `skipped: ${f.skip.code}` and these read
    // `skipped: OVERLAP` and `skipped: undefined`.
    expect(visible(row('sch_a:1'))).toContain('skipped: the last firing was still running')
    expect(visible(row('sch_a:1'))).not.toContain('OVERLAP')
    expect(visible(row('sch_a:2'))).not.toContain('undefined')
    expect(visible(row('sch_a:2'))).toContain('skipped')
    expect(visible(row('sch_a:3'))).toContain('skipped: the day budget was spent')
  })

  it('draws the skip words whole, never in a one-line ellipsis cut', async () => {
    await open(schedule('sch_a'), [firing(1, { state: 'skipped', outcome: 'skipped', skip: { code: 'OVERLAP' } })])
    // The review read 'skipped: the la…' at 1440: `.sc-cut` is nowrap with an
    // ellipsis, and jsdom's textContent reads the whole words either way, so
    // this pins the class on the element that holds the words.
    // MUTATION: put `sc-cut` back on the outcome span in FiringOutcome and
    // the span holding the words carries it.
    const cell = row('sch_a:1')
    const words = Array.from(cell.querySelectorAll('span')).find((el) =>
      (el.textContent ?? '').startsWith('skipped: the last firing was still running'),
    )
    expect(words).toBeDefined()
    expect(words!.closest('.sc-cut')).toBeNull()
  })
})

describe('automate2-19: lateness in duration words', () => {
  it('reads 300 minutes late as 5 hours late', async () => {
    await open(schedule('sch_a'), [firing(1, { slot: minutesAgo(2000), fired_at: minutesAgo(1700) })])
    // MUTATION: put back `${late} min late` and this reads `300 min late`.
    expect(visible(row('sch_a:1'))).toContain('5 hours late')
    expect(visible(row('sch_a:1'))).not.toContain('300 min')
  })
})

describe('automate2-18: a budget field left to the type', () => {
  it('reads the type’s default, never "— not set"', async () => {
    await open(schedule('sch_a', { budget: { per_run_usd: 0.5 } }), [firing(1)], [
      { ...SWEEP_TYPE, budget_caps: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 4 } },
    ])
    const b = sec('spend')
    // MUTATION: put back '— not set' for an unset field and these read it.
    await waitFor(() => expect(kv(b, 'Day budget')).toContain('(the type’s default)'), WAIT)
    expect(kv(b, 'Day budget')).toContain('$120.00 a day')
    expect(kv(b, 'Live runs')).toBe('at most 4 at once (the type’s default)')
    expect(visible(b)).not.toContain('not set')
  })
})

describe('automate2-10: a type that never merges', () => {
  it('says only that it does not merge, with no advice to edit the gate', async () => {
    const s = schedule('sch_a', { type: 'cost-report', name: 'Weekly cost report', gate: { run: 'auto' } })
    await open(s, [firing(1)], [{ name: 'cost-report', description: 'A cost report.', floor_gate: { merge: 'off' }, default_gate: { merge: 'off' } }])
    const note = () => document.querySelector<HTMLElement>('[data-testid="sc-merge-switch"]')!
    await waitFor(() => expect(visible(note())).toContain('never merges'), WAIT)
    // MUTATION: drop the `floor === 'off'` branch and this still tells the
    // reader to edit the gate to approve each merge.
    expect(visible(note())).toContain('This schedule does not merge.')
    expect(visible(note())).not.toContain('Edit its gate')
  })

  it('keeps the edit-the-gate route for a type that may merge', async () => {
    await open(schedule('sch_a', { gate: { run: 'auto' } }), [firing(1)])
    const note = () => document.querySelector<HTMLElement>('[data-testid="sc-merge-switch"]')!
    await waitFor(() => expect(visible(note())).toContain('Edit its gate'), WAIT)
  })
})
