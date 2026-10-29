// QA2-FIX-18 (visual QA pass 2, part of #2694): overview-agents-02, Needs a
// look names a workflow and an agent by its title, the id on hover.
//
// The failed-agents card read 'Workflow wf_5e5ad3b6f7da4299a8…' and its ids
// line 'wf_5e5ad3b6f7da4299a839'; the silent-workers card 'task_8ea5c26d'.
// The card now names each id from the full task read (`useRowTitles`), and
// keeps the id as the name's `title`. An id with no title read stays the id.
//
// Placeholders only; nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Stats, Task, TaskPage } from '../types'
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

const MIN = 60_000
const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()
const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const empty = { status: 'empty', fetchedAt: Date.now() } as const

const WF = 'wf_5e5ad3b6f7da4299a839'
const TITLE = 'Check the five contract invariants, then write one synthesis'
const FAILED = task({ id: 'task_fan_3', state: 'FAILED', step_id: 'fan_3', workflow_id: WF, completed_at: ago(30), updated_at: ago(30), metadata: null, last_error: 'exit 1' })

const STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: { QUEUED: 0, READY: 0, PARKED: 0, LEASED: 0, DISPATCHED: 0, STARTING: 0, RUNNING: 0, SUCCEEDED: 0, FAILED: 2, DEAD_LETTERED: 0, CANCELLED: 0 },
  dispatch_paused: false,
  limits: {},
  generated_at: new Date().toISOString(),
}

async function mount(titled: boolean): Promise<HTMLElement> {
  api.loadCapacity.mockResolvedValue(empty)
  api.loadTasks.mockResolvedValue(ok({ tasks: [FAILED], next_page_token: null, tenant_id: 'eng' } as TaskPage))
  api.loadLeases.mockResolvedValue(empty)
  api.loadProviders.mockResolvedValue(empty)
  api.loadWorkflows.mockResolvedValue(empty)
  api.loadStats.mockResolvedValue(ok(STATS))
  api.loadAccountPool.mockResolvedValue(empty)
  api.loadSpend.mockResolvedValue(empty)
  api.loadTask.mockImplementation(async (id: string): Promise<Result<Task>> => {
    const t = id === FAILED.id ? FAILED : null
    if (t === null || !titled) return empty as unknown as Result<Task>
    return ok({ ...t, metadata: { title: TITLE } })
  })
  const { container } = render(<Overview.OverviewScreen />)
  for (let i = 0; i < 60; i++) await new Promise((r) => setTimeout(r, 5))
  return container
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('overview-agents-02: Needs a look names what it found by title, the id on hover', () => {
  it("draws the failed workflow's link and ids by its title", async () => {
    // Module-level title cache (`useRowTitles`): this case runs first, with titles.
    const el = await mount(true)
    const card = el.querySelector('#ov-needs .ov-att.is-split')
    expect(card, 'no failed-agents card naming a workflow').not.toBeNull()
    const link = card!.querySelector('a.ov-att-wf')!
    // MUTATION: the link back to `Workflow {w.id}`.
    expect(text(link.querySelector('.ov-att-wf-t'))).toBe(TITLE)
    expect(link.getAttribute('title')).toBe(`Open workflow ${WF}`)
    const ids = card!.querySelector('.ov-att-ids')
    if (ids !== null && (ids.textContent ?? '').includes('Check')) {
      // MUTATION: `linkedIds` back to printing `next.w.id`.
      expect(text(ids)).not.toContain(WF)
    }
    expect(text(card)).not.toContain(WF)
  })

  it("draws a silent worker's task id by its agent's title, the id its title", () => {
    const problem = {
      severity: 'warn' as const,
      n: 1,
      headline: '1 worker silent past the grace period: task_8ea5c26d',
      detail: 'task_8ea5c26d has not heartbeat for 2m 20s.',
      href: '#capacity/holders',
    }
    const names = (id: string) => (id === 'task_8ea5c26d' ? 'Fix flaky lease heartbeat test' : null)
    const { container } = render(<Overview.CheckCard problem={problem} name={names} />)
    const ids = container.querySelector('.ov-att-ids')!
    // MUTATION: the ids line back to `keepDash(ids)`, unnamed.
    expect(text(ids)).toBe('Fix flaky lease heartbeat test')
    expect(ids.querySelector('span[title="task_8ea5c26d"]')).not.toBeNull()
    // MUTATION: the detail back to `{detail}`, unnamed.
    expect(text(container.querySelector('small.is-one'))).toBe('Fix flaky lease heartbeat test has not heartbeat for 2m 20s.')
    // The whole sentence, ids and all, stays the card's accessible name.
    expect(container.querySelector('a.ov-att')!.getAttribute('aria-label')).toContain('task_8ea5c26d')
  })

  it('leaves an id it has no title for as the id', () => {
    const problem = { severity: 'warn' as const, n: 1, headline: '1 worker silent past the grace period: task_00000000', detail: 'x', href: '#capacity/holders' }
    const { container } = render(<Overview.CheckCard problem={problem} />)
    expect(text(container.querySelector('.ov-att-ids'))).toBe('task_00000000')
  })
})
