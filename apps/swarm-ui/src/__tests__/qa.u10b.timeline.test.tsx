/**
 * BROWSER QA U10b (owner, 2026-10-04; live console, 1440x900 light and dark):
 * the Timeline's Lanes page.
 *
 *   D4   At 7d/14d/30d/90d every axis tick read "00:00": the label dropped the
 *        date exactly on the midnight ticks. A day tick says its date ("Oct 2").
 *   D5   At 24h the last tick and "now" overprinted ("1now0"): a tick that
 *        close to now is not drawn.
 *   D9   Grouped by profile, the axis said "0 workflows · 0 standalone" over 25
 *        lanes: it counts by the grouping in use ("25 lanes in 3 profiles").
 *   D10  A clicked lane's summary and "Open in Agents" were drawn at the foot of
 *        the page, 11,000px down: it opens right under the lane.
 *   D37  The footer said "since 19:09, until 19:09": it carries the dates.
 *
 * MUTATIONS: restore `clock(t, wide && hour === 0 ? false : wide)`, drop the
 * now-gap filter, count only workflow blocks, render <LaneDetail> after the
 * footer, or pass `days > 1` back to the footer's clock -- each turns a case red.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { AttemptRow, AttemptsPage, Task, TaskEvent, TaskEventsPage, TaskPage } from '../types'
import type { WorkflowRead } from '../api'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

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

const { TimelineLanesScreen, axisLabels, laneSummary } = await import('../TimelineLanes')
const { groupLanes } = await import('../lanes')

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

const lane = (id: string) => document.querySelector(`[data-lane="${id}"]`) as HTMLElement
const HOUR = 3_600_000
const DAY = 24 * HOUR

describe('D4: a day tick says its date', () => {
  for (const days of [7, 14, 30, 90]) {
    it(`labels the ${days}d axis with dates, never a row of 00:00`, () => {
      const until = Date.now()
      const labels = axisLabels(until - days * DAY, until, until).map((l) => l.label)
      expect(labels.length).toBeGreaterThan(2)
      for (const l of labels) expect(l, labels.join(' | ')).toMatch(/^[A-Z][a-z]{2} \d{1,2}$/)
      expect(new Set(labels).size).toBe(labels.length)
    })
  }

  it('labels hours inside a day, and a midnight tick inside a day by its date', () => {
    const midnight = new Date(2026, 9, 2, 0, 0, 0).getTime()
    const labels = axisLabels(midnight - 12 * HOUR, midnight + 11 * HOUR, 0).map((l) => l.label)
    expect(labels).toContain('Oct 2')
    expect(labels.filter((l) => /^\d\d:\d\d$/.test(l)).length).toBeGreaterThanOrEqual(2)
  })
})

describe('D5: the last tick never overprints now', () => {
  it('drops a tick that sits within the now label of the right edge', () => {
    // A 24h window whose end lands 30 minutes after a 3-hour tick: the owner's
    // "18:00" 10px left of "now".
    const until = new Date(2026, 9, 3, 18, 30, 0).getTime()
    const ticks = axisLabels(until - DAY, until, until)
    expect(ticks.length).toBeGreaterThanOrEqual(3)
    for (const t of ticks) expect(t.pct, `${t.label} at ${t.pct.toFixed(1)}%`).toBeLessThanOrEqual(94)
    // With no now label drawn (a window in the past), the same tick stays.
    const past = axisLabels(until - DAY, until, until + 7 * DAY)
    expect(past.some((t) => t.pct > 94)).toBe(true)
  })
})

describe('D9: the axis counts by the grouping in use', () => {
  it('says lanes in profiles when grouped by profile', () => {
    const mk = (id: string, profile: string, wf: string | null) =>
      ({ taskId: id, task: task(id, { runner_profile: profile, workflow_id: wf }), start: 0, neverRan: false }) as never
    const ls = [mk('a', 'claude-code', 'wf_1'), mk('b', 'claude-code', null), mk('c', 'codex', 'wf_1'), mk('d', 'gemini', null)]
    expect(laneSummary(groupLanes(ls, 'profile'), 'profile')).toBe('4 lanes in 3 profiles')
    expect(laneSummary(groupLanes(ls, 'workflow'), 'workflow')).toBe('1 workflow · 2 standalone')
    expect(laneSummary(groupLanes(ls, 'none'), 'none')).toBe('4 lanes')
  })

  it('draws that count on the profile-grouped page', async () => {
    render(<TimelineLanesScreen view="by=profile" onView={() => {}} />)
    await waitFor(() => expect(lane('task_solo')).not.toBeNull())
    expect(document.querySelector('.tl-axis .tl-lab small')!.textContent).toMatch(/^\d+ lanes in 2 profiles$/)
  })
})

describe('D10: a clicked lane opens under itself', () => {
  it('draws the summary right after the lane row, inside the chart', async () => {
    render(<TimelineLanesScreen view="lane=task_solo" onView={() => {}} />)
    const detail = await screen.findByRole('region', { name: /task_solo/ })
    expect(lane('task_solo').nextElementSibling).toBe(detail)
    expect(detail.closest('.tl')).not.toBeNull()
  })
})

describe('D37: the footer carries dates', () => {
  it('says since and until with their dates on a 24h span', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(document.querySelector('.tl-cov')).not.toBeNull())
    expect(document.querySelector('.tl-cov')!.textContent).toMatch(/since [A-Z][a-z]{2} \d{1,2} \d\d:\d\d, until [A-Z][a-z]{2} \d{1,2} \d\d:\d\d/)
  })
})

describe('D10: a stale read dims the lanes, not the open summary', () => {
  it('leaves the summary drawn inside the chart at full strength', () => {
    document.body.innerHTML =
      '<div class="tl-chart is-stale"><div class="tl"><div class="tl-row"></div><section class="tl-detail"></section></div></div>'
    const env: CascadeEnv = { width: 1440 }
    expect(painted(document.querySelector('.tl-row')!, 'opacity', env)).toBe('0.5')
    expect(painted(document.querySelector('.tl-detail')!, 'opacity', env)).toBeNull()
  })
})
