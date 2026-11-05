/**
 * THE WORKFLOW PAGE'S LINEAGE AND ITS STEP CARD (the owner's pick of the
 * workflow-graph mock-ups, direction B, 2026-11-05): the lineage strip above
 * the grid on pick and at the top of the step's sheet on a phone, the head's
 * tally counting a parked step as parked, and no Stop on a parked step that was
 * never attempted.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  cancelWorkflow: vi.fn(),
  loadTask: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen } = await import('../Workflows')

const T0 = Date.parse('2026-11-03T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-600),
    updated_at: iso(-60),
    started_at: null,
    completed_at: null,
    submitted_by: 'priya@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_build',
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: null,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: null,
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

function workflow(id: string, state: TaskState, steps: WorkflowStep[]): Workflow {
  return {
    workflow_id: id,
    state,
    tenant_id: 'eng',
    stored_state: state,
    state_source: 'derived',
    rollup: { state, complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: steps.length },
    created_at: iso(-52 * 60),
    updated_at: iso(-60),
    submitted_by: 'priya@example.com',
    priority: 0,
    on_step_failure: 'continue',
    cancel_requested: false,
    steps,
  }
}

/**
 * Two stages of two features, and a merge held by two owner questions:
 * build-login done, build-search failed, review-login running, review-search
 * not started, merge parked on the questions.
 */
function fixture(): WorkflowBoard {
  const w = workflow('wf_build', 'RUNNING', [
    step('build-login', [], { task_id: 't_bl' }),
    step('build-search', [], { task_id: 't_bs' }),
    step('review-login', ['build-login'], { task_id: 't_rl' }),
    step('review-search', ['build-search']),
    step('merge', ['review-login', 'review-search'], { task_id: 't_m' }),
  ])
  const held = {
    reason: 'HUMAN_REQUIRED',
    park_phase: 'owner_questions',
    owner_questions: {
      unreadable: 0,
      questions: [
        { task_id: 't_bl', step_id: 'build-login', question_index: 0, question: 'Which theme?' },
        { task_id: 't_bl', step_id: 'build-login', question_index: 1, question: 'Which font?' },
      ],
    },
  }
  const tasks = new Map<string, Task>([
    ['t_bl', task('t_bl', 'SUCCEEDED', { started_at: iso(-50 * 60), completed_at: iso(-46 * 60), step_id: 'build-login' })],
    ['t_bs', task('t_bs', 'FAILED', { started_at: iso(-50 * 60), completed_at: iso(-45 * 60), last_error: 'exit 1: 3 tests failing', step_id: 'build-search' })],
    ['t_rl', task('t_rl', 'RUNNING', { started_at: iso(-5 * 60), step_id: 'review-login' })],
    ['t_m', task('t_m', 'PARKED', { park_reason: 'HUMAN_REQUIRED', blocked_by: [held], step_id: 'merge' })],
  ])
  return { workflows: [w], taskById: tasks, statesDetail: null }
}

function serveOwn(board: WorkflowBoard): void {
  api.loadWorkflow.mockImplementation(async (id: string) => {
    const w = board.workflows.find((x) => x.workflow_id === id)
    if (w === undefined) return { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no such workflow' } }
    const tasks = w.steps.map((st) => (st.task_id ? board.taskById?.get(st.task_id) : undefined)).filter((t) => t !== undefined)
    return { status: 'ok', data: { workflow: w, tasks }, fetchedAt: T0 }
  })
}

const realWidth = window.innerWidth
function atWidth(px: number): void {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: px })
}

/**
 * build-login done, build-search failed, review-login running; review-search
 * parked behind build-search and never attempted; merge parked on a person
 * after an attempt.
 */
function lineageFixture(): WorkflowBoard {
  const board = fixture()
  const w = board.workflows[0]!
  const steps = w.steps.map((s) => (s.step_id === 'review-search' ? { ...s, task_id: 't_rs', input_from: { 'build-search': 'search.diff' } } : s))
  const tasks = new Map(board.taskById!)
  tasks.set('t_rs', task('t_rs', 'PARKED', { park_reason: 'DEPENDENCY_INCOMPLETE', attempt_count: 0, step_id: 'review-search' }))
  return { workflows: [{ ...w, steps }], taskById: tasks, statesDetail: null }
}

function media(phone: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('720'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  atWidth(1440)
  media(false)
  const board = lineageFixture()
  api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: board, fetchedAt: T0 } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  serveOwn(board)
  api.loadTask.mockImplementation(async (id: string) => {
    const t = board.taskById?.get(id)
    return t === undefined
      ? { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no such task' } }
      : { status: 'ok', data: t, fetchedAt: T0 }
  })
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})
afterEach(() => {
  atWidth(realWidth)
  vi.unstubAllGlobals()
})

async function page(): Promise<HTMLElement> {
  render(<WorkflowsScreen view="wf=wf_build" />)
  return waitFor(() => {
    const grid = document.querySelector<HTMLElement>('.wi[data-screen="workflow"] .wi-main .sg')
    expect(grid, 'no stage grid in the left pane').toBeTruthy()
    return grid!
  })
}

const chip = (stepId: string) => document.querySelector<HTMLButtonElement>(`.sg [data-step="${stepId}"]`)
const dock = () => document.querySelector<HTMLElement>('.wi-dock')

describe('the workflow page: lineage and the step card', () => {
  it('counts a parked step as parked in the head and its stage, as its chip draws it, not as waiting', async () => {
    await page()
    const tally = within(document.querySelector<HTMLElement>('.wfp-top')!).getByLabelText('Steps by state')
    // review-search parked behind its dependency and merge parked on a person: both drawn parked, both counted parked.
    expect(tally.querySelector('.wfp-t.is-waiting')!.textContent).toContain('0 waiting')
    expect(tally.querySelector('.wfp-t.is-parked')!.textContent).toBe('2 parked')
    expect(chip('review-search')!.className).toContain('is-parked')
    expect(document.querySelector('.sg-row[data-stage="review"]')!.getAttribute('aria-label')).toBe('review: 0 done, 0 failed, 1 running, 0 waiting, 1 parked')
  })

  it('offers no Stop on a parked step never attempted, and keeps it on one that ran an attempt', async () => {
    await page()
    fireEvent.click(chip('review-search')!)
    await waitFor(() => expect(dock()!.getAttribute('aria-label')).toBe('Step review-search'))
    expect(dock()!.querySelector('[aria-label="Stop step"]')).toBeNull()
    fireEvent.click(chip('merge')!)
    await waitFor(() => expect(dock()!.getAttribute('aria-label')).toBe('Step merge'))
    expect(dock()!.querySelector('[aria-label="Stop step"]')).toBeTruthy()
  })

  it('draws the lineage strip above the grid on pick: why it cannot start, in red, and the file that will not come', async () => {
    const grid = await page()
    // At rest: one line, the failure first.
    const rest = grid.querySelector<HTMLElement>('.lg-strip.is-rest')!
    expect(rest.querySelectorAll('.lg-rest')).toHaveLength(1)
    expect(rest.textContent).toMatch(/build-searchfailed→review-searchcannot start\+1 more$/)
    fireEvent.click(chip('review-search')!)
    const strip = await waitFor(() => {
      const el = grid.querySelector<HTMLElement>('.lg-strip:not(.is-rest)')
      expect(el).toBeTruthy()
      return el!
    })
    expect(strip.compareDocumentPosition(grid.querySelector('.sg-grid')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const head = strip.querySelector('.lg-self .lg-head')!
    expect(head.textContent).toBe('cannot start: build-search failed')
    expect(head.className).toContain('is-bad')
    expect(strip.querySelector('.lg-side.is-up')!.textContent).toContain('search.diff not staged: build-search failed')
    expect(strip.querySelector('.lg-side.is-down')!.textContent).toContain('merge')
    // The chip says it too.
    expect(chip('review-search')!.querySelector('.lg-line-part.is-blocked')!.textContent).toBe('blocked: build-search failed')
  })

  it("stacks the strip at the top of the step's sheet on a phone, and names the step only in the sheet's head", async () => {
    media(true)
    atWidth(390)
    const grid = await page()
    expect(grid.querySelector('.lg-strip')).toBeNull()
    fireEvent.click(chip('review-login')!)
    const strip = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.lg-strip.is-stacked')
      expect(el).toBeTruthy()
      return el!
    })
    expect(strip.querySelector('.lg-self')).toBeNull()
    expect(within(strip).queryByText('review-login')).toBeNull()
    expect(strip.querySelector('.lg-side.is-up')!.textContent).toContain('build-login')
    expect(strip.querySelector('.lg-side.is-down')!.textContent).toContain('merge')
  })
})
