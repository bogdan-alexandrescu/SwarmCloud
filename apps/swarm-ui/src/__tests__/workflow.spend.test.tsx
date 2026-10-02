/**
 * WF-5, ONE TOTAL PER WORKFLOW: the list row, the page head and the step
 * table's total read the same figures.
 *
 * The step table's total (`.wf-table-total`) sums each step's attempt
 * telemetry where the attempt read carried a cost, and the step's result only
 * where it did not (`stepCostOf`). The page head and the list row summed the
 * results alone (`workflowSpend(..., null)`), so a workflow whose attempts
 * cost more than its finishing results said -- every retry is an attempt, and
 * a result describes only the attempt that finished -- showed two totals on
 * one page. The fixture below makes the two sources disagree on purpose:
 * results say $1.00 + $1.00, attempts say $3.00 + $4.00.
 *
 * MUTATION: hand the head or the list row `null` telemetry again and it reads
 * $2.00 beside the table's $7.00.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { StepUsage, WorkflowBoard, WorkflowUsage } from '../api'
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

const { WorkflowsScreen } = await import('../Workflows')

const T0 = Date.parse('2026-10-01T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[], task_id: string): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id }
}

function task(id: string, state: TaskState, resultUsd: number): Task {
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
    started_at: iso(-500),
    completed_at: iso(-100),
    submitted_by: 'priya@example.com',
    attempt_count: 2,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_spend',
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
    result_summary: { runner: { usage: { total_cost_usd: resultUsd } } } as unknown as Task['result_summary'],
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
  }
}

function workflow(): Workflow {
  return {
    workflow_id: 'wf_spend',
    state: 'SUCCEEDED',
    tenant_id: 'eng',
    stored_state: 'SUCCEEDED',
    state_source: 'derived',
    rollup: { state: 'SUCCEEDED', complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: 2 },
    created_at: iso(-600),
    updated_at: iso(-60),
    submitted_by: 'priya',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps: [step('plan', [], 't_plan'), step('build', ['plan'], 't_build')],
  }
}

function usage(costUsd: number): StepUsage {
  return {
    attempts: 2,
    attemptsWithCost: 2,
    attemptsWithTokens: 0,
    checkpoints: 0,
    costUsd,
    inputTokens: null,
    outputTokens: null,
    cacheReadTokens: null,
    cacheCreationTokens: null,
  }
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  const board: WorkflowBoard = {
    workflows: [workflow()],
    taskById: new Map([
      ['t_plan', task('t_plan', 'SUCCEEDED', 1)],
      ['t_build', task('t_build', 'SUCCEEDED', 1)],
    ]),
    statesDetail: null,
  }
  api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: board, fetchedAt: T0 } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({
    status: 'ok',
    fetchedAt: T0,
    data: {
      byTaskId: new Map([
        ['t_plan', usage(3)],
        ['t_build', usage(4)],
      ]),
      notSampled: new Set(),
      failed: new Map(),
      sampleLimit: 12,
      tasksRequested: 2,
    },
  } satisfies Result<WorkflowUsage>)
  api.loadWorkflow.mockResolvedValue({
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no such workflow' },
  })
})

describe('one workflow, one total (WF-5)', () => {
  it('draws the attempt-read total in the list row, the page head and the step table alike', async () => {
    const list = render(<WorkflowsScreen view="" />)
    const rowTotal = await waitFor(() => {
      const el = document.querySelector('tr[data-workflow="wf_spend"] .wf-spend')
      expect(el?.textContent ?? '').toContain('$7.00')
      return el!.textContent ?? ''
    })
    list.unmount()

    render(<WorkflowsScreen view="wf=wf_spend&tab=table" />)
    const tableTotal = await waitFor(() => {
      const el = document.querySelector('.wf-table-total .wf-spend')
      expect(el?.textContent ?? '').toContain('$7.00')
      return el!.textContent ?? ''
    })
    const head = await waitFor(() => {
      const el = document.querySelector('.wfp-facts')
      expect(el?.textContent ?? '').toContain('$7.00')
      return el!.textContent ?? ''
    })

    // The control: the results alone say $2.00, and no surface may.
    expect(head).not.toContain('$2.00')
    expect(rowTotal).toBe(tableTotal)
  })
})
