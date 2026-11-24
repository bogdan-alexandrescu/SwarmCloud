// THE SOURCES THAT PROPOSE (plans-pipeline §2.4, lane U6 PLAN-SOURCES):
// Automate › Schedules narrowed by `?proposes=1` to the schedules whose type
// the server says proposes (`proposes` on `GET /v1/schedule-types`), each
// with its gate as stored, today's spend and `proposals_7d` (§3.10).
//
// What these hold:
//   * only the types the catalogue marks `proposes` are listed;
//   * a gate field is drawn only when the schedule's read serves it;
//   * the seven-day count is the served one, "at least" when incomplete, and a
//     dash (never 0) when the read carries none;
//   * spend is today's against the tripwire;
//   * loading, empty, failed, not served and stale are each their own state.
//
// Placeholders only; nothing is token-shaped.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import { holdScheduleClock, hoursFromNow } from './scheduleclock'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

function row(id: string, type: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name: `Schedule ${id}`,
    type,
    tier: 'R2',
    scope: { mode: 'repos', repo_ids: ['r1'] },
    cron: '0 9 * * 1-5',
    timezone: 'Europe/London',
    words: 'at 09:00, Monday through Friday',
    params: {},
    gate: {},
    budget: { per_run_usd: 2, per_day_usd: 10 },
    policy: {},
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: hoursFromNow(-200),
    next_run_at: hoursFromNow(3),
    last_firing: null,
    revision: 1,
    spend_today: { day: '2026-11-24', reported_usd: 3.1, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    state_word: 'active',
    ...over,
  }
}

const counts = (over: Json = {}): Json => ({
  proposed: 5, approved: 3, auto_approved: 1, rejected: 1, expired: 0, running: 1, merged: 2, window_days: 7, complete: true, ...over,
})

function type(name: string, proposes: boolean | undefined): Json {
  const t: Json = {
    name,
    description: name,
    params_schema: {},
    default_gate: {},
    floor_gate: {},
    min_interval_minutes: 60,
    budget_caps: null,
    scopes: ['repos'],
    available: true,
    disabled_reason: null,
  }
  if (proposes !== undefined) t.proposes = proposes
  return t
}

const TYPES = [type('issue-sweep', true), type('observer', true), type('cost-report', false)]

const ROWS = [
  row('sch_sweep', 'issue-sweep', {
    gate: { plan: 'approve', merge: 'approve' },
    proposals_7d: counts(),
  }),
  row('sch_watch', 'observer', {
    gate: { run: 'auto', plan: 'approve', merge: 'auto', approvers: 'reviewers', approval_ttl_hours: 48 },
    proposals_7d: counts({ proposed: 9, complete: false }),
  }),
  row('sch_cost', 'cost-report', { gate: { run: 'auto' } }),
  row('sch_quiet', 'observer', { gate: { plan: 'approve' } }),
]

interface Served {
  schedules: Reply
  types: Reply
}

function handler(served: Served) {
  return (m: string, url: string): Reply => {
    if (m !== 'GET') return null
    const path = url.split('?')[0]
    if (path === '/v1/tenants/me') {
      return { status: 200, body: { tenant: { tenant_id: 'eng', kind: 'group' }, principal: { email: 'person@example.com', domain: 'example.com', groups: [], is_admin: false } } }
    }
    if (path === '/v1/schedules') return served.schedules
    if (path === '/v1/schedule-types') return served.types
    if (path === '/v1/repositories') return { status: 200, body: { repositories: [] } }
    if (path === '/v1/approvals') return { status: 200, body: { approvals: [] } }
    return null
  }
}

const ok = (body: unknown): Reply => ({ status: 200, body })

async function mount(served: Served) {
  const calls = serve(handler(served))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start="proposes=1" spy={go} />)
  return { ...utils, calls, go }
}

const card = (id: string) => document.querySelector<HTMLElement>(`[data-source="${id}"]`)
const gateKeys = (id: string) => [...(card(id)?.querySelectorAll<HTMLElement>('[data-gate]') ?? [])].map((e) => e.dataset.gate)

beforeEach(() => holdScheduleClock())

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the sources that propose (?proposes=1)', () => {
  it('lists only the schedules whose type the catalogue says proposes', async () => {
    await mount({ schedules: ok({ schedules: ROWS, tenant_id: 'eng' }), types: ok({ types: TYPES }) })
    await waitFor(() => expect(card('sch_sweep')).not.toBeNull(), WAIT)
    // MUTATION: the full list drawn, or the narrowing guessed from the type's name.
    const listed = [...document.querySelectorAll<HTMLElement>('[data-source]')].map((e) => e.dataset.source).sort()
    expect(listed).toEqual(['sch_quiet', 'sch_sweep', 'sch_watch'])
    expect(card('sch_cost')).toBeNull()
  })

  it('draws a gate field only where the read serves it', async () => {
    await mount({ schedules: ok({ schedules: ROWS, tenant_id: 'eng' }), types: ok({ types: TYPES }) })
    await waitFor(() => expect(card('sch_sweep')).not.toBeNull(), WAIT)
    // MUTATION: a gate invented from the type's default, or drawn as a dash.
    expect(gateKeys('sch_sweep')).toEqual(['plan', 'merge'])
    expect(gateKeys('sch_quiet')).toEqual(['plan'])
    expect(gateKeys('sch_watch')).toEqual(['run', 'plan', 'merge', 'approvers', 'approval_ttl_hours'])
    const watch = card('sch_watch')!
    expect(visible(watch.querySelector('[data-gate="approvers"]'))).toContain('reviewers')
    expect(visible(watch.querySelector('[data-gate="approval_ttl_hours"]'))).toContain('48 h')
    expect(visible(watch.querySelector('[data-gate="merge"]'))).toContain('auto')
  })

  it("draws the seven days' count as served: at least when incomplete, a dash when absent", async () => {
    await mount({ schedules: ok({ schedules: ROWS, tenant_id: 'eng' }), types: ok({ types: TYPES }) })
    await waitFor(() => expect(card('sch_sweep')).not.toBeNull(), WAIT)
    const seven = (id: string) => card(id)!.querySelector<HTMLElement>('[data-testid="src-7d"]')!
    // MUTATION: a count recomputed here, or the parts dropped.
    expect(visible(seven('sch_sweep').querySelector('.acd-big'))).toBe('5')
    expect(visible(seven('sch_sweep'))).toMatch(/3 approved/)
    expect(visible(seven('sch_sweep'))).toMatch(/2 merged/)
    // MUTATION: an incomplete read shown as the whole figure.
    expect(visible(seven('sch_watch').querySelector('.acd-big'))).toBe('at least 9')
    // MUTATION: a count the read did not carry drawn as 0.
    expect(visible(seven('sch_quiet').querySelector('.acd-big'))).toBe('—')
    expect(seven('sch_quiet').textContent).not.toMatch(/\b0\b/)
  })

  it("draws today's spend against the tripwire", async () => {
    await mount({ schedules: ok({ schedules: ROWS, tenant_id: 'eng' }), types: ok({ types: TYPES }) })
    await waitFor(() => expect(card('sch_sweep')).not.toBeNull(), WAIT)
    const spend = card('sch_sweep')!.querySelector('[data-testid="acd-spend"]')
    expect(visible(spend)).toContain('$3.10')
    expect(visible(spend)).toContain('$10.00')
  })

  it('opens a schedule from its card with a button', async () => {
    const { go } = await mount({ schedules: ok({ schedules: ROWS, tenant_id: 'eng' }), types: ok({ types: TYPES }) })
    await waitFor(() => expect(card('sch_sweep')).not.toBeNull(), WAIT)
    fireEvent.click(card('sch_sweep')!.querySelector('a.acd-manage')!)
    expect(go).toHaveBeenCalledWith('automate/schedules?schedule=sch_sweep')
  })

  it('is empty, in one line and its action, when no schedule proposes', async () => {
    await mount({ schedules: ok({ schedules: [ROWS[2]], tenant_id: 'eng' }), types: ok({ types: TYPES }) })
    const empty = await screen.findByTestId('src-none', {}, WAIT)
    // MUTATION: the full list's empty state, or prose under the heading.
    expect(empty.querySelector('h3')?.textContent).toMatch(/No schedule proposes/)
    expect(empty.querySelector('a, button')?.textContent).toBe('New schedule')
    expect(document.querySelector('[data-source]')).toBeNull()
  })

  it('says the narrowing is not served when the catalogue carries no proposes', async () => {
    await mount({
      schedules: ok({ schedules: ROWS, tenant_id: 'eng' }),
      types: ok({ types: [type('issue-sweep', undefined), type('cost-report', undefined)] }),
    })
    // MUTATION: every schedule listed as a source, or none listed as a real zero.
    await screen.findByTestId('src-not-served', {}, WAIT)
    expect(document.querySelector('[data-source]')).toBeNull()
    expect(screen.queryByTestId('src-none')).toBeNull()
  })

  it('a failed read is a failure, not an empty list', async () => {
    await mount({ schedules: { status: 500, body: { code: 'internal', message: 'boom' } }, types: ok({ types: TYPES }) })
    await waitFor(() => expect(document.querySelector('[role="alert"]')).not.toBeNull(), { timeout: 15000 })
    expect(screen.queryByTestId('src-none')).toBeNull()
    expect(document.querySelector('[data-source]')).toBeNull()
  }, 20000)

  it('draws skeletons while it reads, never an empty list', async () => {
    serve(handler({ schedules: ok({ schedules: ROWS, tenant_id: 'eng' }), types: ok({ types: TYPES }) }))
    const real = globalThis.fetch
    globalThis.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
      String(input).split('?')[0].endsWith('/v1/schedules') ? new Promise<Response>(() => {}) : real(input, init),
    ) as unknown as typeof fetch
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SchedulesScreen } = await import('../Schedules')
    render(<Routed Screen={SchedulesScreen} section="automate/schedules" start="proposes=1" spy={vi.fn()} />)
    await screen.findByRole('heading', { name: 'Schedules', level: 1 }, WAIT)
    await waitFor(() => expect(document.querySelector('[aria-busy="true"], .c-sk')).not.toBeNull(), WAIT)
    expect(screen.queryByTestId('src-none')).toBeNull()
    expect(document.querySelector('[data-source]')).toBeNull()
  })

  it('keeps the sources, marked stale, when a refresh fails', async () => {
    const served: Served = { schedules: ok({ schedules: ROWS, tenant_id: 'eng' }), types: ok({ types: TYPES }) }
    await mount(served)
    await waitFor(() => expect(card('sch_sweep')).not.toBeNull(), WAIT)
    served.schedules = { status: 403, body: { code: 'forbidden', message: 'no' } }
    const refresh = screen.getAllByRole('button').find((b) => /refresh$/.test(b.getAttribute('aria-label') ?? ''))!
    fireEvent.click(refresh)
    // MUTATION: a failed refresh blanking the list, or drawn as fresh.
    await waitFor(() => expect(document.querySelector('.stale-note')).not.toBeNull(), { timeout: 15000 })
    expect(card('sch_sweep')).not.toBeNull()
  }, 20000)

  it('the full list and the sources reach each other by buttons', async () => {
    const { go } = await mount({ schedules: ok({ schedules: ROWS, tenant_id: 'eng' }), types: ok({ types: TYPES }) })
    const nav = await screen.findByRole('navigation', { name: 'Which schedules' }, WAIT)
    const links = [...nav.querySelectorAll('a')]
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/automate/schedules', '/automate/schedules?proposes=1'])
    expect(links[1]!.getAttribute('aria-current')).toBe('page')
    fireEvent.click(links[0]!)
    expect(go).toHaveBeenCalledWith('automate/schedules')
  })
})
