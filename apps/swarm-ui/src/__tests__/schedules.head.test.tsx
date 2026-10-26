// ONE SCHEDULE'S HEAD IS DETAIL A'S (visual QA VQ-029, part of #2391;
// schedules.html "4 · Schedule detail", variant A).
//
// WHAT EACH CASE HOLDS:
//   * the state is a pill beside the title, with the risk chip, never a
//     "State" label in a grid of facts;
//   * one meta line says the type, the scope, the cadence and the owner;
//   * four figures follow, in the mock-up's order -- Next run, Last run, Live
//     runs, Spend today -- and Spend today carries its budget ("of $120.00");
//     a figure the server did not serve is a dash with its reason, never 0;
//   * the figures are four across on a desktop and two by two on a phone
//     (390), so the History tab no longer starts 650px down;
//   * on a phone every action but Run now folds into More, which opens them;
//     a desktop draws them all and no More. Asked of `painted` (cssgate's
//     cascade), because jsdom applies no `@media` block.
//
// The bodies are `routes/schedules.py::schedule_to_api`'s shapes.
// Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

type Json = Record<string, unknown>

const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString()

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
  return utils
}

const figs = () => [...document.querySelectorAll<HTMLElement>('[data-testid="sc-figs"] > [data-fig]')]
const fig = (name: string) => figs().find((f) => f.dataset.fig === name)!.querySelector('dd')!

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("a schedule's head (VQ-029)", () => {
  it('draws the state pill and the risk chip, one meta line, and no grid of labelled facts', async () => {
    await mount(schedule())
    const pills = screen.getByTestId('sc-pills')
    // MUTATION: the "State" fact back in a grid and there is no pill.
    const pill = pills.querySelector<HTMLElement>('.c-pill')
    expect(pill, 'the state is not a pill').not.toBeNull()
    expect(visible(pill)).toBe('enabled')
    expect(visible(pills)).toContain('R2 · merges need approval')
    const meta = screen.getByTestId('sc-meta')
    for (const part of ['issue-sweep', 'every registered repository', 'at 09:00, Monday through Friday', '0 9 * * 1-5', 'Europe/London', 'owner operator@example.com']) {
      expect(visible(meta)).toContain(part)
    }
    expect([...document.querySelectorAll('dt')].map((d) => d.textContent)).not.toContain('State')
    expect(document.querySelector('.sc-facts.sc-head')).toBeNull()
  })

  it('draws detail A’s four figures, Spend today of its budget', async () => {
    await mount(schedule())
    // MUTATION: drop Last run, or the budget, and the head is the old one.
    expect(figs().map((f) => visible(f.querySelector('dt')))).toEqual(['Next run', 'Last run', 'Live runs', 'Spend today'])
    expect(visible(fig('Next run'))).toBe('in 1h')
    expect(visible(fig('Last run').querySelector('.sc-fig-v'))).toBe('partial')
    expect(visible(fig('Last run').querySelector('.sc-sub'))).toMatch(/ ago$/)
    expect(visible(fig('Live runs').querySelector('.sc-fig-v'))).toBe('2 of 8')
    expect(visible(fig('Live runs').querySelector('.sc-sub'))).toBe('1 waiting for approval')
    expect(visible(fig('Spend today'))).toBe('$41.20 of $120.00')
  })

  it('says why a figure is missing, never 0', async () => {
    await mount(schedule({ last_firing: null, budget_use: null, state: 'paused', next_run_at: null, pending_approvals: 0, budget: {} }))
    expect(visible(fig('Next run'))).toBe('— none while paused')
    expect(visible(fig('Last run'))).toBe('— never fired')
    // MUTATION: `live_items ?? 0` and a schedule whose use was not served reads as idle.
    expect(fig('Live runs').querySelector('.sc-fig-v')).toBeNull()
    expect(visible(fig('Live runs').querySelector('.ctl-em'))).toBe('— not served with this schedule')
    expect(visible(fig('Live runs').querySelector('.sc-sub'))).toBe('none waiting for approval')
    expect(visible(fig('Spend today').querySelector('.sc-fig-v'))).toBe('$41.20')
    expect(visible(fig('Spend today').querySelector('.sc-sub'))).toBe('no daily budget set')
    expect(visible(screen.getByTestId('sc-pills').querySelector('.c-pill'))).toBe('paused')
  })

  it('sets the figures four across on a desktop and two by two on a phone, 12px under the meta line', async () => {
    await mount(schedule())
    const strip = screen.getByTestId('sc-figs')
    // MUTATION: leave the strip on `.sc-facts` and a phone stacks the figures one per row.
    expect(painted(strip, 'grid-template-columns', WIDE)).toBe('repeat(4, minmax(0, 1fr))')
    expect(painted(strip, 'grid-template-columns', PHONE)).toBe('repeat(2, minmax(0, 1fr))')
    expect(painted(strip, 'margin-top', WIDE)).toBe('12px')
    expect(painted(strip, 'margin-top', PHONE)).toBe('12px')
  })

  it('folds every action but Run now into More on a phone, and draws them all on a desktop', async () => {
    await mount(schedule())
    const row = screen.getByTestId('sc-head-row')
    // jsdom applies the base sheet, where a desktop has no More, so More has
    // no accessible name there: it is found by its class and its words.
    const more = row.querySelector<HTMLButtonElement>('button.sc-more')!
    expect(visible(more)).toBe('More')
    const fold = row.querySelector<HTMLElement>('.sc-fold')!
    const edit = within(row).getByRole('button', { name: 'Edit' })
    const run = within(row).getByRole('button', { name: 'Run now' })
    expect(fold.contains(run)).toBe(false)
    expect(within(fold).getAllByRole('button').map((b) => visible(b))).toEqual(['Dry run', 'Pause', 'Delete…'])
    expect(more.getAttribute('aria-controls')?.split(' ')).toEqual([fold.id, edit.id])
    // A desktop: everything in the row, no More.
    expect(painted(more, 'display', WIDE)).toBe('none')
    expect(painted(fold, 'display', WIDE)).toBe('contents')
    expect(painted(edit, 'display', WIDE)).toBe('inline-flex')
    // A phone: Run now and More; the rest folded.
    // MUTATION: drop the 640px block and five 44px buttons wrap Edit to a row of its own.
    expect(more.getAttribute('aria-expanded')).toBe('false')
    expect(painted(more, 'display', PHONE)).toBe('inline-flex')
    expect(painted(fold, 'display', PHONE)).toBe('none')
    expect(painted(edit, 'display', PHONE)).toBe('none')
    expect(painted(run, 'display', PHONE)).toBe('inline-flex')
    fireEvent.click(more)
    expect(more.getAttribute('aria-expanded')).toBe('true')
    expect(painted(fold, 'display', PHONE)).toBe('contents')
    expect(painted(edit, 'display', PHONE)).toBe('inline-flex')
  })
})
