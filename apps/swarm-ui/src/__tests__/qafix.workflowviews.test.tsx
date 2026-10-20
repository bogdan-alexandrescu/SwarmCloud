// QAFIX-RUNS-TIMELINE, THE WORKFLOW PAGE'S STEP CARD (part of #1680, the
// first console visual QA run, wf_9e9bf74834634db7bcc3, 2026-10-20):
//
//   QA-121  the only 'why' a failed step's card gave was 'exit status 1:
//           pytest collected 2 failures', with no way to its logs: the head
//           linked 'Open agent →' and 'Changes ›' and nothing else.
//
// Every assertion names the mutation that turns it red.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { ownFromBoard } from './workflowsById'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  cancelWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen } = await import('../Workflows')

const T0 = Date.parse('2026-10-20T03:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[], task_id: string | null): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id }
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id, tenant_id: 'eng', state, runner_profile: 'claude-code', resource_class: 'standard', provider: 'anthropic',
    priority: 0, created_at: iso(-600), updated_at: iso(-60), started_at: iso(-500), completed_at: iso(-100),
    submitted_by: 'operator@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: 'wf_tests', step_id: null, depends_on: null, cancel_requested: false, repository_url: null, model: null,
    timeout_seconds: null, next_eligible_at: null, metadata: { unit: 'run-the-tests' }, repository_ref: null, input: null,
    last_error: null, result_summary: null, latest_checkpoint: null, current_generation: 1, current_lease_id: null,
    ...over,
  }
}

const WF: Workflow = {
  workflow_id: 'wf_tests', state: 'FAILED', tenant_id: 'eng', stored_state: 'FAILED', state_source: 'derived',
  rollup: { state: 'FAILED', complete: true, reason: 'test', counts: { SUCCEEDED: 1, FAILED: 1 }, unreadable_steps: [], unstarted_steps: [], steps_read: 2 },
  created_at: iso(-600), updated_at: iso(-60), submitted_by: 'operator@example.com', priority: 0,
  on_step_failure: 'fail_workflow', cancel_requested: false,
  steps: [step('build', [], 't_build'), step('test', ['build'], 't_test')],
}
const TASKS = new Map<string, Task>([
  ['t_build', task('t_build', 'SUCCEEDED', { step_id: 'build' })],
  ['t_test', task('t_test', 'FAILED', { step_id: 'test', last_error: 'exit status 1: pytest collected 2 failures' })],
])

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows: [WF], taskById: TASKS, statesDetail: null },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

function Routed({ initial }: { initial: string }) {
  const [view, setView] = useState(initial)
  return <WorkflowsScreen view={view} onView={setView} />
}

describe('QA-121: a step card links to the step agent\'s logs', () => {
  it('offers Logs beside Open agent on the failed step the page opens on, to that agent\'s Logs tab', async () => {
    render(<Routed initial="wf=wf_tests" />)
    // The page picks the first failure on arrival.
    const card = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('section.wf-inspect')
      expect(el?.getAttribute('aria-label')).toBe('Step test of wf_tests')
      return el!
    })
    const open = within(card).getByRole('link', { name: 'Open agent →' })
    // MUTATION: drop the Logs link from StepInspector's head and this is red.
    const logs = within(card).getByRole('link', { name: 'Logs ›' })
    // A FAILED task sits in Recent; its Logs tab is the agent's path plus `/logs`.
    // MUTATION: point it at the bare agent (no `/logs`), or at the Live
    // list a finished task is not in, and this is red.
    expect(open.getAttribute('href')).toBe('/agents/recent/t_test')
    expect(logs.getAttribute('href')).toBe('/agents/recent/t_test/logs')
  })
})
