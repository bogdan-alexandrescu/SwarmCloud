/**
 * A WORKFLOW'S CI-FIX ROUND IS DRAWN AS A STEP (STATUS-TRUE, part of #1914).
 *
 * BGD, 2026-10-22, the console page /workflows/wf_15160fca1d07438ab594:
 * CI on PR 1905 had failed and the merge step's round `ci-fix:1` was RUNNING,
 * and nothing on the page said so. The round is appended to the lane's
 * workflow after submission, and its task belongs to the round's OWN
 * workflow. The page draws its steps from its own read,
 * `GET /v1/workflows/{id}` (`loadWorkflowPage`), joining each step to the
 * task that read carries -- the same read whose step-task query left the
 * round out (swarm_api.routes.workflows.get_workflow, fixed beside this).
 * Served the round's task, the page draws the round as it draws any step:
 * its state, and the step's stop control, which is drawn only for a joined
 * task.
 *
 * MUTATIONS: join `taskById` only from tasks whose `workflow_id` is the
 * page's, or draw only the submitted steps -- each turns a case red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflow: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { loadWorkflowPage, WorkflowsScreen } = await import('../Workflows')

const WF = 'wf_15160fca1d07438ab594'
const ROUND_WF = 'wf_round00000000000000'
const T0 = Date.parse('2026-10-22T01:27:00.000Z')
const iso = (ms: number) => new Date(T0 + ms).toISOString()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function step(step_id: string, depends_on: string[], task_id: string, runner_profile = 'claude-code'): WorkflowStep {
  return { step_id, runner_profile, resource_class: 'standard', depends_on, input_from: {}, task_id }
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id, tenant_id: 'eng', state, runner_profile: 'claude-code', resource_class: 'standard', provider: 'anthropic',
    priority: 0, created_at: iso(-600_000), updated_at: iso(-60_000), started_at: iso(-500_000), completed_at: null,
    submitted_by: 'alice@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: WF, step_id: null, depends_on: null, cancel_requested: false, repository_url: null, model: null,
    timeout_seconds: null, next_eligible_at: null, metadata: null, repository_ref: null, input: null, last_error: null,
    result_summary: null, latest_checkpoint: null, current_generation: 1, current_lease_id: null,
    ...over,
  }
}

const STEPS = [
  step('implement', [], 'task_impl'),
  step('merge', ['implement'], 'task_0b5b07c139a54ee78a39', 'merge'),
  // Appended by the merge step's wake tick after submission (SPEED-D2).
  step('ci-fix:1', [], 'task_d38fb3ecddb6463288b5'),
]

const TASKS = [
  task('task_impl', 'SUCCEEDED', { step_id: 'implement', completed_at: iso(-300_000) }),
  task('task_0b5b07c139a54ee78a39', 'PARKED', { step_id: 'merge', runner_profile: 'merge', park_reason: 'CI_PENDING' }),
  // The round's task is its own workflow's step, not this one's.
  task('task_d38fb3ecddb6463288b5', 'RUNNING', { step_id: 'ci-fix', workflow_id: ROUND_WF }),
]

function workflow(): Workflow {
  return {
    workflow_id: WF, state: 'RUNNING', tenant_id: 'eng', stored_state: 'RUNNING', state_source: 'derived',
    rollup: {
      state: 'RUNNING', complete: true, reason: 'steps_hold_capacity',
      counts: { SUCCEEDED: 1, PARKED: 1, RUNNING: 1 }, unreadable_steps: [], unstarted_steps: [], steps_read: 3,
      fix_round_steps: ['ci-fix:1'],
    },
    created_at: iso(-900_000), updated_at: iso(-60_000), submitted_by: 'alice@example.com', priority: 0,
    on_step_failure: 'FAIL_WORKFLOW', cancel_requested: false, steps: STEPS,
  } as unknown as Workflow
}

beforeEach(() => {
  api.loadWorkflowBoard.mockReturnValue(new Promise(() => {}))
  api.loadWorkflow.mockResolvedValue(ok({ workflow: workflow(), tasks: TASKS }))
})

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

describe('STATUS-TRUE: a running ci-fix round is a step of the page', () => {
  it('joins the round\'s task, which belongs to the round\'s own workflow', async () => {
    const r = await loadWorkflowPage(WF)
    const data = (r as { data: { taskById: Map<string, Task> } }).data
    // MUTATION: joining only tasks whose workflow_id is the page's turns this red.
    expect(data.taskById.get('task_d38fb3ecddb6463288b5')?.state).toBe('RUNNING')
  })

  it('draws the round as a step, with its state and its stop control', async () => {
    const { container } = render(<WorkflowsScreen view={`wf=${WF}`} />)
    await waitFor(() => expect(container.querySelector('.node[data-step="ci-fix:1"]')).not.toBeNull())
    const node = container.querySelector('.node[data-step="ci-fix:1"]') as HTMLElement
    // MUTATION: drawing only the submitted steps, or the round unjoined, turns these red.
    expect(node.textContent).toMatch(/running/i)
    expect(node.textContent).not.toMatch(/unread/i)
    // The stop control is drawn only beside a step whose task was joined.
    expect(node.parentElement?.querySelector('.node-stop')).not.toBeNull()
  })
})
