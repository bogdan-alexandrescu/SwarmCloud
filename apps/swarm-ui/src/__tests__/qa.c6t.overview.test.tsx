/**
 * QA ON SWARMCLOUD (2026-10-07): three Overview findings, at 1512 and 390.
 *
 *   G5-05  The Headroom account line said "4 of 4 usable · best team at 22%"
 *          while Accounts listed two accounts whose window had reset (`~1%`,
 *          `~0%`). `accountHeadroom` keeps a projected account out of `best`
 *          -- a projection is not headroom -- but the line said nothing about
 *          them. It now names them: "2 projected (a, b) not counted" (no typed `·`: V145).
 *   G1-08  At 390 a Running row was a dot and a step name, and two
 *          "implement" rows could not be told apart; the runtime was in the
 *          monospace face. The row now carries a second line -- state,
 *          profile, the short workflow id linking to it -- and the runtime is
 *          the desktop's sans face with tabular figures.
 *   G1-13  A disabled profile's tile cut its reason to one line mid-word.
 *          The reason now wraps to two lines, and the whole of it is the
 *          line's title.
 *
 * Each `it` names the mutation that turns it red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Account, AccountsPage, Capacity, Stats, TaskPage } from '../types'
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

const WIDE: CascadeEnv = { width: 1512 }
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
  tasks_by_state: { QUEUED: 0, READY: 0, PARKED: 0, LEASED: 0, DISPATCHED: 0, STARTING: 0, RUNNING: 2, SUCCEEDED: 0, FAILED: 0, DEAD_LETTERED: 0, CANCELLED: 0 },
  dispatch_paused: false,
  limits: {},
  generated_at: new Date().toISOString(),
}

function account(label: string, used: number, over: Partial<Account> = {}): Account {
  return {
    account_id: `eng:${label}`,
    owner_tenant: 'eng',
    label,
    provider: 'anthropic-subscription',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: { five_hour: { utilization: used, resets_at: '2099-01-01T00:00:00Z', reset: false } },
    observed_at: new Date().toISOString(),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  }
}

/** The QA's pool: two live accounts, two whose only window has reset since it was read. */
const RESET = { five_hour: { utilization: 0.01, resets_at: '2020-01-01T00:00:00Z', reset: true } }
const POOL_ACCOUNTS: Account[] = [
  account('team', 0.22),
  account('laptop', 0.6),
  account('devops-team', 0.01, { windows: RESET }),
  account('saga-personal', 0, { windows: { five_hour: { ...RESET.five_hour, utilization: 0 } } }),
]
const accountsPage = (accounts: Account[]): AccountsPage => ({ accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 })

async function mount(reads: { tasks?: Result<TaskPage>; accounts?: Result<AccountsPage> }): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(empty)
  api.loadTasks.mockResolvedValue(reads.tasks ?? empty)
  api.loadLeases.mockResolvedValue(empty)
  api.loadProviders.mockResolvedValue(empty)
  api.loadWorkflows.mockResolvedValue(empty)
  api.loadStats.mockResolvedValue(ok(STATS))
  api.loadAccountPool.mockResolvedValue(reads.accounts ?? empty)
  api.loadSpend.mockResolvedValue(empty)
  api.loadTask.mockResolvedValue(empty)
  const { container } = render(<Overview.OverviewScreen />)
  for (let i = 0; i < 40; i++) await new Promise((r) => setTimeout(r, 5))
  return container
}

afterEach(cleanup)

describe('G5-05: the account line names the projected accounts it did not count', () => {
  // MUTATION: drop `projected` from `accountHeadroom`'s answer, or the clause from `AccountLine`.
  it('appends "2 projected (devops-team, saga-personal) not counted" to the best-account line', async () => {
    const el = await mount({ accounts: ok(accountsPage(POOL_ACCOUNTS)) })
    const line = el.querySelector('.ov-acc')!
    expect(line, 'no account line').not.toBeNull()
    expect(text(line)).toMatch(/best team at 22% of its five-hour window/)
    expect(text(line)).toContain('2 projected (devops-team, saga-personal) not counted')
    expect(text(line)).toMatch(/^2 of 4 usable/)
  })

  it('returns the projected names from accountHeadroom, apart from the unread ones', () => {
    const h = Overview.accountHeadroom(ok(accountsPage([...POOL_ACCOUNTS, account('never', 0, { observed_at: null })])))
    expect(h.projected).toEqual(['devops-team', 'saga-personal'])
    expect(h.best?.label).toBe('team')
  })

  // MUTATION: print the clause whatever the list holds.
  it('says nothing more when every account is current', async () => {
    const el = await mount({ accounts: ok(accountsPage([account('team', 0.22), account('laptop', 0.6)])) })
    expect(text(el.querySelector('.ov-acc'))).not.toMatch(/projected/)
  })

  // MUTATION: draw the clause only beside a best figure.
  it('names them when no account has a current reading, too', async () => {
    const el = await mount({ accounts: ok(accountsPage(POOL_ACCOUNTS.slice(2))) })
    const line = text(el.querySelector('.ov-acc'))
    expect(line).toMatch(/no current reading/)
    expect(line).toContain('2 projected (devops-team, saga-personal) not counted')
  })
})

describe('G1-08: a phone Running row keeps its state, profile and workflow', () => {
  const tasks = (): Result<TaskPage> =>
    ok({
      tasks: [
        task({ id: 'tsk_c6t_a', state: 'RUNNING', step_id: 'implement', runner_profile: 'claude-code', workflow_id: 'wf_ccdd1422ab', started_at: ago(67), metadata: null }),
        task({ id: 'tsk_c6t_b', state: 'RUNNING', step_id: 'implement', runner_profile: 'codex', workflow_id: 'wf_99ee0011cc', started_at: ago(20), metadata: null }),
      ],
      next_page_token: null,
      tenant_id: 'eng',
    } as TaskPage)

  // MUTATION: drop the `.ov-prun-sub` line, or print the full workflow id in it.
  it('draws a second line with state, profile and the short workflow id, linking to the workflow', async () => {
    const el = await mount({ tasks: tasks() })
    const rows = [...el.querySelectorAll('#ov-running .ov-prun li')]
    expect(rows.length).toBe(2)
    const subs = rows.map((r) => text(r.querySelector('.ov-prun-sub')))
    expect(subs).toEqual(['running · claude-code · wf_ccdd14…', 'running · codex · wf_99ee00…'])
    // Two "implement" rows are now two different lines.
    expect(new Set(subs).size).toBe(2)
    const wf = rows[0]!.querySelector<HTMLAnchorElement>('.ov-prun-sub a')!
    expect(wf.getAttribute('href')).toBe('/workflows/wf_ccdd1422ab')
    expect(wf.getAttribute('title')).toContain('wf_ccdd1422ab')
    // The name stays the name.
    expect(text(rows[0]!.querySelector('.ov-prun-n'))).toBe('implement')
  })

  // MUTATION: put `var(--mono)` back on `.ov-prun em`, or drop its tabular figures.
  it('draws the runtime in the sans face with tabular figures, like the desktop column', () => {
    const host = document.createElement('div')
    host.innerHTML = '<ul class="ov-prun"><li><em>1h 7m</em></li></ul>'
    document.body.appendChild(host)
    const em = host.querySelector('.ov-prun em')!
    for (const prop of ['font', 'font-family']) {
      expect(painted(em, prop, PHONE) ?? '', `${prop} names the monospace face`).not.toMatch(/mono/)
    }
    expect(painted(em, 'font-variant-numeric', PHONE)).toBe('tabular-nums')
    host.remove()
  })
})

describe('G1-13: a disabled tile shows its whole reason, two lines before it cuts', () => {
  const REASON = 'the merge chain (#295) is disabled until its review step stops looping'
  const profile = {
    resource_class: 'standard',
    backend: 'cloud_run',
    provider: 'anthropic',
    units: 1,
    pools: ['global'],
    available: false,
    disabled_reason: REASON,
  } as unknown as Capacity['runner_profiles'][string]

  it('keeps the whole reason in the DOM and in the line title', () => {
    const { container } = render(<Overview.ProfileTile name="merge-chain" profile={profile} byName={new Map()} tenant="eng" />)
    const small = container.querySelector('.ov-hp.is-off small')!
    expect(text(small)).toBe(REASON)
    expect(small.getAttribute('title')).toContain(REASON)
  })

  // MUTATION: put `white-space: nowrap` back on the disabled tile's reason, or drop the two-line clamp.
  it('wraps the reason to two lines rather than one ellipsis', () => {
    const host = document.createElement('div')
    host.innerHTML = '<div class="ov-headroom"><div class="ov-hps"><div class="ov-hp is-off"><span class="ov-idc">x</span><b class="ov-hp-off">disabled</b><small>r</small></div></div></div>'
    document.body.appendChild(host)
    const small = host.querySelector('.ov-hp.is-off small')!
    for (const env of [WIDE, PHONE]) {
      expect(painted(small, 'white-space', env)).not.toBe('nowrap')
      expect(painted(small, '-webkit-line-clamp', env)).toBe('2')
      expect(painted(small, 'display', env)).toBe('-webkit-box')
      expect(painted(small, '-webkit-box-orient', env)).toBe('vertical')
      expect(painted(small, 'overflow', env)).toBe('hidden')
    }
    // An available tile's one-line foot is unchanged.
    host.querySelector('.ov-hp')!.classList.remove('is-off')
    expect(painted(host.querySelector('.ov-hp small')!, 'white-space', WIDE)).toBe('nowrap')
    host.remove()
  })
})
