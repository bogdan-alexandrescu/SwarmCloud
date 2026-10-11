/**
 * VISUAL QA LANE L03 (#1038, 2026-10-11): the Overview findings.
 *
 *   V009  Overview's own 56px under the page stacked a second blank band on
 *         the Top pill's; the scroller reserves the room (qa.g1.overview).
 *   V053  A long workflow id widened Running now's Agent column past the card.
 *   V055  Waiting, and why: an id ran through the border; the reason broke
 *         mid-word.
 *   V056  Every card head was padded inside an already padded card.
 *   V057  At 400px the pool bars are hidden (owner Q3), so a full pool's
 *         figure carries the bad tone itself.
 *   V140  A check card's sub-line was sliced at the card edge; a lone "—…".
 *   V141  Two "could not read" pictures; the partial pill twice.
 *   V142  Three pictures of one zero; `tenant —` beside a read tenant.
 *   V143  The phone's refresh alone in its band; the first title cut before
 *         its noun.
 *   V144  Recent failures stopped at six rows with no "N more".
 *   V145  A typed `·` opened the projected clause and orphaned on a wrap.
 *
 * Each `it` names the mutation that turns it red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Problem } from '../checks'
import type { Account, AccountsPage, Capacity, Pool, Stats, TaskPage } from '../types'
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
const PHONE: CascadeEnv = { width: 400 }
const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()
const LONG_WF = 'wf_' + '5e5ad3b6f7da4299a839'.repeat(3)

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}
const empty = { status: 'empty', fetchedAt: Date.now() } as const
const failed = { status: 'error', error: { kind: 'server_error', httpStatus: 500, code: null, message: 'boom' } } as const

const STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: { QUEUED: 0, READY: 0, PARKED: 0, LEASED: 0, DISPATCHED: 0, STARTING: 0, RUNNING: 0, SUCCEEDED: 0, FAILED: 0, DEAD_LETTERED: 0, CANCELLED: 0 },
  dispatch_paused: false,
  limits: {},
  generated_at: new Date().toISOString(),
}

function pool(name: string, active: number, limit: number | null): Pool {
  return {
    name,
    hard_limit: limit,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: limit,
    active,
    available: limit === null ? null : Math.max(0, limit - active),
    enabled: true,
    updated_at: new Date().toISOString(),
  }
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

interface Reads {
  tasks?: Result<TaskPage>
  capacity?: Result<Capacity>
  accounts?: Result<AccountsPage>
}

async function mount(reads: Reads): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(reads.capacity ?? empty)
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

/** A bare frame of the classes the sheet styles, for the cascade checks. */
function frame(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  return host
}

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

describe('V056: an Overview card head is padded once, by its card', () => {
  // MUTATION: drop `padding: 0 0 var(--ctl-s2)` from `.ov-card .ctl-card-head`.
  it('gives up the head primitive’s side and top padding inside .ov-card', () => {
    const host = frame('<section class="ctl-card ov-card"><div class="ctl-card-head"><h2 class="ctl-card-title">Running now</h2></div></section>')
    const head = host.querySelector('.ctl-card-head')!
    for (const env of [WIDE, PHONE]) {
      const pad = painted(head, 'padding', env)
      expect(pad, 'the head kept its own padding').toBe('0 0 var(--ctl-s2)')
    }
  })
})

describe('V053: a long workflow id breaks inside Running now’s Agent cell', () => {
  // MUTATION: drop `overflow-wrap: anywhere` from the row head / `.ov-sub`.
  it('lets the row head and its sub-line break an id anywhere', async () => {
    const el = await mount({
      tasks: ok({ tasks: [task({ id: 'tsk_r1', state: 'RUNNING', started_at: ago(3), workflow_id: LONG_WF, step_id: 'plan' })], tenant_id: 'eng', next_page_token: null } as TaskPage),
    })
    const th = el.querySelector('.ov-running .ov-tbl tbody th')!
    expect(th, 'no running row').not.toBeNull()
    expect(text(th)).toContain(LONG_WF)
    expect(painted(th, 'overflow-wrap', WIDE)).toBe('anywhere')
    expect(painted(th.querySelector('.ov-sub')!, 'overflow-wrap', WIDE)).toBe('anywhere')
  })
})

describe('V055: Waiting, and why wraps ids, not words', () => {
  // MUTATION: put `anywhere` back on the reason, or drop it from the detail.
  it('breaks the detail line’s ids and keeps the reason’s words whole', async () => {
    const el = await mount({
      tasks: ok({
        tasks: [task({ id: 'tsk_p1', state: 'PARKED', park_reason: 'WAITING_ON_DEPENDENCY', workflow_id: LONG_WF, next_eligible_at: ago(-5) } as never)],
        tenant_id: 'eng',
        next_page_token: null,
      } as TaskPage),
    })
    const row = el.querySelector('.ov-wr')!
    expect(row, 'no waiting row').not.toBeNull()
    expect(text(row.querySelector('.ov-wt small'))).toContain(LONG_WF)
    expect(painted(row.querySelector('.ov-wt small')!, 'overflow-wrap', PHONE)).toBe('anywhere')
    expect(painted(row.querySelector('.ov-wt b')!, 'overflow-wrap', PHONE)).toBe('break-word')
  })
})

describe('V057: a full pool’s figure carries the bad tone (owner Q3)', () => {
  // MUTATION: drop `is-full` from PoolRow, or its rule from overview.css.
  it('marks 5/5 full and leaves 4/5 and an unset limit plain', () => {
    const { container } = render(
      <div className="ov-pools">
        <Overview.PoolRow pool={pool('provider:anthropic:tenant:eng', 5, 5)} />
        <Overview.PoolRow pool={pool('global', 4, 5)} />
        <Overview.PoolRow pool={pool('runner:mock', 0, null)} />
      </div>,
    )
    const figures = [...container.querySelectorAll('.ov-pl > b')]
    expect(figures.map((b) => b.classList.contains('is-full'))).toEqual([true, false, false])
    expect(figures[0]!.closest('.ov-pl')!.getAttribute('title')).toMatch(/, full$/)
    for (const env of [WIDE, PHONE]) expect(painted(figures[0]!, 'color', env)).toBe('var(--bad)')
    // The bar stays hidden on a phone: the owner kept that rule.
    const track = container.querySelector('.ov-pl .ctl-util-track')
    if (track !== null) expect(painted(track, 'display', PHONE)).toBe('none')
  })
})

describe('V140: a check card’s sub-lines end in an ellipsis, never a hard cut', () => {
  const p: Problem = {
    severity: 'warn',
    n: 1,
    headline: '1 workflow could not be judged',
    detail: `${LONG_WF} — their rollup came back incomplete (no reason given).`,
    href: '#work/workflows',
  }
  // MUTATION: drop `overflow-wrap: anywhere` from `.ov-att-t small`.
  it('lets a clamped line break a long id', () => {
    const { container } = render(<Overview.CheckCard problem={p} />)
    const small = container.querySelector('.ov-att-t small')!
    expect(painted(small, 'overflow-wrap', PHONE)).toBe('anywhere')
    expect(painted(small, '-webkit-line-clamp', PHONE)).toBe('2')
  })
  // MUTATION: render `p.detail` as it came.
  it('binds the dash to the word before it, so no line opens on "—"', () => {
    const { container } = render(<Overview.CheckCard problem={p} />)
    const small = container.querySelector('.ov-att-t small')!
    expect(small.textContent).toContain(`${LONG_WF} — their`)
    expect(small.textContent).not.toMatch(/ —/)
    expect(small.getAttribute('title'), 'the title keeps the text as written').toBe(p.detail)
  })
})

describe('V141/V142: one picture per absence on the Overview cards', () => {
  // MUTATION: put the em-text lines back in PoolsBody, WaitingWhy or RecentFailures.
  it('draws a failed task read as the not-read absence in every card that reads it', async () => {
    const el = await mount({ tasks: failed as unknown as Result<TaskPage> })
    for (const id of ['ov-running', 'ov-waiting', 'ov-failures']) {
      const card = el.querySelector(`#${id}`)!
      expect(card.querySelector('.ctl-empty.ov-empty.is-failed'), `${id} did not draw the absence`).not.toBeNull()
      expect(text(card), `${id} drew the em-text line`).not.toMatch(/— not read/)
    }
  })

  it('draws a failed pool read the same way in Pools as in Headroom', async () => {
    const el = await mount({ capacity: failed as unknown as Result<Capacity> })
    for (const id of ['ov-headroom', 'ov-pools']) {
      expect(el.querySelector(`#${id} .ctl-empty.ov-empty.is-failed`), `${id} did not draw the absence`).not.toBeNull()
    }
    expect(text(el.querySelector('#ov-pools'))).not.toMatch(/see Headroom/)
  })

  it('draws a real zero as the real-zero mark in Running, Waiting and Failures alike', async () => {
    const el = await mount({ tasks: ok({ tasks: [task({ id: 'tsk_s', state: 'SUCCEEDED', completed_at: ago(4) })], tenant_id: 'eng', next_page_token: null } as TaskPage) })
    for (const id of ['ov-running', 'ov-waiting', 'ov-failures']) {
      const card = el.querySelector(`#${id}`)!
      expect(card.querySelector('.ctl-empty.ov-empty .ctl-mark.is-zero'), `${id} drew its zero some other way`).not.toBeNull()
      expect(card.querySelector('p.ctl-em'), `${id} kept an em-text zero`).toBeNull()
    }
  })

  // MUTATION: draw the head's partial mark whatever is under it.
  it('draws the partial pill once in Needs a look when nothing was found', async () => {
    const el = await mount({ tasks: failed as unknown as Result<TaskPage> })
    const lead = el.querySelector('#ov-needs')!
    const marks = lead.querySelectorAll('.ctl-mark.is-partial')
    expect(marks.length, 'the partial pill is drawn twice').toBeLessThanOrEqual(1)
  })

  // MUTATION: drop the margin after the mark in an in-card absence heading.
  it('sets the mark off its heading', () => {
    const host = frame('<div class="ctl-empty ov-empty"><h3><i class="ctl-mark is-partial">partial</i> 2 not checked</h3></div>')
    expect(painted(host.querySelector('.ctl-mark')!, 'margin-right', WIDE)).toBe('var(--ctl-s1)')
  })

  // MUTATION: read the tenant off the task page only.
  it('names the tenant another read carried instead of `tenant —`', async () => {
    const el = await mount({
      tasks: ok({ tasks: [], tenant_id: null, next_page_token: null } as unknown as TaskPage),
      capacity: ok({ pools: [pool('tenant:eng', 1, 5)], runner_profiles: {} } as Capacity),
    })
    const note = el.querySelector('.c-count-note')!
    expect(text(note)).toMatch(/^tenant eng ·/)
    expect(text(note)).not.toMatch(/tenant —/)
  })
})

describe('V143: the phone head shares its line, and a check title keeps its noun', () => {
  const page =
    '<div class="ov-page"><div class="c-phead"><div class="head"><h1>Overview</h1></div><div class="c-acts">refresh</div></div>' +
    '<p class="c-count-note">tenant eng</p><section class="ov-lead"></section></div>'
  // MUTATION: drop the phone grid that puts the note and the head on one row.
  it('puts the count note and the refresh on the first row at 400', () => {
    const host = frame(page)
    expect(painted(host.querySelector('.ov-page')!, 'display', PHONE)).toBe('grid')
    expect(painted(host.querySelector('.c-phead')!, 'grid-row', PHONE)).toBe('1')
    expect(painted(host.querySelector('.c-count-note')!, 'grid-row', PHONE)).toBe('1')
    expect(painted(host.querySelector('.ov-lead')!, 'grid-column', PHONE)).toBe('1 / -1')
    // A desktop keeps its column.
    expect(painted(host.querySelector('.ov-page')!, 'display', WIDE)).toBe('flex')
  })
  // MUTATION: put `white-space: nowrap` back on the check card title.
  it('lets a check title wrap to two lines', () => {
    const host = frame('<a class="ov-att"><span class="ov-att-t"><b>2 failed tasks among the 26 most recent</b></span></a>')
    const b = host.querySelector('b')!
    expect(painted(b, 'white-space', PHONE) ?? 'normal').not.toBe('nowrap')
    expect(painted(b, '-webkit-line-clamp', PHONE)).toBe('2')
  })
})

describe('V144: Recent failures counts what it does not list', () => {
  // MUTATION: drop the "N more" disclosure after six rows.
  it('says "3 more" under six rows of nine failures, and lists them in place', async () => {
    const tasks = Array.from({ length: 9 }, (_, i) => task({ id: `tsk_f${i}`, state: 'FAILED', completed_at: ago(i + 1), step_id: `s${i}` }))
    const el = await mount({ tasks: ok({ tasks, tenant_id: 'eng', next_page_token: null } as TaskPage) })
    const card = el.querySelector('#ov-failures')!
    const more = card.querySelector('details.ov-more')!
    expect(more, 'no "N more"').not.toBeNull()
    expect(text(more.querySelector('summary'))).toBe('3 more')
    expect(more.querySelectorAll('tbody tr').length).toBe(3)
    expect(card.querySelectorAll('table.ov-fails > tbody > tr').length).toBe(9)
  })
})

describe('V145: the projected clause opens with no typed separator', () => {
  const RESET = { five_hour: { utilization: 0.01, resets_at: '2020-01-01T00:00:00Z', reset: true } }
  // MUTATION: put `· ` back in front of the clause.
  it('starts the clause with its count', async () => {
    const el = await mount({
      accounts: ok({ accounts: [account('team', 0.2), account('old', 0.01, { windows: RESET })], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 }),
    })
    const proj = el.querySelector('.ov-acc-proj')!
    expect(proj, 'no projected clause').not.toBeNull()
    expect(text(proj)).toMatch(/^1 projected \(old\) not counted$/)
  })
})
