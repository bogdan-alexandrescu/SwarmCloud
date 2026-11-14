// AUTOMATE AS CARDS: the Schedules landing ported from the picked cards
// mock-up (the owner's standing answer, 2026-11-12: build the recommended one).
//
// WHAT EACH CASE HOLDS:
//   * each schedule is a card that leads with its next firing, carries its
//     state word only where it asks for something (waiting on approval,
//     paused by its tripwire, failing), its spend today as one bar whose full
//     width is the per-day tripwire, target and cadence, the last outcome and
//     one Manage;
//   * above the cards, the answer-first row (how many need you, the next
//     firing anywhere, spend today) and the approvals waiting, oldest first,
//     each with its expiry and Review, which opens the item's own page;
//   * an unreadable read is a dash with its reason, never 0 or "nothing";
//   * no schedules, not served, and nothing waiting are their own states;
//   * at 390 nothing is laid out wider than the screen.
//
// The bodies are `routes/schedules.py::list_schedules`'s and
// `routes/approvals.py::list_approvals`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { serve } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }
const DESK: CascadeEnv = { width: 1440 }

type Json = Record<string, unknown>

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

function schedule(id: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name: `Schedule ${id}`,
    type: 'issue-sweep',
    scope: { mode: 'repos', repo_ids: ['repo_a'] },
    cron: '0 2 * * *',
    timezone: 'UTC',
    params: {},
    gate: { merge: 'approve', approvers: 'members' },
    budget: { per_run_usd: 5, per_day_usd: 40, max_concurrent: 1 },
    policy: { overlap: 'skip', catch_up: 'skip', dry_run: false, max_consecutive_failures: 3 },
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: hoursFromNow(-48),
    next_run_at: hoursFromNow(5),
    last_firing: { firing_id: `${id}:1`, slot: hoursFromNow(-7), outcome: 'succeeded', ended_at: hoursFromNow(-6) },
    consecutive_failures: 0,
    revision: 1,
    words: 'every day at 02:00',
    tier: 'R2',
    spend_today: { day: '2026-11-12', reported_usd: 10, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    state_word: 'active',
    ...over,
  }
}

const EVERY_WORD: Json[] = [
  schedule('sch_active', { name: 'Active one', next_run_at: hoursFromNow(2) }),
  schedule('sch_paused', { name: 'Paused one', state: 'paused', next_run_at: null, pause: { by: 'a@example.com', reason: 'holiday week' }, state_word: 'paused' }),
  schedule('sch_trip', {
    name: 'Tripped one',
    state: 'auto_paused',
    next_run_at: null,
    spend_today: { day: '2026-11-12', reported_usd: 52, unreported_attempts: 0, coverage: 'complete' },
    pause: { by: 'tick', code: 'DAY_OVER_BUDGET', reason: 'the day reached its tripwire', at: hoursFromNow(-2) },
    state_word: 'paused_by_tripwire',
  }),
  schedule('sch_wait', { name: 'Waiting one', pending_approvals: 1, state_word: 'waiting_on_approval' }),
  schedule('sch_fail', {
    name: 'Failing one',
    consecutive_failures: 2,
    last_firing: { firing_id: 'f', slot: hoursFromNow(-1), outcome: 'failed', ended_at: hoursFromNow(-1) },
    state_word: 'failing',
  }),
  schedule('sch_never', { name: 'Never one', last_firing: null, next_run_at: hoursFromNow(80), state_word: 'never_fired' }),
]

const BY_STATE = { active: 1, paused: 1, paused_by_tripwire: 1, waiting_on_approval: 1, failing: 1, never_fired: 1 }

function item(n: number, over: Json = {}): Json {
  return {
    approval_id: `apr_${String(n).repeat(4)}`,
    kind: 'merge',
    subject: { schedule_id: 'sch_wait' },
    summary: `Merge PR #${100 + n}`,
    state: 'pending',
    requested_at: hoursFromNow(-n),
    expires_at: hoursFromNow(70 - n),
    ...over,
  }
}

/** Served newest first, so the page has to sort them oldest first itself. */
const INBOX = [item(1), item(3, { kind: 'plan', summary: 'Plan for issue #7', subject: { run_id: 'run_by_hand' } }), item(2)]

type Routes = Record<string, { status: number; body: unknown }>

function routes(over: Partial<Routes> = {}): Routes {
  return {
    '/v1/schedules': {
      status: 200,
      body: { schedules: EVERY_WORD, summary: { by_state: BY_STATE, pending_approvals: 3, oldest_expires_at: hoursFromNow(67), unread: null }, tenant_id: 'eng' },
    },
    '/v1/approvals': { status: 200, body: { approvals: INBOX, tenant_id: 'eng' } },
    '/v1/repositories': { status: 200, body: { repositories: [] } },
    ...over,
  } as Routes
}

async function mount(r: Routes) {
  serve((m, url) => (m === 'GET' ? r[url.split('?')[0]!] ?? null : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={null} spy={go} />)
  await screen.findByRole('heading', { name: 'Schedules', level: 1 }, WAIT)
  return { go }
}

/** The words a reader sees, element by element: the (i)'s own copy left out. */
function said(el: Element | null): string {
  if (el === null) return ''
  const clone = el.cloneNode(true) as HTMLElement
  for (const tip of clone.querySelectorAll('.c-tip-host')) tip.remove()
  const parts: string[] = []
  const walk = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT)
  for (let n = walk.nextNode(); n !== null; n = walk.nextNode()) parts.push(n.textContent ?? '')
  return parts.join(' ').replace(/\s+/g, ' ').trim()
}

const cardOf = (id: string) => document.querySelector<HTMLElement>(`section.acd-card[data-schedule="${id}"]`)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the landing as cards', () => {
  it('draws one card per schedule, leading with its next firing, the ones that ask first', async () => {
    await mount(routes())
    await waitFor(() => expect(cardOf('sch_active')).not.toBeNull(), WAIT)
    const order = [...document.querySelectorAll<HTMLElement>('section.acd-card')].map((c) => c.dataset.schedule)
    expect(order.slice(0, 3).sort()).toEqual(['sch_fail', 'sch_trip', 'sch_wait'])
    const card = cardOf('sch_active')!
    expect(card.querySelector('h3')!.textContent).toBe('Active one')
    expect(said(card.querySelector('[data-testid="acd-next"]'))).toMatch(/^Next firing in 2h/)
    expect(said(cardOf('sch_trip')!.querySelector('[data-testid="acd-next"]'))).toContain('— paused by its tripwire, so no next firing')
    expect(said(card.querySelector('.acd-line'))).toContain('every day at 02:00')
    expect(said(card.querySelector('.acd-card-f'))).toMatch(/succeeded/)
    expect(said(cardOf('sch_never')!.querySelector('.acd-card-f'))).toContain('never fired')
    const manage = within(card).getByRole('link', { name: 'Manage' })
    expect(manage.getAttribute('href')).toBe('/automate/schedules/sch_active')
    // No table is left on the landing.
    expect(document.querySelector('table')).toBeNull()
  })

  it('shows a state word only on the schedules that ask for something', async () => {
    await mount(routes())
    await waitFor(() => expect(cardOf('sch_never')).not.toBeNull(), WAIT)
    const word = (id: string) => cardOf(id)!.querySelector('[data-testid="acd-word"]')
    expect(said(word('sch_wait'))).toBe('Waiting on approval')
    expect(said(word('sch_trip'))).toBe('Paused by its tripwire')
    expect(said(word('sch_fail'))).toBe('Failing · 2 in a row')
    for (const id of ['sch_active', 'sch_paused', 'sch_never']) {
      expect(word(id), id).toBeNull()
      expect(cardOf(id)!.querySelector('.acd-word'), id).toBeNull()
    }
    // An active schedule says nothing about being active anywhere on its card.
    const active = cardOf('sch_active')!.cloneNode(true) as HTMLElement
    active.querySelector('h3')!.remove()
    expect(said(active)).not.toMatch(/\bactive\b/i)
    // The tint rides with the word, never alone.
    expect(cardOf('sch_trip')!.className).toContain('is-warn')
    expect(cardOf('sch_active')!.className).toBe('acd-card')
  })

  it("draws today's spend as one bar whose full width is the per-day tripwire", async () => {
    await mount(routes())
    await waitFor(() => expect(cardOf('sch_trip')).not.toBeNull(), WAIT)
    const bar = cardOf('sch_active')!.querySelector<HTMLElement>('[role="meter"]')!
    expect(bar.getAttribute('aria-valuemax')).toBe('40')
    expect(bar.getAttribute('aria-valuenow')).toBe('10')
    expect(bar.querySelector('i')!.style.width).toBe('25%')
    expect(said(cardOf('sch_active')!.querySelector('.acd-spend-t'))).toBe('$10.00 of $40.00 tripwire today')
    // Past the tripwire the bar is full, never wider than its track.
    const over = cardOf('sch_trip')!.querySelector<HTMLElement>('[role="meter"]')!
    expect(over.querySelector('i')!.style.width).toBe('100%')
    expect(over.className).toContain('is-bad')
    expect(painted(over, 'overflow', DESK)).toBe('hidden')
  })

  it('answers first: how many need you, the next firing anywhere, spend today', async () => {
    await mount(routes())
    await waitFor(() => expect(screen.getByTestId('acd-needs')).toBeTruthy(), WAIT)
    expect(said(screen.getByTestId('acd-needs'))).toBe('Needs you 3 of 6 schedules 3 approvals · 1 tripwire · 1 failing')
    expect(said(screen.getByTestId('acd-next-any'))).toMatch(/^Next firing in 2h Active one · /)
    expect(said(screen.getByTestId('acd-spend-today'))).toBe('Spend today $102.00 of $240.00 across every tripwire')
  })

  it('lists the approvals waiting, oldest first, each with its expiry and Review', async () => {
    await mount(routes())
    const panel = await screen.findByTestId('acd-waiting', undefined, WAIT)
    await waitFor(() => expect(panel.querySelectorAll('li[data-approval]').length).toBe(3), WAIT)
    const rows = [...panel.querySelectorAll<HTMLElement>('li[data-approval]')]
    expect(rows.map((r) => r.dataset.approval)).toEqual(['apr_3333', 'apr_2222', 'apr_1111'])
    expect(said(rows[0]!)).toContain('Expires in 3d')
    expect(said(rows[0]!)).toContain('by hand')
    expect(said(rows[1]!)).toContain('Waiting one')
    expect(within(rows[1]!).getByRole('link', { name: 'Review' }).getAttribute('href')).toBe('/automate/approvals/apr_2222')
    // Approve and deny stay on the item's own page, behind its approvers rule.
    expect(within(panel).queryByRole('button', { name: /approve|deny|reject/i })).toBeNull()
  })
})

describe('the empty and honest states', () => {
  it('draws a dash with its reason when the inbox could not be read, never 0 or nothing waiting', async () => {
    await mount(
      routes({
        '/v1/schedules': {
          status: 200,
          body: { schedules: EVERY_WORD, summary: { by_state: BY_STATE, pending_approvals: null, oldest_expires_at: null, unread: 'the approvals inbox timed out' }, tenant_id: 'eng' },
        },
        '/v1/approvals': { status: 503, body: { code: 'unavailable', message: 'unavailable' } },
      }),
    )
    const panel = await screen.findByTestId('acd-waiting', undefined, WAIT)
    await waitFor(() => expect(within(panel).getByTestId('acd-unread')).toBeTruthy(), WAIT)
    expect(said(within(panel).getByTestId('acd-unread'))).toBe('— the approvals inbox timed out')
    expect(within(panel).queryByTestId('acd-nothing-waiting')).toBeNull()
    expect(said(screen.getByTestId('acd-needs'))).not.toMatch(/\b0 approvals\b/)
    // Visual QA: the tile's line still names the approvals, as not read,
    // rather than leaving them out as if none waited.
    // MUTATION: drop the unread approvals from the line and this fails.
    expect(said(screen.getByTestId('acd-needs'))).toContain('approvals not read')
  })

  it("draws a dash with its reason when a schedule's spend is not served", async () => {
    const { spend_today: _drop, ...bare } = schedule('sch_bare', { name: 'Bare one' })
    await mount(routes({ '/v1/schedules': { status: 200, body: { schedules: [bare], summary: null, tenant_id: 'eng' } } }))
    await waitFor(() => expect(cardOf('sch_bare')).not.toBeNull(), WAIT)
    expect(said(cardOf('sch_bare')!.querySelector('[data-testid="acd-spend"]'))).toBe('— not served by this API')
    expect(cardOf('sch_bare')!.querySelector('[role="meter"]')).toBeNull()
    expect(said(screen.getByTestId('acd-spend-today'))).toContain('—')
    expect(said(screen.getByTestId('acd-spend-today'))).not.toContain('$0.00')
  })

  it('says nothing waits when the inbox was read and is empty', async () => {
    await mount(routes({ '/v1/approvals': { status: 200, body: { approvals: [], tenant_id: 'eng' } }, '/v1/schedules': { status: 200, body: { schedules: EVERY_WORD, summary: null, tenant_id: 'eng' } } }))
    const nothing = await screen.findByTestId('acd-nothing-waiting', undefined, WAIT)
    expect(said(nothing)).toBe('Nothing waiting')
  })

  it('draws no schedules as its own state with New schedule', async () => {
    await mount(
      routes({
        '/v1/schedules': { status: 200, body: { schedules: [], summary: { by_state: {}, pending_approvals: 0, oldest_expires_at: null, unread: null }, tenant_id: 'eng' } },
        '/v1/approvals': { status: 200, body: { approvals: [], tenant_id: 'eng' } },
      }),
    )
    const none = await screen.findByTestId('acd-none', undefined, WAIT)
    expect(within(none).getByRole('heading', { name: 'No schedules' })).toBeTruthy()
    expect(within(none).getByRole('link', { name: 'New schedule' }).getAttribute('href')).toBe('/automate/schedules/new')
    expect(document.querySelector('section.acd-card')).toBeNull()
  })

  it('says what the empty state\u2019s (i) is about: Start from a type, as the mock-up draws it (visual QA)', async () => {
    await mount(
      routes({
        '/v1/schedules': { status: 200, body: { schedules: [], summary: { by_state: {}, pending_approvals: 0, oldest_expires_at: null, unread: null }, tenant_id: 'eng' } },
        '/v1/approvals': { status: 200, body: { approvals: [], tenant_id: 'eng' } },
      }),
    )
    const none = await screen.findByTestId('acd-none', undefined, WAIT)
    // A lone (i) under the heading said nothing until it was opened.
    const empty = none.querySelector('.c-tip-host')!.parentElement!
    expect(said(empty)).toContain('Start from a type')
  })

  it('draws a 404 as not served by this deployment, counting nothing', async () => {
    await mount(routes({ '/v1/schedules': { status: 404, body: { code: 'not_found', message: 'Not Found' } } }))
    const ns = await screen.findByTestId('acd-not-served', undefined, WAIT)
    expect(said(ns)).toContain('Automate is not served here')
    expect(said(ns)).toContain('GET /v1/schedules answered 404')
    expect(document.body.textContent).not.toMatch(/\b0 of 0\b|\$0\.00/)
  })
})

describe('the phone at 390', () => {
  it('lays every part out in one column, nothing wider than the screen', async () => {
    await mount(routes())
    const panel = await screen.findByTestId('acd-waiting', undefined, WAIT)
    await waitFor(() => expect(panel.querySelectorAll('li[data-approval]').length).toBe(3), WAIT)
    const grid = document.querySelector<HTMLElement>('.acd-grid')!
    expect(painted(grid, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(grid, 'grid-template-columns', DESK)).toBe('repeat(auto-fill, minmax(300px, 1fr))')
    expect(painted(document.querySelector<HTMLElement>('.acd-answer')!, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    // The desktop's fixed 180px and 170px columns are gone on the phone.
    expect(painted(panel.querySelector<HTMLElement>('li[data-approval]')!, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr) auto')
    for (const el of [grid, ...document.querySelectorAll<HTMLElement>('.acd-card, .acd-panel, .acd-tile')]) {
      expect(painted(el, 'min-width', PHONE), el.className).toBe('0')
    }
  })
})
