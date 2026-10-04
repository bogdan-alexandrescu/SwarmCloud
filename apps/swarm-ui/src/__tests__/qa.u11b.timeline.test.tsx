/**
 * BROWSER QA U11b N16 (owner, 2026-10-04; live console at main 69416faf,
 * 1440x900): /timeline's lane gridlines (730/865/1000/...) did not line up
 * with the axis ticks (605/808/1012/1215). The gridlines were a CSS gradient
 * repeated every sixth of the track, a scale of their own; the ticks are
 * times. The gridlines are now drawn from the ticks' own positions
 * (`gridlineImage(axisLabels(...))`), set once on the chart.
 *
 * MUTATIONS: put the sixths gradient back on `.tl-trk`, or build the
 * gridlines from anything but the axis's ticks -- each turns a case red.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'

import STYLES from '../styles.css?raw'
import TIMELINE_CSS from '../styles/timeline.css?raw'
import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { AttemptRow, AttemptsPage, Task, TaskEvent, TaskEventsPage, TaskPage } from '../types'
import type { WorkflowRead } from '../api'
import { cascade } from './cssgate'

const api = vi.hoisted(() => ({
  loadAttemptsPage: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadTasks: vi.fn(),
  loadTask: vi.fn(),
  loadWorkflow: vi.fn(),
  loadOutcomes: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { TimelineLanesScreen, axisLabels, gridlineImage } = await import('../TimelineLanes')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const failed = <T,>(httpStatus: number, message: string): Result<T> => ({
  status: 'error',
  error: { kind: 'server', httpStatus, code: null, message } as never,
})

const NOW = Date.now()
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()

function task(id: string, extra: Partial<Task> = {}): Task {
  return {
    id, tenant_id: 'eng', state: 'SUCCEEDED', runner_profile: 'claude-code', resource_class: 'standard', provider: null,
    priority: 0, created_at: ago(300), updated_at: ago(100), started_at: ago(290), completed_at: ago(100),
    submitted_by: 'operator@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: null, step_id: null, depends_on: null, cancel_requested: false, repository_url: null,
    current_generation: 1, current_lease_id: null, ...extra,
  } as Task
}

function attempt(taskId: string, gen: number, from: number, to: number | null, extra: Partial<AttemptRow> = {}): AttemptRow {
  return {
    attempt_id: `att_${taskId}_${gen}`, task_id: taskId, tenant_id: 'eng', generation: gen, lease_id: `l${gen}`,
    backend: 'cloud_run', execution_name: null, created_at: ago(from), started_at: ago(from - 1),
    completed_at: to === null ? null : ago(to), exit_code: to === null ? null : 0, error: null,
    peak_rss_bytes: null, peak_disk_bytes: null, oom_near_miss: false, checkpoints: [], input_tokens: null,
    output_tokens: null, cache_read_input_tokens: null, cache_creation_input_tokens: null, cost_usd: null, ...extra,
  } as AttemptRow
}

function ev(taskId: string, type: string, min: number, gen: number | null, detail: Record<string, unknown> | null = null): TaskEvent {
  return { event_id: `${taskId}-${type}-${min}`, task_id: taskId, type, at: ago(min), attempt_id: null, lease_id: null, generation: gen, detail }
}

const TASKS: Task[] = [
  task('task_solo', { attempt_count: 2, current_generation: 2 }),
  task('task_plan', { workflow_id: 'wf_7c1e', step_id: 'plan' }),
  task('task_test', { workflow_id: 'wf_7c1e', step_id: 'test', runner_profile: 'codex', state: 'FAILED' }),
]
const NEVER_RAN = task('task_publish', { workflow_id: 'wf_7c1e', step_id: 'publish', state: 'CANCELLED', attempt_count: 0, started_at: null })

const ATTEMPTS: AttemptRow[] = [
  attempt('task_test', 1, 150, 110, { exit_code: 1 }),
  attempt('task_solo', 2, 200, 100),
  attempt('task_plan', 1, 240, 160),
  attempt('task_solo', 1, 280, 210, { exit_code: 70 }),
]

function page(attempts: AttemptRow[], next: string | null = null): AttemptsPage {
  return { tenant_id: 'eng', read_at: new Date(NOW).toISOString(), attempts, next_page_token: next, coverage: { scope: 'page' } } as AttemptsPage
}

const EVENTS: Record<string, TaskEvent[]> = {
  task_solo: [ev('task_solo', 'succeeded', 100, 2), ev('task_solo', 'generation_fenced', 205, 2, { expected_generation: 1, observed_generation: 2 }), ev('task_solo', 'running', 279, 1)],
  task_plan: [ev('task_plan', 'cancel_requested', 170, 1), ev('task_plan', 'succeeded', 160, 1)],
  task_test: [ev('task_test', 'failed', 110, 1)],
}

function eventsPage(events: TaskEvent[], next: string | null = null): TaskEventsPage {
  return { events, next_page_token: next }
}

beforeEach(() => {
  api.loadAttemptsPage.mockResolvedValue(ok(page(ATTEMPTS)))
  api.loadTaskEventsPage.mockImplementation(async (id: string) => ok(eventsPage(EVENTS[id] ?? [])))
  api.loadTasks.mockResolvedValue(ok({ tasks: TASKS } as TaskPage))
  api.loadTask.mockImplementation(async (id: string) => failed(404, `no ${id}`))
  api.loadWorkflow.mockResolvedValue(ok({ workflow: { workflow_id: 'wf_7c1e', state: 'FAILED', steps: [{}, {}, {}] }, tasks: [TASKS[1]!, TASKS[2]!, NEVER_RAN] } as unknown as WorkflowRead))
  api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const SHEET = `${TIMELINE_CSS}\n${STYLES}`
const HOUR = 3_600_000

/** The percentages a gradient draws a 1px line at: each colour stop that is not transparent. */
function linesIn(image: string): number[] {
  return [...image.matchAll(/var\(--line-soft\) ([\d.]+)%/g)].map((m) => Number(m[1]))
}

describe('N16: a lane gridline is a tick', () => {
  it('builds the gridlines from the ticks\' own positions', () => {
    const until = new Date(2026, 9, 3, 18, 30, 0).getTime()
    const ticks = axisLabels(until - 24 * HOUR, until, until)
    const lines = linesIn(gridlineImage(ticks))
    expect(lines.length).toBe(ticks.length)
    ticks.forEach((t, i) => expect(lines[i]).toBeCloseTo(t.pct, 3))
  })

  it('draws a lane track\'s gridlines from the chart\'s tick image, not a repeat of its own', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const tl = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.tl')
      expect(el?.querySelector('[data-lane]')).toBeTruthy()
      return el!
    })
    const image = tl.style.getPropertyValue('--tl-grid')
    const ticks = [...tl.querySelectorAll<HTMLElement>('.tl-axis .tl-tk:not(.is-now)')].map((t) => Number.parseFloat(t.style.left))
    expect(ticks.length).toBeGreaterThan(1)
    const lines = linesIn(image)
    expect(lines.length).toBe(ticks.length)
    ticks.forEach((t, i) => expect(lines[i]).toBeCloseTo(t, 3))
    const track = tl.querySelector('[data-lane] .tl-trk') ?? tl.querySelector('.tl-row:not(.tl-axis):not(.is-group) .tl-trk')!
    expect(cascade(SHEET, track, ['background-image', 'background'], { width: 1440 }).winner?.value).toBe('var(--tl-grid, none)')
    expect(cascade(SHEET, track, 'background-size', { width: 1440 }).winner?.value ?? 'auto').not.toMatch(/100% \/ 6/)
  })
})
