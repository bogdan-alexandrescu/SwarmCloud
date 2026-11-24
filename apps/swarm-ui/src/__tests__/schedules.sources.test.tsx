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
import { fireEvent, render, screen, within } from '@testing-library/react'
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

const NAME = (id: string) => `Schedule ${id}`
/** The cards drawn, by schedule id. */
const listed = () => screen.queryAllByTestId('src-card').map((e) => e.getAttribute('data-source') ?? '')
const card = (id: string) => screen.getByRole('region', { name: NAME(id) })
const cardDrawn = async (id: string) => screen.findByRole('region', { name: NAME(id) }, WAIT)
const GATE_ID = /^src-gate-/
const gateKeys = (id: string) => within(card(id)).queryAllByTestId(GATE_ID).map((e) => (e.getAttribute('data-testid') ?? '').replace(GATE_ID, ''))
const gate = (id: string, key: string) => visible(within(card(id)).getByTestId(`src-gate-${key}`))
const seven = (id: string) => visible(within(card(id)).getByTestId('src-7d-n'))
const ROWS_OK = (): Served => ({ schedules: ok({ schedules: ROWS, tenant_id: 'eng' }), types: ok({ types: TYPES }) })

beforeEach(() => holdScheduleClock())

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the sources that propose (?proposes=1)', () => {
  it('lists only the schedules whose type the catalogue says proposes', async () => {
    await mount(ROWS_OK())
    await cardDrawn('sch_sweep')
    // MUTATION: the full list drawn, or the narrowing guessed from the type's name.
    expect(listed().sort((a, b) => a.localeCompare(b))).toEqual(['sch_quiet', 'sch_sweep', 'sch_watch'])
    expect(screen.queryByRole('region', { name: NAME('sch_cost') })).toBeNull()
  })

  it('draws a gate field only where the read serves it', async () => {
    await mount(ROWS_OK())
    await cardDrawn('sch_sweep')
    // MUTATION: a gate invented from the type's default, or drawn as a dash.
    expect(gateKeys('sch_sweep')).toEqual(['plan', 'merge'])
    expect(gateKeys('sch_quiet')).toEqual(['plan'])
    expect(gateKeys('sch_watch')).toEqual(['run', 'plan', 'merge', 'approvers', 'approval_ttl_hours'])
    expect(gate('sch_watch', 'approvers')).toContain('reviewers')
    expect(gate('sch_watch', 'approval_ttl_hours')).toContain('48 h')
    expect(gate('sch_watch', 'merge')).toContain('auto')
  })

  it("draws the seven days' count as served: at least when incomplete, a dash when absent", async () => {
    await mount(ROWS_OK())
    await cardDrawn('sch_sweep')
    // MUTATION: a count recomputed here, or the parts dropped.
    expect(seven('sch_sweep')).toBe('5')
    const parts = visible(within(card('sch_sweep')).getByTestId('src-7d-parts'))
    expect(parts).toMatch(/3 approved/)
    expect(parts).toMatch(/2 merged/)
    expect(parts).not.toMatch(/expired/)
    // MUTATION: an incomplete read shown as the whole figure.
    expect(seven('sch_watch')).toBe('at least 9')
    // MUTATION: a count the read did not carry drawn as 0.
    expect(seven('sch_quiet')).toBe('—')
    expect(within(card('sch_quiet')).queryByTestId('src-7d-parts')).toBeNull()
  })

  it("draws today's spend against the tripwire", async () => {
    await mount(ROWS_OK())
    await cardDrawn('sch_sweep')
    const spend = visible(within(card('sch_sweep')).getByTestId('acd-spend'))
    expect(spend).toContain('$3.10')
    expect(spend).toContain('$10.00')
  })

  it('opens a schedule from its card with a button', async () => {
    const { go } = await mount(ROWS_OK())
    await cardDrawn('sch_sweep')
    fireEvent.click(within(card('sch_sweep')).getByRole('link', { name: 'Manage' }))
    expect(go).toHaveBeenCalledWith('automate/schedules?schedule=sch_sweep')
  })

  it('is empty, in one line and its action, when no schedule proposes', async () => {
    await mount({ schedules: ok({ schedules: [ROWS[2]], tenant_id: 'eng' }), types: ok({ types: TYPES }) })
    const empty = await screen.findByTestId('src-none', {}, WAIT)
    // MUTATION: the full list's empty state, or prose under the heading.
    expect(within(empty).getByRole('heading').textContent).toMatch(/No schedule proposes/)
    expect(within(empty).getByText('New schedule')).toBeTruthy()
    expect(listed()).toEqual([])
  })

  it('says the narrowing is not served when the catalogue carries no proposes', async () => {
    await mount({
      schedules: ok({ schedules: ROWS, tenant_id: 'eng' }),
      types: ok({ types: [type('issue-sweep', undefined), type('cost-report', undefined)] }),
    })
    // MUTATION: every schedule listed as a source, or none listed as a real zero.
    await screen.findByTestId('src-not-served', {}, WAIT)
    expect(listed()).toEqual([])
    expect(screen.queryByTestId('src-none')).toBeNull()
  })

  it('a failed read is a failure, not an empty list', async () => {
    await mount({ schedules: { status: 403, body: { code: 'forbidden', message: 'no' } }, types: ok({ types: TYPES }) })
    await screen.findByRole('heading', { name: /The API refused this request/ }, WAIT)
    expect(screen.queryByTestId('src-none')).toBeNull()
    expect(listed()).toEqual([])
  })

  it('reads before it draws, never an empty list meanwhile', async () => {
    serve(handler(ROWS_OK()))
    const real = globalThis.fetch
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string, init?: RequestInit) => ((input.split('?')[0] ?? '').endsWith('/v1/schedules') ? new Promise<Response>(() => {}) : real(input, init))),
    )
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SchedulesScreen } = await import('../Schedules')
    render(<Routed Screen={SchedulesScreen} section="automate/schedules" start="proposes=1" spy={vi.fn()} />)
    await screen.findByRole('heading', { name: 'Schedules', level: 1 }, WAIT)
    // MUTATION: an empty or failed state drawn before the read lands.
    expect(await screen.findByRole('button', { name: 'Reading' }, WAIT)).toBeTruthy()
    expect(screen.queryByTestId('src-none')).toBeNull()
    expect(screen.queryByTestId('src-not-served')).toBeNull()
    expect(listed()).toEqual([])
  })

  it('keeps the sources, marked stale, when a refresh fails', async () => {
    const served = ROWS_OK()
    await mount(served)
    await cardDrawn('sch_sweep')
    served.schedules = { status: 403, body: { code: 'forbidden', message: 'no' } }
    fireEvent.click(screen.getByRole('button', { name: /refresh$/ }))
    // MUTATION: a failed refresh blanking the list, or drawn as fresh.
    await screen.findByText(/showing older data/, {}, WAIT)
    expect(listed()).toContain('sch_sweep')
  })

  it('the full list and the sources reach each other by buttons', async () => {
    const { go } = await mount(ROWS_OK())
    const nav = await screen.findByRole('navigation', { name: 'Which schedules' }, WAIT)
    const links = within(nav).getAllByRole('link')
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/automate/schedules', '/automate/schedules?proposes=1'])
    expect(links[1]!.getAttribute('aria-current')).toBe('page')
    fireEvent.click(links[0]!)
    expect(go).toHaveBeenCalledWith('automate/schedules')
  })
})
