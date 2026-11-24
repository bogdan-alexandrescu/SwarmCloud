/**
 * PAUSE AND RESUME BESIDE STOP WORKFLOW (docs/design/workflow-pause.md §3, §9):
 * which button shows in each state, each one's call, a viewer's disabled
 * button, a refusal in the API's own words, and the one-word paused mark in
 * the page head with who and when behind its tooltip.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  cancelWorkflow: vi.fn(),
  pauseWorkflow: vi.fn(),
  resumeWorkflow: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})
// The role read is the gate's (roleGate.ts); here a test sets what it says.
const gate = vi.hoisted(() => ({ sentence: null as string | null }))
vi.mock('../roleGate', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../roleGate')>()),
  useRoleGate: () => gate.sentence,
}))

const { PauseWorkflow, WorkflowsScreen } = await import('../Workflows')

const T0 = Date.parse('2026-11-24T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()
const VIEWER = 'Your role in eng is viewer; members pause here.'

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
    submitted_by: 'dev@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
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

function workflow(state: TaskState, over: Partial<Workflow> = {}): Workflow {
  return {
    workflow_id: 'wf_broker',
    state,
    tenant_id: 'eng',
    stored_state: state,
    state_source: 'derived',
    rollup: { state, complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: 2 },
    created_at: iso(-600),
    updated_at: iso(-60),
    submitted_by: 'dev@example.com',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps: [step('plan', [], { task_id: 't_plan' }), step('core', ['plan'], { task_id: 't_core' })],
    ...over,
  }
}

const PAUSED: Partial<Workflow> = { paused: true, paused_at: iso(-300), paused_by: 'dev@example.com' }
const TASKS = [task('t_plan', 'SUCCEEDED'), task('t_core', 'RUNNING', { started_at: iso(-300) })]

const refusal = (httpStatus: number, code: string, message: string) => ({
  status: 'error' as const,
  error: { kind: httpStatus === 503 ? ('rate_limited' as const) : ('conflict' as const), httpStatus, code, message },
})

beforeEach(() => {
  gate.sentence = null
  for (const f of Object.values(api)) f.mockReset()
  api.pauseWorkflow.mockResolvedValue({
    status: 'ok',
    fetchedAt: T0,
    data: { workflow_id: 'wf_broker', paused: true, paused_at: iso(0), steps_signalled: ['t_core'], steps_already_terminal: ['t_plan'] },
  })
  api.resumeWorkflow.mockResolvedValue({ status: 'ok', fetchedAt: T0, data: { workflow_id: 'wf_broker', paused: false } })
})

afterEach(() => cleanup())

describe('which button shows', () => {
  it('offers Pause, and not Resume, while the workflow runs unpaused', () => {
    render(<PauseWorkflow workflow={workflow('RUNNING')} onDone={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Pause workflow' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Resume workflow' })).toBeNull()
  })

  it('offers Resume, and not Pause, while the workflow is paused', () => {
    render(<PauseWorkflow workflow={workflow('RUNNING', PAUSED)} onDone={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Resume workflow' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Pause workflow' })).toBeNull()
  })

  for (const state of ['SUCCEEDED', 'FAILED', 'CANCELLED'] as const) {
    it(`offers neither once the workflow is ${state}`, () => {
      const { container } = render(<PauseWorkflow workflow={workflow(state)} onDone={vi.fn()} />)
      expect(container.textContent).toBe('')
      cleanup()
      const paused = render(<PauseWorkflow workflow={workflow(state, PAUSED)} onDone={vi.fn()} />)
      expect(paused.container.textContent).toBe('')
    })
  }

  it('offers neither once a cancel was requested, paused or not', () => {
    const { container } = render(<PauseWorkflow workflow={workflow('RUNNING', { cancel_requested: true })} onDone={vi.fn()} />)
    expect(container.textContent).toBe('')
    cleanup()
    const paused = render(<PauseWorkflow workflow={workflow('RUNNING', { ...PAUSED, cancel_requested: true })} onDone={vi.fn()} />)
    expect(paused.container.textContent).toBe('')
  })

  it('offers neither once this page recorded a stop the read has not caught up with', () => {
    const { container } = render(<PauseWorkflow workflow={workflow('RUNNING')} onDone={vi.fn()} stopSent />)
    expect(container.textContent).toBe('')
  })
})

describe('each button’s call', () => {
  it('Pause sends the pause for this workflow at once, then re-reads', async () => {
    const onDone = vi.fn()
    render(<PauseWorkflow workflow={workflow('RUNNING')} onDone={onDone} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Pause workflow' }))
    })
    expect(api.pauseWorkflow).toHaveBeenCalledTimes(1)
    expect(api.pauseWorkflow).toHaveBeenCalledWith('wf_broker')
    expect(api.resumeWorkflow).not.toHaveBeenCalled()
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    // The answer holds until the read carries `paused`: Resume, not a second Pause.
    expect(screen.getByRole('button', { name: 'Resume workflow' })).toBeTruthy()
  })

  it('Resume sends the resume for this workflow at once, then re-reads', async () => {
    const onDone = vi.fn()
    render(<PauseWorkflow workflow={workflow('RUNNING', PAUSED)} onDone={onDone} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Resume workflow' }))
    })
    expect(api.resumeWorkflow).toHaveBeenCalledTimes(1)
    expect(api.resumeWorkflow).toHaveBeenCalledWith('wf_broker')
    expect(api.pauseWorkflow).not.toHaveBeenCalled()
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: 'Pause workflow' })).toBeTruthy()
  })

  it('is a button, never a link', () => {
    render(<PauseWorkflow workflow={workflow('RUNNING')} onDone={vi.fn()} />)
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByRole('button', { name: 'Pause workflow' }).tagName).toBe('BUTTON')
  })
})

describe('a viewer', () => {
  for (const [what, over, name] of [
    ['Pause', {}, 'Pause workflow'],
    ['Resume', PAUSED, 'Resume workflow'],
  ] as const) {
    it(`sees ${what} disabled with the role sentence beside it, and sends nothing`, () => {
      gate.sentence = VIEWER
      render(<PauseWorkflow workflow={workflow('RUNNING', over)} onDone={vi.fn()} />)
      const button = screen.getByRole('button', { name }) as HTMLButtonElement
      expect(button.disabled).toBe(true)
      const note = document.querySelector<HTMLElement>('[data-role-gate="member"]')
      expect(note?.textContent).toBe(VIEWER)
      expect(button.getAttribute('aria-describedby')).toBe(note?.id)
      fireEvent.click(button)
      expect(api.pauseWorkflow).not.toHaveBeenCalled()
      expect(api.resumeWorkflow).not.toHaveBeenCalled()
    })
  }
})

describe('a refusal, in the API’s own words', () => {
  it('shows a 409 for a finished workflow and does not re-read', async () => {
    api.pauseWorkflow.mockResolvedValue(refusal(409, 'workflow_terminal', 'workflow wf_broker has already finished'))
    const onDone = vi.fn()
    render(<PauseWorkflow workflow={workflow('RUNNING')} onDone={onDone} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Pause workflow' }))
    })
    expect((await screen.findByRole('alert')).textContent).toContain('workflow wf_broker has already finished')
    expect(onDone).not.toHaveBeenCalled()
    // Nothing changed: the same button is still offered.
    expect(screen.getByRole('button', { name: 'Pause workflow' })).toBeTruthy()
  })

  it('shows a 409 for a workflow being cancelled, on Resume', async () => {
    api.resumeWorkflow.mockResolvedValue(refusal(409, 'workflow_cancel_requested', 'a cancel was requested; nothing is left to resume'))
    render(<PauseWorkflow workflow={workflow('RUNNING', PAUSED)} onDone={vi.fn()} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Resume workflow' }))
    })
    expect((await screen.findByRole('alert')).textContent).toContain('a cancel was requested; nothing is left to resume')
  })

  it('shows a 503 for contended steps', async () => {
    api.pauseWorkflow.mockResolvedValue(refusal(503, 'pause_contended', '1 step stayed contended; retry to finish the pause'))
    const onDone = vi.fn()
    render(<PauseWorkflow workflow={workflow('RUNNING')} onDone={onDone} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Pause workflow' }))
    })
    expect((await screen.findByRole('alert')).textContent).toContain('1 step stayed contended; retry to finish the pause')
    expect(onDone).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// On the page
// ---------------------------------------------------------------------------

function serve(w: Workflow): void {
  const board: WorkflowBoard = { workflows: [w], taskById: new Map(TASKS.map((t) => [t.id, t])), statesDetail: null }
  api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: board, fetchedAt: T0 } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadWorkflow.mockResolvedValue({ status: 'ok', data: { workflow: w, tasks: TASKS }, fetchedAt: T0 })
}

async function pageHead(): Promise<HTMLElement> {
  render(<WorkflowsScreen view="wf=wf_broker" onView={() => {}} />)
  return waitFor(
    () => {
      const head = document.querySelector<HTMLElement>('.wfp:not(.is-loading) > .wfp-head')
      expect(head, 'no page head').toBeTruthy()
      return head!
    },
    { timeout: 4000 },
  )
}

describe('the page', () => {
  const realWidth = window.innerWidth
  afterEach(() => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: realWidth })
  })

  it('marks a paused workflow in its head with one word, who and when behind the tooltip', async () => {
    serve(workflow('RUNNING', PAUSED))
    const head = await pageHead()
    const mark = head.querySelector<HTMLElement>('.tag.paused')
    expect(mark?.textContent).toBe('paused')
    const tip = mark?.getAttribute('title') ?? ''
    expect(tip).toContain('dev@example.com')
    expect(tip).toContain(PAUSED.paused_at!.slice(0, 10))
    // Never a sentence on the page: who and when are not drawn as text.
    expect(head.textContent).not.toContain('dev@example.com')
  })

  it('draws no paused mark on a workflow that is not paused', async () => {
    serve(workflow('RUNNING'))
    const head = await pageHead()
    expect(head.querySelector('.tag.paused')).toBeNull()
  })

  it('draws Pause beside Stop workflow in the Overview’s actions', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
    serve(workflow('RUNNING'))
    await pageHead()
    const actions = await waitFor(() => {
      const a = document.querySelector<HTMLElement>('aside.wi-dock[aria-label="Workflow overview"] .wfp-actions')
      expect(a, 'the Overview did not dock').toBeTruthy()
      return a!
    })
    expect(within(actions).getByRole('button', { name: 'Stop workflow' })).toBeTruthy()
    expect(within(actions).getByRole('button', { name: 'Pause workflow' })).toBeTruthy()
  })
})
