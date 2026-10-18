/**
 * LIVE QA, WORK › TIMELINE › LANES (section work-runs-timeline, report of
 * 2026-10-11). Each case states the property the finding broke:
 *
 *   RT-1   the lanes cover the span picked: the attempts read follows its
 *          page token across the span, and a read that stops short says how
 *          far back it reaches instead of naming the span. The task read
 *          pages back across the span too, so an old lane carries its state
 *          and profile and survives a filter.
 *   RT-7b  a settled workflow and an ended lane's events are not re-read
 *          every minute.
 *   RT-22  the count follows the filters; zooming keeps one way back; the
 *          window pans.
 *   RT-23  Escape closes the lane inspector.
 *   RT-24  a mark is a control that opens its lane; the attempt count is the
 *          task's, beside the span's.
 *   RT-25  a workflow is named as the Workflows list names it.
 *
 * MUTATIONS: read one attempts page (`goOn = false`); read one task page
 * (`taskReadCovers` always true); drop `reach` from the
 * count note; clear every workflow on refresh; keep `back` from
 * `zoomedTo` alone -- each turns a case red.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { AttemptRow, AttemptsPage, Task, TaskPage } from '../types'
import type { WorkflowRead } from '../api'
import { byIdVia } from './workflowsById'

const api = vi.hoisted(() => ({
  loadAttemptsPage: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadTasks: vi.fn(),
  loadTask: vi.fn(),
  loadWorkflow: vi.fn(),
  loadWorkflowsById: vi.fn(),
  loadOutcomes: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { TimelineLanesScreen, SPAN_PAGES_READ, TASK_PAGES_READ, drawnReach, panWindow, taskReadCovers, workflowSettled } = await import('../TimelineLanes')
const { parseLanesView } = await import('../lanes')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const NOW = Date.now()
const HOUR = 3_600_000
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()

function task(id: string, extra: Partial<Task> = {}): Task {
  return {
    id, tenant_id: 'eng', state: 'SUCCEEDED', runner_profile: 'claude-code', resource_class: 'standard', provider: null,
    priority: 0, created_at: ago(300), updated_at: ago(100), started_at: ago(290), completed_at: ago(100),
    submitted_by: 'operator@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: null, step_id: null, depends_on: null, cancel_requested: false, repository_url: null,
    current_generation: 1, current_lease_id: null, metadata: null, ...extra,
  } as Task
}

function attempt(taskId: string, gen: number, from: number, to: number): AttemptRow {
  return {
    attempt_id: `att_${taskId}_${gen}`, task_id: taskId, tenant_id: 'eng', generation: gen, lease_id: `l${gen}`,
    backend: 'cloud_run', execution_name: null, created_at: ago(from), started_at: ago(from - 1),
    completed_at: ago(to), exit_code: 0, error: null, peak_rss_bytes: null, peak_disk_bytes: null, oom_near_miss: false,
    checkpoints: [], input_tokens: null, output_tokens: null, cache_read_input_tokens: null,
    cache_creation_input_tokens: null, cost_usd: null,
  } as AttemptRow
}

const page = (attempts: AttemptRow[], next: string | null = null): AttemptsPage =>
  ({ tenant_id: 'eng', read_at: new Date(NOW).toISOString(), attempts, next_page_token: next, coverage: { scope: 'page' } }) as AttemptsPage

// Three tasks, an attempt each, spread over a week: one per page.
const STEP_A = task('task_a', { workflow_id: 'wf_named', step_id: 'impl', metadata: { unit: 'PERM-2' } })
const STEP_B = task('task_b', { workflow_id: 'wf_named', step_id: 'review', state: 'FAILED' })
const OLD = task('task_old', { attempt_count: 3, current_generation: 3, created_at: ago(7 * 24 * 60) })
const TASKS = [STEP_A, STEP_B, OLD]
const PAGES: Record<string, AttemptsPage> = {
  first: page([attempt('task_a', 1, 60, 30)], 'p2'),
  p2: page([attempt('task_b', 1, 26 * 60, 25 * 60)], 'p3'),
  p3: page([attempt('task_old', 3, 6 * 24 * 60, 6 * 24 * 60 - 30)], null),
}
const WF_READ = {
  workflow: { workflow_id: 'wf_named', state: 'FAILED', steps: [{ step_id: 'impl', task_id: 'task_a' }, { step_id: 'review', task_id: 'task_b' }] },
  tasks: [STEP_A, STEP_B],
} as unknown as WorkflowRead

beforeEach(() => {
  api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
  api.loadAttemptsPage.mockImplementation(async (o: { pageToken?: string | null }) => ok(PAGES[o.pageToken ?? 'first']!))
  api.loadTaskEventsPage.mockResolvedValue(ok({ events: [], next_page_token: null }))
  api.loadTasks.mockResolvedValue(ok({ tasks: TASKS } as TaskPage))
  api.loadTask.mockResolvedValue({ status: 'error', error: { kind: 'server', httpStatus: 404, code: null, message: 'gone' } })
  api.loadWorkflow.mockResolvedValue(ok(WF_READ))
  api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

/** The screen under a parent that owns the address, as App does. */
function Routed({ initial, seen }: { initial: string; seen: (q: string) => void }) {
  const [q, setQ] = useState(initial)
  return (
    <TimelineLanesScreen
      view={q}
      onView={(next) => {
        seen(next)
        setQ(next)
      }}
    />
  )
}

const lane = (id: string) => document.querySelector(`[data-lane="${id}"]`)
const countNote = () => (document.querySelector('.c-count-note, .ctl-count-note, [data-count-note]') ?? screen.getByText(/lanes?$/)).textContent ?? ''

describe('RT-1: the lanes cover the span picked', () => {
  it('follows the page token across a 7-day span, so a week-old attempt is drawn', async () => {
    render(<TimelineLanesScreen view="span=7d" onView={() => {}} />)
    await waitFor(() => expect(lane('task_old')).not.toBeNull())
    expect(api.loadAttemptsPage).toHaveBeenCalledTimes(3)
    expect(lane('task_a')).not.toBeNull()
    expect(lane('task_b')).not.toBeNull()
    expect(countNote()).not.toMatch(/newest/)
  })

  it('stops at SPAN_PAGES_READ, and then names the reach drawn, not the span', async () => {
    api.loadAttemptsPage.mockImplementation(async () => ok(page([attempt('task_a', 1, 60, 30)], 'more')))
    render(<TimelineLanesScreen view="span=90d" onView={() => {}} />)
    await waitFor(() => expect(api.loadAttemptsPage).toHaveBeenCalledTimes(SPAN_PAGES_READ))
    await waitFor(() => expect(countNote()).toMatch(/newest \d+ attempts, .+ of /))
    expect(document.querySelector('.tl-lanes-range')?.textContent ?? '').not.toBe('')
  })

  it('pages the task read back across the span, so a lane off the first task page keeps its state and profile', async () => {
    // The real shape of a wide span: the first task page holds only the newest
    // tasks, the week-old one is on a later page, and a single read of it fails.
    api.loadTasks.mockImplementation(async (_limit: number, o: { pageToken?: string | null }) =>
      o.pageToken === 't2'
        ? ok({ tasks: [OLD], next_page_token: 't3' } as TaskPage)
        : ok({ tasks: [STEP_A, STEP_B], next_page_token: 't2' } as TaskPage),
    )
    render(<TimelineLanesScreen view="span=7d&state=SUCCEEDED&profile=claude-code" onView={() => {}} />)
    await waitFor(() => expect(lane('task_old')).not.toBeNull())
    await waitFor(() => expect(lane('task_old')!.textContent).toContain('claude-code'))
    expect(lane('task_old')!.textContent).not.toMatch(/task not read/)
    expect(lane('task_b'), 'the State filter still applies').toBeNull()
    // OLD was created before the span's start, so the read stopped there.
    expect(api.loadTasks).toHaveBeenCalledTimes(2)
    expect(api.loadTask).not.toHaveBeenCalledWith('task_old')
  })

  it('stops the task read at the span\'s start, at TASK_PAGES_READ, or, on a re-read, past the oldest task that can change', () => {
    const since = NOW - 7 * 24 * HOUR
    expect(taskReadCovers([STEP_A], since, null)).toBe(false)
    expect(taskReadCovers([STEP_A, OLD], since + HOUR, null)).toBe(true)
    expect(taskReadCovers([], since, null)).toBe(true)
    const prior = new Map([STEP_A, STEP_B].map((t) => [t.id, t]))
    expect(taskReadCovers([STEP_A], since, prior), 'every task read has ended').toBe(true)
    const running = task('task_live', { state: 'RUNNING', created_at: ago(2 * 24 * 60) })
    prior.set(running.id, running)
    expect(taskReadCovers([STEP_A], since, prior), 'a running task is older than this page').toBe(false)
    expect(TASK_PAGES_READ).toBeGreaterThanOrEqual(SPAN_PAGES_READ)
  })

  it('the reach is the oldest attempt read, and null once the span is read', () => {
    const rows = [attempt('a', 1, 60, 30), attempt('b', 1, 600, 590)]
    expect(drawnReach({ rows, next: 'x' })).toBe(Date.parse(ago(600)))
    expect(drawnReach({ rows, next: null })).toBeNull()
  })
})

describe('RT-7b: what cannot change is not read again every minute', () => {
  it('keeps a settled workflow\'s read across a refresh', async () => {
    render(<TimelineLanesScreen view="span=7d" onView={() => {}} />)
    await waitFor(() => expect(api.loadWorkflow).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(document.querySelector('.tl-row.is-group[data-group="wf_named"] a')).not.toBeNull())
    fireEvent.click(screen.getByRole('button', { name: /^refresh/i }))
    await waitFor(() => expect(api.loadAttemptsPage.mock.calls.length).toBeGreaterThan(3))
    await new Promise((r) => setTimeout(r, 30))
    expect(api.loadWorkflow, 'a FAILED workflow whose steps have all ended').toHaveBeenCalledTimes(1)
  })

  it('a workflow with a step still running is not settled', () => {
    expect(workflowSettled({ status: 'ok', read: WF_READ })).toBe(true)
    const live = { ...WF_READ, tasks: [STEP_A, { ...STEP_B, state: 'RUNNING' }] } as WorkflowRead
    expect(workflowSettled({ status: 'ok', read: live })).toBe(false)
  })

  it('reads the task page in its summary view', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(api.loadTasks).toHaveBeenCalled())
    expect(api.loadTasks.mock.calls[0]![1]).toMatchObject({ view: 'summary' })
  })
})

describe('RT-22: the header, the zoom and the pan', () => {
  it('counts the lanes a filter leaves, of the lanes read', async () => {
    render(<TimelineLanesScreen view="span=7d&state=FAILED" onView={() => {}} />)
    await waitFor(() => expect(lane('task_b')).not.toBeNull())
    await waitFor(() => expect(countNote()).toMatch(/1 of 3 lanes/))
  })

  it('keeps one way back, to the span the zoom began from, however many times it zooms', async () => {
    const seen = vi.fn()
    render(<Routed initial="span=24h" seen={seen} />)
    await waitFor(() => expect(lane('task_a')).not.toBeNull())
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }))
    const backs = seen.mock.calls.map((c) => parseLanesView(c[0] as string).back)
    expect(backs).toHaveLength(3)
    expect(new Set(backs).size, 'every zoom keeps the first way back').toBe(1)
    expect(parseLanesView(backs[0]!).span).toBe('24h')
  })

  it('pans half a window earlier, and never later than now', async () => {
    const win = { since: NOW - 4 * HOUR, until: NOW }
    expect(panWindow(win, -1, NOW)).toEqual({ since: NOW - 6 * HOUR, until: NOW - 2 * HOUR })
    expect(panWindow(win, 1, NOW)).toEqual(win)
    const seen = vi.fn()
    render(<Routed initial="span=24h" seen={seen} />)
    await waitFor(() => expect(lane('task_a')).not.toBeNull())
    expect(screen.getByRole('button', { name: 'Later' }).hasAttribute('disabled')).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Earlier' }))
    const v = parseLanesView(seen.mock.calls.at(-1)![0] as string)
    expect(Date.parse(v.until!)).toBeLessThan(NOW - 11 * HOUR)
    expect(Date.parse(v.until!) - Date.parse(v.since!)).toBeCloseTo(24 * HOUR, -5)
  })
})

describe('RT-23 and RT-24: the inspector and the marks', () => {
  it('Escape closes the lane inspector', async () => {
    const seen = vi.fn()
    render(<Routed initial="span=7d&lane=task_a" seen={seen} />)
    await screen.findByRole('region', { name: /task_a/ })
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('region', { name: /task_a/ })).toBeNull())
    expect(parseLanesView(seen.mock.calls.at(-1)![0] as string).lane).toBeNull()
  })

  it('a mark takes focus and opens its lane', async () => {
    api.loadTaskEventsPage.mockImplementation(async (id: string) =>
      ok({ events: id === 'task_a' ? [{ event_id: 'e1', task_id: 'task_a', type: 'succeeded', at: ago(30), attempt_id: null, lease_id: null, generation: 1, detail: null }] : [], next_page_token: null }),
    )
    const seen = vi.fn()
    render(<Routed initial="span=7d" seen={seen} />)
    // The rows settle under their workflow's group first: a row re-mounts when it moves.
    await waitFor(() => expect(document.querySelector('.tl-row.is-group[data-group="wf_named"] a')).not.toBeNull())
    await new Promise((r) => setTimeout(r, 30))
    const mark = await waitFor(() => {
      const el = lane('task_a')?.querySelector<HTMLElement>('.tl-mkr[data-mark="succeeded"]')
      expect(el).toBeTruthy()
      return el!
    })
    expect(mark.tabIndex).toBe(0)
    expect(mark.getAttribute('aria-label')).toMatch(/succeeded/)
    fireEvent.keyDown(mark, { key: 'Enter' })
    expect(parseLanesView(seen.mock.calls.at(-1)![0] as string).lane).toBe('task_a')
  })

  it('says the task\'s attempt count beside the span\'s', async () => {
    render(<TimelineLanesScreen view="span=7d" onView={() => {}} />)
    await waitFor(() => expect(lane('task_old')).not.toBeNull())
    expect(lane('task_old')!.textContent).toContain('1 of 3 attempts in this span')
  })
})

describe('RT-25: a workflow is named as the Workflows list names it', () => {
  it('heads its group and its filter option with its label, the id as the title', async () => {
    render(<TimelineLanesScreen view="span=7d" onView={() => {}} />)
    const head = await waitFor(() => {
      const a = document.querySelector<HTMLAnchorElement>('.tl-row.is-group[data-group="wf_named"] a')
      expect(a).not.toBeNull()
      return a!
    })
    expect(head.textContent).toBe('PERM-2')
    expect(head.getAttribute('title')).toBe('wf_named')
    const option = document.querySelector<HTMLOptionElement>('.tl-wf-sel option[value="wf_named"]')!
    expect(option.textContent).toBe('PERM-2 · wf_named')
  })
})
