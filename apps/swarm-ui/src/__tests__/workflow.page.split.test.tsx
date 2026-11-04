/**
 * ONE WORKFLOW'S PAGE AS TWO PANES (the owner's pick of 2026-11-03, the Graph
 * Work redesign; frames graph-workflow-*): the head with state, steps, spend
 * and age and its tally line, the Needs-you row, the Graph / Table / Steps
 * over time / Changes switch with the Graph as the stage grid, the merge strip
 * and the live browsers under it, and the docked inspector -- the workflow's
 * Overview at rest, a picked step's agent with up and down walking stages,
 * folded at 1280. The open step is never drawn over the stages any more.
 */
import STYLES from '../styles.css?raw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  cancelWorkflow: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen, STAGE_ANY_STATE } = await import('../Workflows')

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

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  atWidth(1440)
  api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: fixture(), fetchedAt: T0 } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  serveOwn(fixture())
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})
afterEach(() => atWidth(realWidth))

async function page(view = 'wf=wf_build'): Promise<HTMLElement> {
  render(<WorkflowsScreen view={view} />)
  return waitFor(() => {
    const grid = document.querySelector<HTMLElement>('.wi[data-screen="workflow"] .wi-main .sg')
    expect(grid, 'no stage grid in the left pane').toBeTruthy()
    return grid!
  })
}

const chip = (stepId: string) => document.querySelector<HTMLButtonElement>(`.sg [data-step="${stepId}"]`)
const dock = () => document.querySelector<HTMLElement>('.wi-dock')

describe('one workflow as two panes', () => {
  it('draws the stage grid in the left pane and the Overview in the inspector, and no canvas or step card over the stages', async () => {
    await page()
    expect(document.querySelector('.wf-canvas')).toBeNull()
    expect(document.querySelector('.wf-panel')).toBeNull()
    expect(document.querySelector('.wf-split')).toBeNull()
    const ov = dock()!
    expect(ov.getAttribute('aria-label')).toBe('Workflow overview')
    const o = within(ov)
    expect(o.getByText('Spent')).toBeTruthy()
    expect(o.getByText('Pull requests')).toBeTruthy()
    expect(o.getByText('On a failed step')).toBeTruthy()
    expect(o.getByText('keep going')).toBeTruthy()
    // The usage read came back empty: earlier attempts are not known, never "none".
    expect(o.getByText(/earlier attempts not known/)).toBeTruthy()
    // Policy and the id and reads sit behind toggles.
    expect(o.getByText('Policy').closest('summary')).toBeTruthy()
    expect(o.getByText('Workflow id and reads').closest('summary')).toBeTruthy()
    expect(o.getByRole('button', { name: /^Stop/ })).toBeTruthy()
  })

  it('heads the page with state, steps, age and the tally counted from the steps it read', async () => {
    await page()
    const head = document.querySelector<HTMLElement>('.wfp-top')!
    expect(head.textContent).toContain('5 steps')
    expect(head.textContent).toContain('52m old')
    const tally = within(head).getByLabelText('Steps by state')
    expect(tally.textContent).toContain('1 running')
    expect(tally.textContent).toContain('1 done')
    expect(tally.textContent).toContain('1 failed')
    // review-search not started and the parked merge both wait.
    expect(tally.textContent).toContain('2 waiting')
    expect(tally.textContent).toContain('0 browsers')
  })

  it('opens a picked chip in the inspector and keeps the grid where it was; up and down walk the stages', async () => {
    const grid = await page()
    fireEvent.click(chip('build-login')!)
    await waitFor(() => expect(dock()!.getAttribute('aria-label')).toBe('Step build-login'))
    // The same grid node: the left pane did not move or remount.
    expect(document.querySelector('.wi-main .sg')).toBe(grid)
    const down = within(dock()!).getByRole('button', { name: /^Down a stage: review-login/ })
    expect(within(dock()!).getByRole('button', { name: 'No stage above' })).toHaveProperty('disabled', true)
    fireEvent.click(down)
    await waitFor(() => expect(dock()!.getAttribute('aria-label')).toBe('Step review-login'))
    fireEvent.click(within(dock()!).getByRole('button', { name: /^Up a stage: build-login/ }))
    await waitFor(() => expect(dock()!.getAttribute('aria-label')).toBe('Step build-login'))
    // Closing the step goes back to the Overview.
    fireEvent.click(within(dock()!).getByRole('button', { name: 'Close the inspector' }))
    await waitFor(() => expect(dock()!.getAttribute('aria-label')).toBe('Workflow overview'))
  })

  it('puts the merge held by questions under the grid, and Answer opens the questions in the Needs-you row', async () => {
    await page()
    const strip = screen.getByRole('region', { name: 'Merge' })
    expect(strip.textContent).toContain('held')
    expect(strip.textContent).toContain('Held by 2 questions')
    fireEvent.click(within(strip).getByRole('button', { name: 'Answer 2 questions' }))
    // Only the asking step's chip stays bright.
    await waitFor(() => expect(document.querySelector('.sg.is-focused')).toBeTruthy())
    expect(chip('build-login')!.className).not.toContain('is-dimmed')
    expect(chip('build-search')!.className).toContain('is-dimmed')
  })

  it('carries a Needs-you row counting the failure', async () => {
    await page()
    const row = document.querySelector<HTMLElement>('.wi-main .ny, .wi-main [class*="needs"]')
    expect(row, 'no Needs-you row').toBeTruthy()
    expect(row!.textContent).toMatch(/Failed\s*1/)
  })

  it("opens a stage's tally as the Table on that stage in every state", async () => {
    const onView = vi.fn()
    render(<WorkflowsScreen view="wf=wf_build" onView={onView} />)
    await waitFor(() => expect(document.querySelector('.sg')).toBeTruthy())
    const tally = document.querySelector<HTMLButtonElement>('.sg button.sg-tally')!
    fireEvent.click(tally)
    expect(onView).toHaveBeenLastCalledWith(expect.stringContaining('tab=table'))
    expect(onView).toHaveBeenLastCalledWith(expect.stringContaining(`stepstate=${encodeURIComponent(STAGE_ANY_STATE).replace(/%20/g, '+')}`))
  })

  it('draws the Table in the left pane and a picked row opens in the inspector, never under the row', async () => {
    render(<WorkflowsScreen view="wf=wf_build&tab=table&stage=0&stepstate=any+state" />)
    await waitFor(() => expect(document.querySelector('.wi-main .wf-steps, .wi-main table')).toBeTruthy())
    expect(screen.getByText(/stage 1 · any state/)).toBeTruthy()
    // Both stage-one steps, whatever their state.
    expect(screen.getByText('2 of 5 steps')).toBeTruthy()
    const pick = document.querySelector<HTMLElement>('.wi-main button.wf-pick[data-step="build-search"]')!
    fireEvent.click(pick)
    await waitFor(() => expect(dock()!.getAttribute('aria-label')).toBe('Step build-search'))
    expect(document.querySelector('.wi-main .wf-inspect')).toBeNull()
  })

  it('starts folded at 1280 and docked at 1440', async () => {
    atWidth(1280)
    await page()
    expect(document.querySelector('.wi.is-folded')).toBeTruthy()
    expect(dock()).toBeNull()
  })

  it('opens the folded inspector at 1280 when a chip is picked, keeping the grid', async () => {
    atWidth(1280)
    const grid = await page()
    fireEvent.click(chip('build-login')!)
    await waitFor(() => expect(dock()?.getAttribute('aria-label')).toBe('Step build-login'))
    expect(document.querySelector('.wi.is-folded')).toBeNull()
    expect(document.querySelector('.wi-main .sg')).toBe(grid)
  })

  it('styles the page in the console stylesheet with no fixed width wider than a phone', () => {
    const rules = STYLES.slice(STYLES.indexOf('/* One workflow\'s page as two panes'))
    expect(rules).toContain('.wfp-merge-row')
    expect(rules).toContain('.wfp-live-row')
    for (const m of rules.slice(0, rules.indexOf('/* The Recent (5)')).matchAll(/(?:^|[^-])width:\s*(\d+)px/g)) {
      expect(Number(m[1])).toBeLessThanOrEqual(390)
    }
  })
})
