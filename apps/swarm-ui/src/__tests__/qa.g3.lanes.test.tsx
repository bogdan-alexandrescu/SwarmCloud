/**
 * BROWSER QA G3 (2026-10-07, swarm.example.com at 1440 and 390): Timeline › Lanes.
 *
 *   G3-07  15 of 49 workflow reads answered 429, all asked at once, and the
 *          group subtitle printed the raw route: `steps — (/v1/workflows/
 *          wf_553d… answered 429)`, cut to `steps — (/v1/workflows/wf_55…`.
 *   G3-08  The strip's "Decided counts cover 20:57–20:57" sat over counts the
 *          route had read from 22:00, and "Outcomes for this span ›" did not
 *          say the two pages read different ranges.
 *
 * MUTATIONS, each turns a case red: ask every workflow at once; return a 429
 * without asking again; print `workflow.why` with the route in the subtitle;
 * print the lanes' own window as the strip's range.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import { alignedWords, instantLabel, type Outcomes } from '../outcomes'
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

const { TimelineLanesScreen, WORKFLOW_READS_AT_ONCE, readQueue, withBackoff } = await import('../TimelineLanes')
const { WORKFLOW_IDS_PER_READ } = await import('../api')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const refused = <T,>(httpStatus: number): Result<T> => ({
  status: 'error',
  error: { kind: 'server', httpStatus, code: null, message: 'slow down' } as never,
})

const NOW = Date.now()
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()

function task(id: string, wf: string): Task {
  return {
    id, tenant_id: 'eng', state: 'SUCCEEDED', runner_profile: 'mock', resource_class: 'standard', provider: null,
    priority: 0, created_at: ago(300), updated_at: ago(100), started_at: ago(290), completed_at: ago(100),
    submitted_by: 'operator@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: wf, step_id: 'plan', depends_on: null, cancel_requested: false, repository_url: null,
    current_generation: 1, current_lease_id: null,
  } as Task
}

function attempt(taskId: string, i: number): AttemptRow {
  return {
    attempt_id: `att_${taskId}`, task_id: taskId, tenant_id: 'eng', generation: 1, lease_id: `l${i}`,
    backend: 'cloud_run', execution_name: null, created_at: ago(200 - i), started_at: ago(199 - i),
    completed_at: ago(150 - i), exit_code: 0, error: null, peak_rss_bytes: null, peak_disk_bytes: null,
    oom_near_miss: false, checkpoints: [], input_tokens: null, output_tokens: null, cache_read_input_tokens: null,
    cache_creation_input_tokens: null, cost_usd: null,
  } as AttemptRow
}

const WFS = Array.from({ length: 10 }, (_, i) => `wf_${String(i).padStart(4, '0')}`)
const TASKS = WFS.map((wf, i) => task(`task_${i}`, wf))

// Three steps over the one lane each workflow draws: since QAFIX4 work-a-008
// the header counts the steps with an attempt in the span, not the step list.
const HEAD = '1 of 3 steps have attempts in this span · succeeded'

function workflowRead(wf: string): WorkflowRead {
  return { workflow: { workflow_id: wf, state: 'SUCCEEDED', steps: [{}, {}, {}] }, tasks: [] } as unknown as WorkflowRead
}

beforeEach(() => {
  api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
  api.loadAttemptsPage.mockResolvedValue(
    ok({ tenant_id: 'eng', read_at: new Date(NOW).toISOString(), attempts: TASKS.map((t, i) => attempt(t.id, i)), next_page_token: null, coverage: { scope: 'page' } } as AttemptsPage),
  )
  api.loadTaskEventsPage.mockResolvedValue(ok({ events: [], next_page_token: null }))
  api.loadTasks.mockResolvedValue(ok({ tasks: TASKS } as TaskPage))
  api.loadTask.mockResolvedValue(refused(404))
  api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('G3-07: workflow reads, at most four at once, a 429 asked again', () => {
  it('runs at most `limit` reads at once, in order', async () => {
    const run = readQueue(2)
    let active = 0
    let most = 0
    const order: number[] = []
    const gates: Array<() => void> = []
    const reads = [0, 1, 2, 3, 4].map((i) =>
      run(async () => {
        active += 1
        most = Math.max(most, active)
        order.push(i)
        await new Promise<void>((r) => gates.push(r))
        active -= 1
        return i
      }),
    )
    while (order.length < 5) {
      await Promise.resolve()
      gates.shift()?.()
      await new Promise((r) => setTimeout(r, 0))
    }
    for (const g of gates) g()
    expect(await Promise.all(reads)).toEqual([0, 1, 2, 3, 4])
    expect(most).toBe(2)
    expect(order).toEqual([0, 1, 2, 3, 4])
  })

  it('asks a refused read again after each wait, and returns any other answer as it came', async () => {
    const waited: number[] = []
    const sleep = async (ms: number) => {
      waited.push(ms)
    }
    const read = vi.fn<() => Promise<Result<number>>>()
    read.mockResolvedValueOnce(refused(429)).mockResolvedValueOnce(refused(429)).mockResolvedValueOnce(ok(7))
    expect(await withBackoff(read, [10, 20, 40], sleep)).toEqual(expect.objectContaining({ status: 'ok', data: 7 }))
    expect(waited).toEqual([10, 20])
    const gone = vi.fn<() => Promise<Result<number>>>().mockResolvedValue(refused(404))
    expect((await withBackoff(gone, [10], sleep)).status).toBe('error')
    expect(gone).toHaveBeenCalledTimes(1)
    const always = vi.fn<() => Promise<Result<number>>>().mockResolvedValue(refused(429))
    await withBackoff(always, [1, 2], sleep)
    expect(always).toHaveBeenCalledTimes(3)
  })

  it('asks for every workflow the lanes name in one read, not one each (live QA CH-6)', async () => {
    api.loadWorkflow.mockImplementation(async (wf: string) => ok(workflowRead(wf)))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(document.querySelectorAll('.tl-row.is-group a').length).toBe(WFS.length))
    // Before CH-6: one `GET /v1/workflows/{id}` per workflow, 10 here.
    expect(api.loadWorkflowsById).toHaveBeenCalledTimes(1)
    expect([...api.loadWorkflowsById.mock.calls[0]![0]].sort()).toEqual(WFS)
    expect(api.loadWorkflowsById.mock.calls.flatMap((c) => [...c[0]])).toHaveLength(WFS.length)
  })

  it('asks in chunks of the route\'s cap, at most four reads in flight', async () => {
    const many = Array.from({ length: WORKFLOW_IDS_PER_READ * 2 + 3 }, (_, i) => `wf_m${String(i).padStart(4, '0')}`)
    const tasks = many.map((wf, i) => task(`task_m${i}`, wf))
    api.loadTasks.mockResolvedValue(ok({ tasks } as TaskPage))
    api.loadAttemptsPage.mockResolvedValue(
      ok({ tenant_id: 'eng', read_at: new Date(NOW).toISOString(), attempts: tasks.map((t, i) => attempt(t.id, i)), next_page_token: null, coverage: { scope: 'page' } } as AttemptsPage),
    )
    let active = 0
    let most = 0
    const via = byIdVia(api.loadWorkflow)
    api.loadWorkflow.mockImplementation(async (wf: string) => ok(workflowRead(wf)))
    api.loadWorkflowsById.mockImplementation(async (ids: readonly string[]) => {
      active += 1
      most = Math.max(most, active)
      await new Promise((r) => setTimeout(r, 5))
      active -= 1
      return via(ids)
    })
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(api.loadWorkflowsById.mock.calls.flatMap((c) => [...c[0]])).toHaveLength(many.length))
    const sizes = api.loadWorkflowsById.mock.calls.map((c) => c[0].length)
    expect(Math.max(...sizes)).toBeLessThanOrEqual(WORKFLOW_IDS_PER_READ)
    expect(sizes.length).toBe(3)
    expect(new Set(api.loadWorkflowsById.mock.calls.flatMap((c) => [...c[0]])).size).toBe(many.length)
    expect(most).toBeLessThanOrEqual(WORKFLOW_READS_AT_ONCE)
    expect(WORKFLOW_READS_AT_ONCE).toBe(4)
    expect(WORKFLOW_IDS_PER_READ).toBe(50)
    // Every chunk answered before the next case starts counting.
    await waitFor(() => expect(api.loadWorkflow).toHaveBeenCalledTimes(many.length))
  })

  it('reads each workflow on its own where the API does not take ids', async () => {
    api.loadWorkflow.mockImplementation(async (wf: string) => ok(workflowRead(wf)))
    api.loadWorkflowsById.mockResolvedValue(ok({ batched: false, reads: new Map(), missing: [] }))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(document.querySelectorAll('.tl-row.is-group a').length).toBe(WFS.length))
    await waitFor(() => expect(api.loadWorkflow).toHaveBeenCalledTimes(WFS.length))
    await waitFor(() =>
      expect(document.querySelector(`.tl-row.is-group[data-group="${WFS[0]}"] small`)!.textContent).toBe(HEAD),
    )
  })

  it('says a read still refused after its retries is rate-limited, with the route in the title only, and retries it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.loadWorkflowsById.mockResolvedValue(refused(429))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(api.loadWorkflowsById).toHaveBeenCalled())
    // The three waits, 1 + 3 + 9 s, each a step of the fake clock.
    for (let i = 0; i < 4; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000)
      })
    }
    const head = await waitFor(() => {
      const el = document.querySelector<HTMLElement>(`.tl-row.is-group[data-group="${WFS[0]}"] .tl-wf-unread`)
      expect(el).not.toBeNull()
      return el!
    })
    expect(head.textContent).toBe('steps not read: rate-limited · retry')
    expect(head.textContent).not.toContain('/v1/workflows')
    expect(head.title).toBe(`/v1/workflows?ids=${WFS[0]} answered 429`)
    expect(api.loadWorkflowsById, 'the 429 was asked again before it was said').toHaveBeenCalledTimes(4)
    api.loadWorkflow.mockImplementation(async (wf: string) => ok(workflowRead(wf)))
    api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
    fireEvent.click(head.querySelector('button')!)
    await waitFor(() =>
      expect(document.querySelector(`.tl-row.is-group[data-group="${WFS[0]}"] small`)!.textContent).toBe(HEAD),
    )
    expect(api.loadWorkflowsById.mock.calls.at(-1)![0]).toEqual([WFS[0]])
  })
})

describe('G3-08: the strip says the range its counts cover, and the lanes’ beside it', () => {
  function aligned(): Outcomes {
    const d = ledgerFixture()
    d.requested = { ...d.requested, span: '24h' }
    d.bucket = 'hour'
    d.generated_at = new Date(NOW).toISOString()
    d.until = d.generated_at
    d.since = new Date(NOW - 23 * 3_600_000).toISOString()
    return d
  }

  it('prints the ledger’s aligned range for the decided counts, and both ranges on the link between the pages', async () => {
    api.loadWorkflow.mockImplementation(async (wf: string) => ok(workflowRead(wf)))
    const d = aligned()
    api.loadOutcomes.mockResolvedValue(ok(d))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const range = await waitFor(() => {
      const el = document.querySelector('.tl-strip-range')
      expect(el).not.toBeNull()
      return el!
    })
    expect(range.textContent).toBe(`${instantLabel(d.since, d.tz)} → now`)
    expect(document.querySelector('.tl-strip')!.textContent).toContain(`${alignedWords(d)} as on Outcomes`)
    expect(document.querySelector('.tl-lanes-range')).not.toBeNull()
    const link = [...document.querySelectorAll<HTMLAnchorElement>('.tl-strip a')].find((a) => a.textContent === 'Outcomes for this span ›')!
    expect(link.title).toContain(`Outcomes reads ${instantLabel(d.since, d.tz)} → now`)
    expect(link.title).toContain('these lanes read')
    expect(document.querySelector<HTMLAnchorElement>('.tl-pages a[href="/timeline/outcomes"]')!.title).toBe(link.title)
  })
})
