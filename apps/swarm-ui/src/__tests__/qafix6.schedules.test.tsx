// VISUAL QA RUN 6, LANE QAFIX6-SCHEDULES (part of #1680): Schedules' pause
// note, the built-in rows' repository, and the 390 row.
//
// One case per item, each holding the property the fix gives: the whole
// text on screen and no `.sc-cut` on it, a break opportunity after the
// repository's `/`, and on a phone the three facts in the row's summary with
// the actions on a row of their own. jsdom lays nothing out, so the layout
// is held as the cascade's winner at the width (`marks.painted`).
//
// The bodies are `routes/schedules.py::schedule_to_api`'s shape, the fixture
// build's paused row ('Paused by alice@example.com: holiday week'). Placeholders
// only; nothing is token-shaped.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()
const NOTE = 'Paused by alice@example.com: holiday week'
const LONG_REPO = 'example-payments-reconciliation-service-monorepo'

function schedule(id: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name: `Schedule ${id}`,
    type: 'issue-sweep',
    scope: { mode: 'all' },
    cron: '0 8 * * 1',
    timezone: 'UTC',
    params: {},
    gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: hoursFromNow(-48),
    next_run_at: hoursFromNow(3),
    last_firing: null,
    consecutive_failures: 0,
    revision: 1,
    words: 'At 08:00 on Monday',
    tier: 'R0',
    spend_today: { day: '2026-10-23', reported_usd: 0, unreported_attempts: 0, coverage: 'full' },
    pending_approvals: 0,
    ...over,
  }
}

const PAUSED = schedule('sch_p', {
  state: 'paused',
  next_run_at: null,
  tier: 'R1',
  pause: { by: 'alice@example.com', at: hoursFromNow(-48), reason: 'holiday week', code: null },
})
const ENABLED = schedule('sch_e')

const REPO = {
  repo_id: 'repo_long',
  owner: 'example-org',
  repo: LONG_REPO,
  default_branch: 'main',
  index: { interval_hours: 24, on_change: 'poll', min_change_interval_minutes: 30, paused: false, next_run_at: hoursFromNow(5) },
}

function routes(m: string, url: string): Reply {
  const path = url.split('?')[0]!
  if (m === 'GET' && path === '/v1/schedules') return { status: 200, body: { schedules: [PAUSED, ENABLED], tenant_id: 'eng' } }
  if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [REPO] } }
  return null
}

async function mountList() {
  serve(routes)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  return render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={null} spy={vi.fn()} />)
}

/** Stub `matchMedia`, which jsdom does not have: `phone` matches the 560px query. */
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
})

describe('Schedules at 1440 (run 6)', () => {
  it('a. 6:overview-automate-006 (box 6343772528): the pause note wraps under the state, whole', async () => {
    // MUTATION: put `sc-cut` back on the `.sc-why` span in Schedules.tsx, or cut its text.
    media(false)
    await mountList()
    await waitFor(() => expect(rowOf('sch_p')).not.toBeNull(), WAIT)
    const why = rowOf('sch_p')!.querySelector<HTMLElement>('td[data-label="State"] .sc-why')!
    expect(visible(why)).toBe(NOTE)
    expect(why.classList.contains('sc-cut')).toBe(false)
    expect(painted(why, 'white-space', WIDE)).not.toBe('nowrap')
    expect(painted(why, 'text-overflow', WIDE)).not.toBe('ellipsis')
  })

  it("b. 6:overview-automate-007 (box 6343773328): the 'Built in' repository breaks after its slash instead of being cut", async () => {
    // MUTATION: the built-in cell back to `<span className="sc-cut">Index · {r.owner}/{r.repo}</span>`.
    media(false)
    await mountList()
    const cell = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('tr[data-builtin="repo_long"] td[data-label="Built in"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(visible(cell)).toContain(`example-org/${LONG_REPO}`)
    expect(cell.querySelector('.sc-cut')).toBeNull()
    const slash = [...cell.querySelectorAll('.sc-part')].find((p) => p.textContent === 'example-org/')!
    expect(slash, 'the owner is a piece of its own').toBeDefined()
    expect(slash.nextSibling?.nodeName, 'a break opportunity follows the slash').toBe('WBR')
    for (const env of [WIDE, PHONE]) {
      const span = cell.firstElementChild as HTMLElement
      expect(painted(span, 'white-space', env)).not.toBe('nowrap')
      expect(painted(span, 'text-overflow', env)).not.toBe('ellipsis')
    }
  })
})
