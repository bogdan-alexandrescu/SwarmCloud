/**
 * THE WORKFLOW PAGE'S CENSUS NAMES WHAT ITS PARKED STEPS WAIT ON (the owner's
 * answer, 11 November 2026: "Name the reasons in the census"). The head's
 * `N parked` stays the only visible words; its tooltip, on hover and on
 * keyboard focus, says the reasons by count, from the stage grid's own
 * `parkReasonsOf`. A census with no parked step has no tooltip at all.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen } = await import('../Workflows')

const T0 = Date.parse('2026-11-11T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[], task_id: string): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id }
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
    submitted_by: 'someone@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_census',
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

function board(tasks: Task[]): WorkflowBoard {
  const steps = [step('build', [], 't_b'), step('lint', [], 't_l'), step('review', ['build'], 't_r')]
  const w: Workflow = {
    workflow_id: 'wf_census',
    state: 'RUNNING',
    tenant_id: 'eng',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: { state: 'RUNNING', complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: steps.length },
    created_at: iso(-30 * 60),
    updated_at: iso(-60),
    submitted_by: 'someone@example.com',
    priority: 0,
    on_step_failure: 'continue',
    cancel_requested: false,
    steps,
  }
  return { workflows: [w], taskById: new Map(tasks.map((t) => [t.id, t])), statesDetail: null }
}

function serve(b: WorkflowBoard): void {
  api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: b, fetchedAt: T0 } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadWorkflow.mockResolvedValue({ status: 'ok', data: { workflow: b.workflows[0]!, tasks: [...b.taskById!.values()] }, fetchedAt: T0 })
}

async function census(): Promise<HTMLElement> {
  render(<WorkflowsScreen view="wf=wf_census" />)
  return waitFor(() => {
    const head = document.querySelector<HTMLElement>('.wfp-top')
    expect(head, 'no workflow head').toBeTruthy()
    return within(head!).getByLabelText('Steps by state')
  })
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

describe('the workflow census names what its parked steps wait on', () => {
  it('gives the parked count a tooltip listing each reason with its count, shown on keyboard focus', async () => {
    serve(
      board([
        task('t_b', 'PARKED', { park_reason: 'PROVIDER_QUOTA_EXHAUSTED', step_id: 'build' }),
        task('t_l', 'RUNNING', { started_at: iso(-120), step_id: 'lint' }),
        task('t_r', 'PARKED', { park_reason: 'DEPENDENCY_INCOMPLETE', attempt_count: 0, step_id: 'review' }),
      ]),
    )
    const tally = await census()
    const parked = tally.querySelector<HTMLElement>('.wfp-t.is-parked')!
    // No visible prose is added: the count still reads `N parked`.
    expect(parked.textContent).toBe('2 parked')
    const id = parked.getAttribute('aria-describedby')
    expect(id, 'the parked count names no tooltip').toBeTruthy()
    const tip = document.getElementById(id!)!
    expect(tip.getAttribute('role')).toBe('tooltip')
    expect(tip.textContent).toBe('2 parked: 1 provider quota, 1 waiting on a step')
    // Help, not surface prose: the prose ratchet does not count it.
    expect(tip.hasAttribute('data-help-description')).toBe(true)
    expect(tip.hidden).toBe(true)
    // A keyboard reaches it, and focus shows the same tooltip; Escape and blur hide it.
    expect(parked.tabIndex).toBe(0)
    fireEvent.focus(parked)
    expect(tip.hidden).toBe(false)
    fireEvent.keyDown(parked, { key: 'Escape' })
    expect(tip.hidden).toBe(true)
    fireEvent.focus(parked)
    fireEvent.blur(parked)
    expect(tip.hidden).toBe(true)
    fireEvent.mouseEnter(parked)
    await waitFor(() => expect(tip.hidden).toBe(false))
    fireEvent.mouseLeave(parked)
    expect(tip.hidden).toBe(true)
  })

  it('shows no tooltip when no step is parked', async () => {
    serve(
      board([
        task('t_b', 'SUCCEEDED', { started_at: iso(-500), completed_at: iso(-300), step_id: 'build' }),
        task('t_l', 'RUNNING', { started_at: iso(-120), step_id: 'lint' }),
        task('t_r', 'QUEUED', { step_id: 'review' }),
      ]),
    )
    const tally = await census()
    expect(tally.querySelector('.wfp-t.is-parked')).toBeNull()
    expect(tally.textContent).not.toContain('parked')
    expect(tally.querySelector('[role="tooltip"]')).toBeNull()
    expect(tally.querySelector('[aria-describedby]')).toBeNull()
  })
})
