/**
 * VISUAL QA 2026-10-20 (epic #1680), lane QAFIX-CHECKS: Overview's
 * 'Needs a look' checks.
 *
 *   QA-013  The head said "checks 8/8 · 6 found something" over ten cards:
 *           it counted the checks that fired and drew one card per problem,
 *           and never said which number was which.
 *   QA-061  At 390px the THROTTLED card's link, "quota, all tenants ·
 *           admin →", held about half the card and squeezed its title and
 *           body into the rest.
 *   QA-109  One thing had several names: the card counted "failed tasks",
 *           its link said "failed agents", the page it opens is "Agents".
 *
 * Each `it` names the mutation that turns it red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import type { Result } from '../fetch'
import type { ProvidersPage, QuotaState, Stats, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadTasks: vi.fn(),
  loadLeases: vi.fn(),
  loadProviders: vi.fn(),
  loadAccountPool: vi.fn(),
  loadWorkflows: vi.fn(),
  loadStats: vi.fn(),
  loadSpend: vi.fn(),
  loadTask: vi.fn(),
}))
vi.mock('../api', () => ({ ...api, TASK_PAGE_LIMIT: 200 }))

const Overview = await import('../Overview')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}
const empty = { status: 'empty', fetchedAt: Date.now() } as const

const STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: { QUEUED: 0, READY: 0, PARKED: 0, LEASED: 0, DISPATCHED: 0, STARTING: 0, RUNNING: 0, SUCCEEDED: 0, FAILED: 0, DEAD_LETTERED: 0, CANCELLED: 0 },
  dispatch_paused: false,
  limits: {},
  generated_at: new Date().toISOString(),
}

function quota(provider: string, state: string): QuotaState {
  return {
    provider,
    tenant_id: 'eng',
    state,
    updated_at: new Date().toISOString(),
    configured_hard_max: 4,
    adaptive_target: null,
    quota_derived_limit: null,
    requests_remaining: null,
    tokens_remaining: null,
    reset_at: null,
    cooldown_until: null,
    last_429_at: ago(2),
    retry_after_seconds: null,
    success_count: 10,
    rate_limit_count: 3,
    effective_limit: 2,
  } as QuotaState
}

/** Two providers throttled: ONE check (provider quota), TWO cards. */
const TWO_THROTTLED: ProvidersPage = {
  tenant_id: 'eng',
  providers: ['anthropic', 'openai'].map((p) => ({
    provider: p,
    credential_registered: true,
    runner_profiles: [],
    quota: quota(p, 'THROTTLED'),
  })),
  generated_at: new Date().toISOString(),
}

interface Reads {
  tasks?: Result<TaskPage>
  providers?: Result<ProvidersPage>
}

async function mount(reads: Reads): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(empty)
  api.loadTasks.mockResolvedValue(reads.tasks ?? empty)
  api.loadLeases.mockResolvedValue(empty)
  api.loadProviders.mockResolvedValue(reads.providers ?? empty)
  api.loadWorkflows.mockResolvedValue(empty)
  api.loadStats.mockResolvedValue(ok(STATS))
  api.loadAccountPool.mockResolvedValue(empty)
  api.loadSpend.mockResolvedValue(empty)
  api.loadTask.mockResolvedValue(empty)
  const { container } = render(<Overview.OverviewScreen />)
  for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
  return container
}

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

describe('QA-013: the Needs a look head counts the cards it draws', () => {
  it('says how many problems the checks that fired found, and draws that many cards', async () => {
    const el = await mount({ providers: ok(TWO_THROTTLED) })
    const lead = el.querySelector('#ov-needs')!
    const cards = lead.querySelectorAll('a.ov-att')
    expect(cards.length, 'two throttled providers, two cards').toBe(2)
    // MUTATION: put back `· ${found} found something`, which reads "1" over two cards.
    expect(text(lead.querySelector('.ov-cnt'))).toMatch(/^checks (\d+)\/\1 · 1 found 2 problems$/)
  })

  it('says one problem in the singular', async () => {
    const el = await mount({ providers: ok({ ...TWO_THROTTLED, providers: TWO_THROTTLED.providers.slice(0, 1) }) })
    const lead = el.querySelector('#ov-needs')!
    expect(lead.querySelectorAll('a.ov-att').length).toBe(1)
    // MUTATION: drop the `problem`/`problems` choice and always say "problems".
    expect(text(lead.querySelector('.ov-cnt'))).toMatch(/^checks (\d+)\/\1 · 1 found 1 problem$/)
  })
})

describe('QA-061: a check card’s link drops under its text on a phone', () => {
  it('wraps the card and gives the link a line of its own at 390px, and keeps it beside the text at 1440', async () => {
    await mount({ providers: ok(TWO_THROTTLED) })
    const card = screen.getByRole('link', { name: /^anthropic is THROTTLED/ })
    const go = card.querySelector('.ov-att-go')!
    expect(text(go)).toBe('quota, all tenants · admin →')
    // MUTATION: drop `flex-wrap: wrap` from `.ov-att` in the 560px block.
    expect(painted(card, 'flex-wrap', PHONE)).toBe('wrap')
    // MUTATION: drop `flex-basis: 100%` from `.ov-att-go` in the 560px block.
    expect(painted(go, 'flex-basis', PHONE)).toBe('100%')
    // MUTATION: move the phone rules out of the 560px block, onto every width.
    expect(painted(card, 'flex-wrap', WIDE)).not.toBe('wrap')
    expect(painted(go, 'flex-basis', WIDE)).not.toBe('100%')
  })
})

describe('QA-109: the failures card’s link names the page it opens', () => {
  it('says "Agents · FAILED", the page’s own title and the state the card counts', async () => {
    await mount({
      tasks: ok({ tasks: [task({ id: 'tsk_f1', state: 'FAILED', completed_at: ago(5) })], tenant_id: 'eng', next_page_token: null } as TaskPage),
    })
    const card = screen.getByRole('link', { name: /^1 failed task/ })
    // MUTATION: put back `linkLabel: 'failed agents'`, a name the page it opens does not carry.
    expect(text(card.querySelector('.ov-att-go'))).toBe('Agents · FAILED →')
    expect(card.getAttribute('href')).toBe('/agents/recent?state=failed')
  })
})
