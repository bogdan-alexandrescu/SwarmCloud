// QAFIX4-TIMELINE, THE CURRENT STATE OF A FREE LANE AND THE STEPS A GROUP
// COUNTS (part of #1680; visual QA run 4, wf_ecd935541ced4a539540, the fixture
// build of main at fabe4d6c, 1440 and 390, light and dark):
//
//   N18 work-a-005  'report' (parked), e7d210b6 (queued) and 950b8e4a (ready)
//          were each three solid held-capacity pills and nothing else: no
//          hatching, no waiting line, though the legend above says parked is
//          hatched and free and waiting is a line. A park was drawn only from
//          a `parked` event, so a PARKED task whose event was not (yet) read
//          drew none; and the whole-life wait line ran through the middle of
//          the track, under the pills piled at 'now'. The lane's current state
//          is now drawn to 'now' on a rail beneath the pills, which keep their
//          layout (box comment 6314122402 waits on the owner).
//   N19 work-a-008  wf_audit_01 said '5 steps' over four step lanes: its
//          fifth step has no task yet. The header now says how many of its
//          steps have attempts in this span, and names the others.
//
// Every assertion names the mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowRead } from '../api'
import { buildLane } from '../lanes'
import { ledgerFixture } from '../outcomes.fixture'
import type { AttemptRow, AttemptsPage, Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
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

const { TimelineLanesScreen } = await import('../TimelineLanes')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const NOW = Date.now()
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()
const ENVS: CascadeEnv[] = [
  { width: 1440, theme: 'light' },
  { width: 1440, theme: 'dark' },
  { width: 390, theme: 'light' },
  { width: 390, theme: 'dark' },
]

function task(id: string, state: Task['state'], extra: Partial<Task> = {}): Task {
  return {
    id, tenant_id: 'eng', state, runner_profile: 'claude-code', resource_class: 'standard', provider: null,
    priority: 0, created_at: ago(60), updated_at: ago(5), started_at: ago(30), completed_at: null,
    submitted_by: 'operator@example.com', attempt_count: 3, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: null, step_id: null, depends_on: null, cancel_requested: false, repository_url: null,
    current_generation: 3, current_lease_id: null, next_eligible_at: null, ...extra,
  } as Task
}

// The fixture's three attempts, as run 4 drew them: 30, 19 and 9 minutes ago,
// the newest with no end written. Over 24 h each is a floor-width pill at 'now'.
function attempts(taskId: string): AttemptRow[] {
  const mk = (n: number, from: number, to: number | null) =>
    ({
      attempt_id: `att_${taskId}_${n}`, task_id: taskId, tenant_id: 'eng', generation: n, lease_id: `l${n}`,
      backend: 'cloud_run', execution_name: null, created_at: ago(from), started_at: ago(from), completed_at: to === null ? null : ago(to),
      exit_code: to === null ? null : 1, error: null, peak_rss_bytes: null, peak_disk_bytes: null, oom_near_miss: false, checkpoints: [],
      input_tokens: null, output_tokens: null, cache_read_input_tokens: null, cache_creation_input_tokens: null, cost_usd: null,
    }) as unknown as AttemptRow
  return [mk(3, 9, null), mk(2, 19, 15), mk(1, 30, 26)]
}

const step = (id: string, state: Task['state'], stepId: string) =>
  task(id, state, { workflow_id: 'wf_five', step_id: stepId, completed_at: state === 'SUCCEEDED' ? ago(20) : null })
const PLAN = step('task_plan', 'SUCCEEDED', 'plan')
const SCAN_A = step('task_scan_a', 'SUCCEEDED', 'scan-scripts')
const SCAN_B = step('task_scan_b', 'RUNNING', 'scan-terraform')
const REPORT = task('task_report', 'PARKED', { workflow_id: 'wf_five', step_id: 'report', park_reason: 'DEPENDENCY_INCOMPLETE' })
const QUEUED = task('task_e7d210b6', 'QUEUED', { current_generation: 0 })
const READY = task('task_950b8e4a', 'READY', { current_generation: 0 })
const TASKS = [PLAN, SCAN_A, SCAN_B, REPORT, QUEUED, READY]

const PAGE = {
  tenant_id: 'eng', read_at: new Date(NOW).toISOString(), next_page_token: null, coverage: { scope: 'page' },
  attempts: TASKS.flatMap((t) => attempts(t.id)),
} as unknown as AttemptsPage
const WF_READ = {
  workflow: {
    workflow_id: 'wf_five', state: 'RUNNING',
    steps: [
      { step_id: 'plan', task_id: 'task_plan' },
      { step_id: 'scan-scripts', task_id: 'task_scan_a' },
      { step_id: 'scan-terraform', task_id: 'task_scan_b' },
      { step_id: 'report', task_id: 'task_report' },
      // No task yet: the workflow has not reached it, as wf_audit_01's `publish`.
      { step_id: 'publish', task_id: null },
    ],
  },
  tasks: [PLAN, SCAN_A, SCAN_B, REPORT],
} as unknown as WorkflowRead

beforeEach(() => {
  api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
  api.loadAttemptsPage.mockResolvedValue(ok(PAGE))
  // Read, and no `parked` event on the page: run 4's fixture writes none.
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

const lane = (id: string) => document.querySelector<HTMLElement>(`.tl-row[data-lane="${id}"]`)
const px = (v: string | null): number => {
  const m = /^(-?[\d.]+)px$/.exec(v ?? '')
  if (m === null) throw new Error(`not a pixel length: ${JSON.stringify(v)}`)
  return Number(m[1])
}

/** The lane's current-state mark, once the lanes are drawn. */
async function current(id: string, kind: 'park' | 'wait'): Promise<HTMLElement> {
  return waitFor(() => {
    const el = lane(id)?.querySelector<HTMLElement>(`.tl-seg.is-now[data-seg="${kind}"]`) ?? null
    expect(el, `${id}: no current ${kind} drawn`).not.toBeNull()
    return el!
  })
}

/**
 * The rail is below the pills: the room under a centred pill in the track
 * holds the mark's bottom offset and height, so no pill at 'now' covers it.
 */
function clearOfPills(row: HTMLElement, mark: HTMLElement, env: CascadeEnv, at: string) {
  const pill = row.querySelector<HTMLElement>('.tl-seg.is-hold')!
  const trk = row.querySelector<HTMLElement>('.tl-trk')!
  expect(pill, `${at}: the lane's pills are still drawn`).not.toBeNull()
  const room = (px(painted(trk, 'height', env)) - px(painted(pill, 'height', env))) / 2
  expect(painted(mark, 'top', env), `${at}: top`).toBe('auto')
  expect(px(painted(mark, 'bottom', env)) + px(painted(mark, 'height', env)), `${at}: under the pills`).toBeLessThanOrEqual(room)
}

describe('N18 work-a-005: a free lane draws its current state to now, not as held capacity', () => {
  it('draws a PARKED task\'s current park from the task, when no parked event was read', () => {
    const lane = buildLane('task_report', REPORT, attempts('task_report'), [], NOW)
    const park = lane.segs.find((s) => s.kind === 'park' && s.current)
    // MUTATION: draw a park only from a `parked` event (as before) and there
    // is none: the lane is its three pills and nothing says it is parked.
    expect(park, 'no current park').toBeDefined()
    // MUTATION: end the task-derived park at its start instead of `now` and
    // the hatching stops short of 'now'.
    expect(park!.to).toBe(NOW)
    // MUTATION: start the park at `now` and it is a zero-length span.
    expect(park!.from).toBeLessThan(NOW)
    // MUTATION: start the park before the newest attempt began and a free
    // span is drawn inside a held one.
    expect(park!.from).toBeGreaterThanOrEqual(Date.parse(ago(9)))
  })

  it('marks a QUEUED or READY task\'s wait to now as its current state, and a RUNNING one has none', () => {
    for (const t of [QUEUED, READY]) {
      const l = buildLane(t.id, t, attempts(t.id), [], NOW)
      const wait = l.segs.find((s) => s.kind === 'wait' && s.current)
      // MUTATION: leave the current wait out of buildLane and this is red.
      expect(wait, `${t.state}: no current wait`).toBeDefined()
      expect(wait!.to).toBe(NOW)
    }
    // MUTATION: mark the wait current for every live task and a RUNNING lane
    // draws a 'waiting, free' line beside the attempt that holds capacity.
    const running = buildLane('task_scan_b', SCAN_B, attempts('task_scan_b'), [], NOW)
    expect(running.segs.filter((s) => s.current)).toHaveLength(0)
  })

  it('draws the PARKED lane\'s current park hatched, up to now, clear of the pills', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const park = await current('task_report', 'park')
    const row = lane('task_report')!
    // MUTATION: anchor the mark at its start (`left`) and a park that began
    // under the pills is a 0 px sliver short of now.
    expect(park.style.right).toBe('0%')
    // MUTATION: set both `left` and `right` on the current mark and its
    // floor width can no longer grow leftward from 'now'.
    expect(park.style.left).toBe('')
    // MUTATION: draw the current park with the pills' layout and it pushes
    // them left. Its three attempts, minutes apart at 24 h, are one counted
    // pill (QAFIX4-TIMELINE-2, c00), and the park is not among them.
    expect(row.querySelectorAll('.tl-seg.is-hold')).toHaveLength(1)
    expect(row.querySelector('.tl-seg.is-hold .tl-seg-n')?.textContent).toBe('3')
    // MUTATION: give the current park the `is-hold` class and it paints as
    // held capacity.
    expect(park.classList.contains('is-hold')).toBe(false)
    // MUTATION: title the current park as an attempt ('holds …') and its
    // tooltip says the opposite of the hatching.
    expect(park.getAttribute('title')).toMatch(/nothing held/)
    for (const env of ENVS) {
      const at = `${env.width} ${env.theme}`
      // MUTATION: drop `.tl-seg.is-park`'s hatching and the park reads as a box.
      expect(painted(park, 'background', env), `${at}: hatched`).toContain('repeating-linear-gradient')
      // MUTATION: delete the `.tl-seg.is-now` rule and the park is centred
      // in the track again, under the pills at 'now'.
      clearOfPills(row, park, env, at)
      // MUTATION: drop the floor and a park that began a minute ago is a
      // sub-pixel sliver at 24 h.
      expect(px(painted(park, 'min-width', env)), `${at}: floor`).toBeGreaterThanOrEqual(14)
    }
  })

  it('draws the QUEUED and READY lanes\' waiting line up to now, clear of the pills', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    for (const id of ['task_e7d210b6', 'task_950b8e4a']) {
      const wait = await current(id, 'wait')
      const row = lane(id)!
      // MUTATION: anchor at the start and the line ends short of now.
      expect(wait.style.right, id).toBe('0%')
      // MUTATION: give the current wait the `is-hold` class and the line
      // paints as a held-capacity pill.
      expect(wait.classList.contains('is-hold'), id).toBe(false)
      // MUTATION: title the current wait as an attempt and its tooltip says
      // it holds capacity.
      expect(wait.getAttribute('title'), id).toMatch(/nothing held/)
      // Neither lane draws its present as an attempt holding capacity: no
      // hold runs open to now (invariant 1: queued and ready hold nothing).
      // MUTATION: draw the newest attempt with no end as open on a QUEUED
      // task and it reads as held to now.
      expect(row.querySelectorAll('.tl-seg.is-hold.is-open'), id).toHaveLength(0)
      for (const env of ENVS) {
        const at = `${id} ${env.width} ${env.theme}`
        // The legend's waiting mark: a 2 px line in the faint ink.
        // MUTATION: draw the current wait as a park or a bar and these are red.
        expect(painted(wait, 'height', env), at).toBe('2px')
        expect(painted(wait, 'background', env), at).toBe('var(--text-faint)')
        clearOfPills(row, wait, env, at)
        expect(px(painted(wait, 'min-width', env)), `${at}: floor`).toBeGreaterThanOrEqual(14)
      }
    }
  })
})

describe('N19 work-a-008: a workflow group says how many of its steps the span draws', () => {
  it('says "4 of 5 steps have attempts in this span" and names the fifth', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const head = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.tl-row.is-group[data-group="wf_five"] .tl-lab')
      expect(el?.textContent).toMatch(/steps/)
      return el!
    })
    // MUTATION: print `${steps.length} steps` again and the header says 5
    // over four lanes.
    expect(head.textContent).toMatch(/4 of 5 steps have attempts in this span/)
    // MUTATION: print the plain `${n} steps` when k < n and '5 steps' sits
    // over four lanes again.
    expect(head.textContent).not.toMatch(/^\D*5 steps/)
    // MUTATION: count without naming and nothing says which step is missing.
    expect(head.textContent).toMatch(/publish/)
    // MUTATION: drop the `task_id === null` case from stepsGap's reason and
    // a step with no task reads as one whose attempt is outside the span.
    expect(head.textContent).toMatch(/not started/)
  })

  it('names a step whose task is drawn as an empty lane nowhere as having no lane', async () => {
    // The fifth step has a task, created up front and parked on its
    // dependencies with no attempt: the view draws it as an empty lane.
    const publish = task('task_publish', 'PARKED', {
      workflow_id: 'wf_five', step_id: 'publish', park_reason: 'DEPENDENCY_INCOMPLETE', attempt_count: 0, current_generation: 0, started_at: null,
    })
    api.loadWorkflow.mockResolvedValue(
      ok({
        workflow: { ...WF_READ.workflow, steps: [...WF_READ.workflow.steps.slice(0, 4), { step_id: 'publish', task_id: 'task_publish' }] },
        tasks: [...WF_READ.tasks, publish],
      } as unknown as WorkflowRead),
    )
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lane('task_publish'), 'the empty lane is drawn').not.toBeNull())
    const head = document.querySelector<HTMLElement>('.tl-row.is-group[data-group="wf_five"] .tl-lab')!
    // The count stays true: the fifth step has no attempt in this span.
    // MUTATION: count neverRan lanes into `ran` and the header says '5 steps'
    // over a step that never ran.
    expect(head.textContent).toMatch(/4 of 5 steps have attempts in this span/)
    // MUTATION: drop `!stepRan(s, empty)` from stepsGap and the header says
    // 'no lane: publish (no attempt yet)' directly above publish's lane.
    expect(head.querySelector('.tl-wf-gap'), 'no "no lane" line over a drawn lane').toBeNull()
    expect(head.textContent).not.toMatch(/no lane/)
  })

  it('says a step whose only attempt is outside the span has "no attempt in this span"', async () => {
    // The fifth step ran before the span: its task has attempts, none on the page.
    const publish = task('task_publish', 'SUCCEEDED', { workflow_id: 'wf_five', step_id: 'publish', attempt_count: 1, completed_at: ago(3000) })
    api.loadWorkflow.mockResolvedValue(
      ok({
        workflow: { ...WF_READ.workflow, steps: [...WF_READ.workflow.steps.slice(0, 4), { step_id: 'publish', task_id: 'task_publish' }] },
        tasks: [...WF_READ.tasks, publish],
      } as unknown as WorkflowRead),
    )
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const head = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.tl-row.is-group[data-group="wf_five"] .tl-lab')
      expect(el?.textContent).toMatch(/steps/)
      return el!
    })
    // MUTATION: draw an empty lane for every step with no row on the page
    // (the other form of N19) and the header would have to count it.
    expect(lane('task_publish'), 'no lane for an attempt outside the span').toBeNull()
    // MUTATION: count tasks with attempt_count > 0 as having attempts in the
    // span and the header says '5 steps' over four lanes.
    expect(head.textContent).toMatch(/4 of 5 steps have attempts in this span/)
    // MUTATION: word every task-backed gap 'no attempt yet' and a step that
    // ran yesterday reads as one that never ran.
    expect(head.textContent).toMatch(/publish \(no attempt in this span\)/)
  })

  it('keeps the plain count when every step has an attempt in the span', async () => {
    api.loadWorkflow.mockResolvedValue(
      ok({ ...WF_READ, workflow: { ...WF_READ.workflow, steps: WF_READ.workflow.steps.slice(0, 4) } } as unknown as WorkflowRead),
    )
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    const head = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.tl-row.is-group[data-group="wf_five"] .tl-lab')
      expect(el?.textContent).toMatch(/4 steps/)
      return el!
    })
    // MUTATION: always print 'N of M' and a whole workflow reads as partial.
    expect(head.textContent).not.toMatch(/of 4/)
  })
})
