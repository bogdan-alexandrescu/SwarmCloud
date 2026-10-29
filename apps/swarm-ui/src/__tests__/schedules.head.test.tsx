// ONE SCHEDULE'S HEAD, NOW THE SIDE SHEET'S (visual QA VQ-029, part of
// #2391; rewritten for frame A2, lane AUTO-A2-TESTS, part of #2417).
//
// WHAT EACH CASE HOLDS:
//   * the state is the sheet's state word beside the title, with the type's
//     risk tier, never a "State" label in a grid of facts;
//   * what the old head's one meta line said -- the type, the scope, the
//     cadence and the owner -- is each said once, in the sheet's sections;
//   * what the old head's four figures said is in the section it belongs to:
//     the next firing under Cadence, Spend today against its day budget under
//     Spend, and the approvals waiting under the gate;
//   * a figure the server did not serve is a dash with its reason, never 0.
//
// RETIRED with frame A2 (pr-comment.md): the four-across figure strip and its
// phone grid, and the phone's More/Less fold of the head's actions: the sheet
// draws neither.
//
// The bodies are `routes/schedules.py::schedule_to_api`'s shapes.
// Placeholders only; nothing is token-shaped.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import { holdScheduleClock, minutesAgo } from './scheduleclock'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>

function schedule(over: Json = {}): Json {
  return {
    schedule_id: 'sch_a', tenant_id: 'eng', name: 'Weekday issue sweep', type: 'issue-sweep',
    scope: { mode: 'all' }, cron: '0 9 * * 1-5', timezone: 'Europe/London',
    params: {}, gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'enabled', pause: null, owner: 'operator@example.com', created_by: 'operator@example.com',
    created_at: minutesAgo(10_000), next_run_at: minutesAgo(-60),
    last_firing: { firing_id: 'sch_a:1', slot: minutesAgo(120), outcome: 'partial', ended_at: minutesAgo(90) },
    consecutive_failures: 0, revision: 2, words: 'at 09:00, Monday through Friday', tier: 'R2',
    spend_today: { day: '2026-10-26', reported_usd: 41.2, unreported_attempts: 0, coverage: 'complete' }, pending_approvals: 1,
    budget_use: {
      day: '2026-10-26', per_day_usd: 120, per_run_usd: 15, reported_usd: 41.2, unreported_attempts: 0, unreported_usd: 0,
      live_firings: 1, live_items: 2, live_usd: 30, free_usd: 48.8,
    },
    ...over,
  }
}

async function mount(s: Json) {
  serve((m, url) => {
    if (m !== 'GET') return null
    if (url.split('?')[0] === '/v1/schedules/sch_a') return { status: 200, body: { schedule: s, firings: [] } }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start="schedule=sch_a" spy={vi.fn()} />)
  await screen.findByRole('heading', { name: String(s.name), level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('aside.as-sheet [data-sec="gate"]')).not.toBeNull(), WAIT)
  return utils
}

const sheet = () => document.querySelector<HTMLElement>('aside.as-sheet')!
const sec = (name: string) => sheet().querySelector<HTMLElement>(`[data-sec="${name}"]`)!
/** A `dl.as-kv`'s value for a name, inside a section. */
function kv(root: HTMLElement, name: string): string {
  const dt = [...root.querySelectorAll('dt')].find((d) => visible(d) === name)
  expect(dt, `no ${name}`).toBeTruthy()
  return visible(dt!.nextElementSibling)
}

// One instant for the fixture and the page: "in 1h" is not a race (CLOCK-ONCE).
beforeEach(() => holdScheduleClock())

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("a schedule's head, on the sheet (VQ-029)", () => {
  it('says the state word and the risk tier beside the title, one meta line, and no grid of labelled facts', async () => {
    await mount(schedule({ state_word: 'active' }))
    const meta = screen.getByTestId('as-meta')
    // MUTATION: the "State" fact back in a grid and there is no state word in the head.
    expect(visible(meta.querySelector('.as-word'))).toBe('active')
    expect(visible(meta)).toContain('issue-sweep · R2')
    expect(sheet().querySelectorAll('[data-testid="as-meta"]')).toHaveLength(1)
    // What the old meta line said, each said once in its section.
    expect(kv(sec('what'), 'Target')).toBe('every registered repository')
    expect(kv(sec('what'), 'Owner')).toContain('operator@example.com')
    const cadence = visible(sec('cadence').querySelector('h3'))
    for (const part of ['at 09:00, Monday through Friday', '0 9 * * 1-5', 'Europe/London']) expect(cadence).toContain(part)
    expect([...document.querySelectorAll('dt')].map((d) => d.textContent)).not.toContain('State')
    expect(document.querySelector('.sc-facts.sc-head')).toBeNull()
  })

  it('says the next firing, Spend today of its budget and the approvals waiting, each in its section', async () => {
    await mount(schedule({ state_word: 'active' }))
    // MUTATION: drop the next firing when the preview is not read, and Cadence says nothing about when.
    await waitFor(() => expect(visible(screen.getByTestId('as-next'))).toContain('in 1h'), WAIT)
    // MUTATION: drop the budget and Spend today reads with nothing to measure it against.
    expect(sec('spend').querySelector('[data-testid="sc-burn"]')).not.toBeNull()
    expect(kv(sec('spend'), 'Day budget')).toBe('$120.00 a day. When it is spent, the next firing is skipped.')
    // The old "Live runs 2 of 8": the live runs are the bar's reserved part, the ceiling beside it.
    expect(visible(sec('spend').querySelector('.sc-burn-key'))).toContain('2 live runs reserved at $15.00 each: $30.00')
    expect(kv(sec('spend'), 'Live runs')).toBe('at most 8 at once')
    expect(kv(sec('gate'), 'Waiting')).toBe('1 approval waiting · Open Approvals')
  })

  it('says why a figure is missing, never 0', async () => {
    await mount(schedule({ last_firing: null, budget_use: null, state: 'paused', state_word: 'paused', next_run_at: null, pending_approvals: 0, budget: {} }))
    expect(visible(screen.getByTestId('as-next'))).toBe('— paused, so no next firing')
    expect(visible(sec('firings'))).toContain('Never fired')
    // MUTATION: `?? 0` on the budget and a schedule with none reads as a $0.00 budget.
    const absent = visible(screen.getByTestId('sc-burn-absent'))
    expect(absent).toContain('— no day bar')
    expect(absent).toContain('$41.20')
    // The gate rows' words since QA2-FIX-21 (automate2-18): this type serves no caps.
    expect(kv(sec('spend'), 'Day budget')).toBe('— the type’s default, which the catalogue did not say')
    expect(kv(sec('spend'), 'Live runs')).toBe('— the type’s default, which the catalogue did not say')
    expect(kv(sec('gate'), 'Waiting')).toBe('nothing waits on a person')
    expect(visible(sheet())).not.toContain('$0.00')
    expect(visible(screen.getByTestId('as-meta').querySelector('.as-word'))).toBe('paused')
  })
})
