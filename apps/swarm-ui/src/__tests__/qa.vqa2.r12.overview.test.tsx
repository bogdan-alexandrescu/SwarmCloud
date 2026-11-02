/**
 * VISUAL QA RE-RUN, LANE R12 (#1427, 2026-10-16): the Overview findings.
 *
 *   V057  On a phone the pool bars are hidden, so a pool at >=80% use takes
 *         the warn tone on its figure, as a full one takes the bad (owner Q4).
 *   V142  The lifecycle band drew a successful empty read as "—" under
 *         Finished today beside "0" under Waiting and Holding.
 *   V176  Needs a look held one ~80px skeleton card until every check ran,
 *         then ten cards landed and everything below jumped.
 *   V177  The phone's Running now list had no row cap.
 *   V258  "Waiting, and why" joined a workflow id and its count with a space.
 *   V259  An in-card absence's reason was only the mark's accessible name.
 *   V263  Recent failures cut the workflow id at every width, and its title
 *         repeated the cut text (owner Q5: whole at desktop width).
 *
 * Each `it` names the mutation that turns it red.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Pool, Stats, TaskPage } from '../types'
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
const { waitGroups } = await import('../OverviewRegions')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 400 }
const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()
const LONG_WF = 'wf_' + '5e5ad3b6f7da4299a839'.repeat(2)

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

function pool(name: string, active: number, limit: number | null, enabled = true): Pool {
  return {
    name,
    hard_limit: limit,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: limit,
    active,
    available: limit === null ? null : Math.max(0, limit - active),
    enabled,
    updated_at: new Date().toISOString(),
  }
}

interface Reads {
  tasks?: Result<TaskPage>
  capacity?: Result<Capacity>
  stats?: Result<Stats>
}

async function mount(reads: Reads): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(reads.capacity ?? empty)
  api.loadTasks.mockResolvedValue(reads.tasks ?? empty)
  api.loadLeases.mockResolvedValue(empty)
  api.loadProviders.mockResolvedValue(empty)
  api.loadWorkflows.mockResolvedValue(empty)
  api.loadStats.mockResolvedValue(reads.stats ?? ok(STATS))
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
  for (const fn of Object.values(api)) fn.mockReset()
})

describe('V057: a pool at >=80% takes the warn tone on its figure (owner Q4)', () => {
  // MUTATION: drop `is-near` from PoolRow, or its rule from overview.css.
  it('tones 4/5 warn, 5/5 bad, and leaves 3/5, a paused 4/5 and an unset limit plain', () => {
    const { container } = render(
      <div className="ov-pools">
        <Overview.PoolRow pool={pool('global', 4, 5)} />
        <Overview.PoolRow pool={pool('provider:anthropic:tenant:eng', 5, 5)} />
        <Overview.PoolRow pool={pool('runner:mock', 3, 5)} />
        <Overview.PoolRow pool={pool('runner:browser', 4, 5, false)} />
        <Overview.PoolRow pool={pool('resource:gpu', 0, null)} />
      </div>,
    )
    const figures = [...container.querySelectorAll('.ov-pl > b')]
    expect(figures.map((b) => b.className)).toEqual(['is-near', 'is-full', '', '', ''])
    expect(figures[0]!.closest('.ov-pl')!.getAttribute('title')).toMatch(/, nearly full$/)
    for (const env of [WIDE, PHONE]) expect(painted(figures[0]!, 'color', env), `width ${env.width}`).toBe('var(--warn)')
  })
})

describe('V142: a successful empty read counts 0 under Finished today', () => {
  // MUTATION: give LifecycleBand `rows(tasks)` back, which is null for `empty`.
  it('draws 0 in all three cells, never the not-measured dash', async () => {
    const el = await mount({ tasks: empty as unknown as Result<TaskPage> })
    const cells = [...el.querySelectorAll('#ov-band .ov-lc')]
    expect(cells.length).toBe(3)
    expect(cells.map((c) => text(c.querySelector('.ov-lc-n')))).toEqual(['0', '0', '0'])
    const done = cells[2]!
    expect(done.querySelector('.ov-lc-n')!.getAttribute('title')).not.toMatch(/^Not measured/)
    // MUTATION (VOCAB-AGENTS): put back `'no task exists'` in LifecycleBand's page foot.
    expect(text(done.querySelector('.ov-lc-foot.is-page'))).toBe('no agent exists')
  })
})

describe('V176: Needs a look holds the room it last drew while its checks read', () => {
  const pending = (): HTMLElement => {
    for (const fn of Object.values(api)) fn.mockReturnValue(new Promise(() => {}))
    return render(<Overview.OverviewScreen />).container
  }
  const skeletons = (el: HTMLElement): number => el.querySelectorAll('#ov-needs .ov-atts[aria-busy="true"] .ov-att.is-skel').length

  it('reserves one card before the row has ever settled', async () => {
    vi.resetModules()
    const fresh = await import('../Overview')
    expect(fresh.needsRoom()).toBe(1)
  })

  // MUTATION: draw the single skeleton card back, whatever was kept.
  it('reserves as many skeleton cards as the row last settled at, one to twelve', async () => {
    Overview.keepNeedsRoom(6)
    const el = pending()
    await new Promise((r) => setTimeout(r, 5))
    expect(skeletons(el)).toBe(6)
    Overview.keepNeedsRoom(400)
    expect(Overview.needsRoom()).toBe(12)
    Overview.keepNeedsRoom(0)
    expect(Overview.needsRoom()).toBe(1)
  })

  // MUTATION: drop the `keepNeedsRoom` effect.
  it('remembers how many cards it drew once every check has run', async () => {
    Overview.keepNeedsRoom(9)
    const tasks = Array.from({ length: 3 }, (_, i) => task({ id: `tsk_f${i}`, state: 'FAILED', completed_at: ago(i + 1), step_id: `s${i}` }))
    const el = await mount({ tasks: ok({ tasks, tenant_id: 'eng', next_page_token: null } as TaskPage) })
    expect(el.querySelector('#ov-needs .ov-atts[aria-busy="true"]'), 'the checks never settled').toBeNull()
    const cards = el.querySelectorAll('#ov-needs .ov-att:not(.is-skel)').length
    expect(cards).toBeGreaterThan(0)
    expect(cards).not.toBe(9)
    expect(Overview.needsRoom()).toBe(cards)
  })
})

describe('V177: the phone’s Running now list stops at eight rows', () => {
  // MUTATION: map every running task into `ul.ov-prun` again.
  it('lists eight, then "39 more" with the rest in place, on the phone only', async () => {
    const tasks = Array.from({ length: 47 }, (_, i) =>
      task({ id: `tsk_h${i}`, state: 'RUNNING', started_at: ago(i + 1), step_id: `s${i}` }),
    )
    const el = await mount({ tasks: ok({ tasks, tenant_id: 'eng', next_page_token: null } as TaskPage) })
    const card = el.querySelector('#ov-running')!
    const list = card.querySelector('ul.ov-prun[aria-label="Running now"]')!
    expect(list.querySelectorAll(':scope > li').length).toBe(8)
    const more = card.querySelector('details.ov-prun-more')!
    expect(more, 'no phone "N more"').not.toBeNull()
    expect(text(more.querySelector('summary'))).toBe('39 more')
    expect(more.querySelectorAll('li').length).toBe(39)
    expect(painted(more, 'display', WIDE)).toBe('none')
    expect(painted(more, 'display', PHONE)).toBe('block')
  })
})

describe('V258: Waiting, and why sets a workflow’s count apart from its id', () => {
  // MUTATION: join `${w} ${n}` with a bare space again.
  it('reads "wf_audit_01 ×1", never "wf_audit_01 1"', () => {
    const [g] = waitGroups([
      task({ id: 'p1', state: 'PARKED', park_reason: 'CHECKS_PENDING', workflow_id: 'wf_audit_01', created_at: ago(5), updated_at: ago(1) }),
    ])
    expect(g!.detail).toContain('wf_audit_01 ×1')
    expect(g!.detail).not.toMatch(/wf_audit_01 1/)
  })
})

describe('V259: an in-card absence says why in sight', () => {
  // MUTATION: drop the `readWhy` sentence from TaskListAbsent or CardAbsent.
  it('draws the read’s reason under every unread card heading', async () => {
    const el = await mount({ tasks: failed as unknown as Result<TaskPage>, capacity: failed as unknown as Result<Capacity> })
    for (const id of ['ov-running', 'ov-waiting', 'ov-failures', 'ov-pools']) {
      const absent = el.querySelector(`#${id} .ctl-empty.ov-empty.is-failed`)
      expect(absent, `${id} drew no absence`).not.toBeNull()
      const p = absent!.querySelector(':scope > p')
      expect(p, `${id} drew no reason`).not.toBeNull()
      expect(text(p)).toMatch(/boom\.$/)
      for (const env of [WIDE, PHONE]) expect(painted(p!, 'display', env)).not.toBe('none')
    }
  })
})

describe('V263: Recent failures names the whole workflow id at desktop width (owner Q5)', () => {
  // MUTATION: draw `shortWf` again, or drop the phone's `.ov-wf-id` rule.
  it('draws the whole id wide, the short one on a phone, and the whole id in the title', async () => {
    const el = await mount({
      tasks: ok({ tasks: [task({ id: 'tsk_f1', state: 'FAILED', completed_at: ago(3), step_id: 'synthesis', workflow_id: LONG_WF })], tenant_id: 'eng', next_page_token: null } as TaskPage),
    })
    const link = el.querySelector('#ov-failures .ov-fails .ov-name')!
    expect(link, 'no failure row').not.toBeNull()
    // The link's words, its short id `f1` included (walk.long C), then the whole id.
    expect(link.getAttribute('title')).toBe(`synthesis · ${LONG_WF} · f1 · tsk_f1`)
    const id = link.querySelector('.ov-wf-id')!
    expect(text(id)).toBe(LONG_WF)
    // Whole at desktop width: nothing cuts it.
    expect(painted(id, 'max-width', WIDE)).toBeNull()
    expect(painted(id, 'text-overflow', WIDE)).toBeNull()
    // The phone's short form: cut to about nine characters and an ellipsis.
    expect(painted(id, 'max-width', PHONE)).toBe('10ch')
    expect(painted(id, 'text-overflow', PHONE)).toBe('ellipsis')
    expect(painted(id, 'overflow', PHONE)).toBe('hidden')
  })
})
