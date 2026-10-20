/**
 * LANE HW-UI (docs/design/human-steps.md §4.5; part of #1436): an
 * `await-input` step on the workflow page and on the run's Steps card.
 *
 *   * the step's pill reads "waiting for a member" -- never a bare `parked`,
 *     never "waiting for you", since any member of the tenant may answer
 *     (Q2) -- and its row shows the deadline, "expires in 3 h 20 m";
 *   * opening the workflow picks the waiting step, so its card (the place an
 *     answer is given, and where the inbox's Answer lands) is open on arrival;
 *   * on the run's Steps card the step reads what it waits for and leads to
 *     its card, never "Open agent": it has no agent.
 *
 * Each case fails without the change. Placeholders only.
 */
import { render, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowRead, WorkflowUsage } from '../api'
import type { IssueRun, Task, TaskState, Workflow, WorkflowStep } from '../types'
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
const { StepsCard } = await import('../RunSteps')

const T0 = Date.parse('2026-10-18T10:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()
const EXPIRES = iso(3 * 3600 + 20 * 60)

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-7200),
    updated_at: iso(-60),
    started_at: null,
    completed_at: null,
    submitted_by: 'alex@example.com',
    attempt_count: 0,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_rv',
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
    current_generation: 0,
    current_lease_id: null,
    ...over,
  } as Task
}

const draft = () => task('t_draft', 'SUCCEEDED', { step_id: 'draft', attempt_count: 1, started_at: iso(-3700), completed_at: iso(-3600) })
const review = () =>
  task('t_review', 'PARKED', {
    step_id: 'review',
    runner_profile: 'await-input',
    park_reason: 'AWAITING_INPUT',
    next_eligible_at: iso(-3600),
    metadata: { await_input: { opened_at: iso(-3600), expires_at: EXPIRES } },
  })

function wfStep(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
}

const STEPS = [
  wfStep('draft', [], { task_id: 't_draft' }),
  wfStep('review', ['draft'], { task_id: 't_review', runner_profile: 'await-input', input_from: { draft: 'draft.md' } }),
  wfStep('revise', ['review'], { input_from: { review: 'review.json' } }),
]

function workflow(): Workflow {
  return {
    workflow_id: 'wf_rv',
    state: 'RUNNING',
    tenant_id: 'eng',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: { state: 'PARKED', complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: 3 },
    created_at: iso(-7200),
    updated_at: iso(-60),
    submitted_by: 'alex@example.com',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps: STEPS,
  } as Workflow
}

/** The card's own read, `GET .../steps/review/input`, through the real fetch path. */
function serveView(): void {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input).split('?')[0]
    const body =
      url === '/v1/workflows/wf_rv/steps/review/input'
        ? {
            workflow_id: 'wf_rv', step_id: 'review', task_id: 't_review', status: 'waiting', spec_verified: true,
            prompt: 'Review the draft.', form: 'verdict', artifact: 'review.json', max_files: 0, max_answer_bytes: 16 * 1024 * 1024,
            tenant: 'eng', may_answer: true, remind: [], opened_at: iso(-3600), expires_at: EXPIRES, on_expire: 'fail',
            shows: [{ step_id: 'draft', task_id: 't_draft', name: 'draft.md' }], questions: null, answer: null,
          }
        : null
    return new Response(JSON.stringify(body ?? { detail: 'Not Found' }), { status: body === null ? 404 : 200, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
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

async function page() {
  serveView()
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows: [workflow()], taskById: new Map([['t_draft', draft()], ['t_review', review()]]), statesDetail: null },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
  api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
  render(<Routed initial="wf=wf_rv" />)
  return waitFor(() => {
    const n = document.querySelector<HTMLElement>('button.node[data-step="review"]')
    expect(n).toBeTruthy()
    return n!
  })
}

describe('the workflow page', () => {
  it('the waiting step’s pill reads "waiting for a member" and its row shows the deadline', async () => {
    const node = await page()
    expect(node.querySelector('.node-state')!.textContent).toBe('waiting for a member')
    expect(node.textContent).not.toContain('waiting for you')
    expect(node.querySelector('.node-until')!.textContent).toBe('expires in 3 h 20 m')
  })

  it('opens the waiting step’s card on arrival, which reads the step and offers its form', async () => {
    await page()
    const card = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.wf-inspect [data-testid="ai-card"]')
      expect(el).toBeTruthy()
      return el!
    })
    await waitFor(() => expect(within(card).getByRole('button', { name: 'Approve' })).toBeTruthy())
    expect(card.textContent).toContain('Review the draft.')
  })
})

describe('the run’s Steps card', () => {
  it('reads what the step waits for and leads to its card, never to an agent', () => {
    const read = {
      ids: ['wf_rv'],
      loads: [{ wf: 'wf_rv', round: 0, data: { workflow: workflow(), tasks: [draft(), review()] } as unknown as WorkflowRead, error: null }],
      usage: null,
    }
    const run = { id: 'run_1', workflow_id: 'wf_rv', ci_fix_workflows: [], plan: null, history: [] } as unknown as IssueRun
    const go = vi.fn()
    const { container } = render(<StepsCard run={run} read={read} go={go} now={T0} />)
    const row = container.querySelector<HTMLElement>('li[data-step="review"]')!
    expect(row.textContent).toContain('waiting for a member')
    expect(row.textContent).toContain('expires in 3 h 20 m')
    expect(row.textContent).not.toMatch(/attempt|elapsed|Open agent/)
    const answer = within(row).getByRole('link', { name: 'Answer ›' })
    expect(answer.getAttribute('href')).toBe('/workflows/wf_rv')
    answer.click()
    expect(go).toHaveBeenCalledWith('work/workflows?wf=wf_rv')
  })
})
