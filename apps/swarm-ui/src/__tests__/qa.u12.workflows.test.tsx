/**
 * QA ROUND 3, LANE U12 (owner, 2026-10-04, live console at main 8455be37):
 * one workflow's page and the Workflows list.
 *
 *   R8   /workflows/wf_8fa28bbc… (succeeded, 10/10): Graph and Table drew every
 *        step `state unread` and every cell `task unread` (12 of 267 joined)
 *        while the step card read exit 0, 2m 56s and $0.89 for the same step.
 *        The page joined states only through the board's 200-task window; it
 *        now reads its own steps' tasks by id. The Attempts cell clipped
 *        'task unread' (79px in 70px).
 *   R10  Owner showed 'swarm-…' in 74px; Cost printed '$0.4950', '$7.6778' and
 *        '$12.92' side by side, and '$0.6024 1/3' was clipped by 3px.
 *   R16  The head said "started start not read".
 *
 * MUTATIONS: return the board without the workflow's own read; put Attempts
 * back to 70px; put `money` back to four decimals under $10; put Cost or
 * Owner back to their shares; print `started ${text}` -- each turns a case red.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { WorkflowBoard } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { cellStyle, fixedColumns, textPx } from './tablefit'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflow: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { loadWorkflowPage, startedPhrase, WorkflowsScreen } = await import('../Workflows')
const { usd } = await import('../measure')
const { workflowStartText } = await import('../types')

const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 8000 }
const WF = 'wf_8fa28bbc798e4b8a8e7e'
const T0 = Date.parse('2026-10-04T09:00:00.000Z')
const iso = (ms: number) => new Date(T0 + ms).toISOString()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function step(step_id: string, depends_on: string[], task_id: string | null): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id }
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id, tenant_id: 'eng', state, runner_profile: 'claude-code', resource_class: 'standard', provider: 'anthropic',
    priority: 0, created_at: iso(-600_000), updated_at: iso(-60_000), started_at: iso(-500_000), completed_at: iso(-300_000),
    submitted_by: 'bogdan@saga.xyz', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: WF, step_id: null, depends_on: null, cancel_requested: false, repository_url: null, model: null,
    timeout_seconds: null, next_eligible_at: null, metadata: null, repository_ref: null, input: null, last_error: null,
    result_summary: null, latest_checkpoint: null, current_generation: 1, current_lease_id: null,
    ...over,
  }
}

function workflow(steps: WorkflowStep[]): Workflow {
  return {
    workflow_id: WF, state: 'SUCCEEDED', tenant_id: 'eng', stored_state: 'SUCCEEDED', state_source: 'derived',
    rollup: { state: 'SUCCEEDED', complete: true, reason: null, counts: { SUCCEEDED: steps.length }, unreadable_steps: [], unstarted_steps: [], steps_read: steps.length },
    created_at: iso(-900_000), updated_at: iso(-60_000), submitted_by: 'bogdan@saga.xyz', priority: 0,
    on_step_failure: 'FAIL_WORKFLOW', cancel_requested: false, steps,
  } as unknown as Workflow
}

const STEPS = [step('plan', [], 'task_plan'), step('build', ['plan'], 'task_build'), step('ship', ['build'], 'task_ship')]
const OWN = STEPS.map((s) => task(s.task_id!, 'SUCCEEDED', { step_id: s.step_id }))
/** The board's window: twelve other tasks, none of this workflow's. */
const WINDOW = Array.from({ length: 12 }, (_, i) => task(`task_other_${i}`, 'SUCCEEDED', { workflow_id: 'wf_other' }))

function board(taskById: Map<string, Task> | null): WorkflowBoard {
  return { workflows: [workflow(STEPS)], taskById, statesDetail: taskById === null ? 'The task read failed.' : null } as WorkflowBoard
}

beforeEach(() => {
  api.loadWorkflowBoard.mockResolvedValue(ok(board(new Map(WINDOW.map((t) => [t.id, t])))))
  api.loadWorkflow.mockResolvedValue(ok({ workflow: workflow(STEPS), tasks: OWN }))
})

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

describe('R8: the page joins its own steps\' tasks', () => {
  it('reads the workflow by id and joins its tasks, whether or not the board\'s window reached them', async () => {
    const r = await loadWorkflowPage(WF)
    expect(r.status).toBe('ok')
    const data = (r as { data: WorkflowBoard }).data
    expect(api.loadWorkflow).toHaveBeenCalledWith(WF)
    for (const t of OWN) expect(data.taskById?.get(t.id)?.state, t.id).toBe('SUCCEEDED')
    // The window is still joined for the other workflows the page scrubs.
    expect(data.taskById?.get('task_other_0')).toBeDefined()
    expect(data.workflows.filter((w) => w.workflow_id === WF)).toHaveLength(1)
  })

  it('joins them even when the board\'s task read failed', async () => {
    api.loadWorkflowBoard.mockResolvedValue(ok(board(null)))
    const r = await loadWorkflowPage(WF)
    const data = (r as { data: WorkflowBoard }).data
    for (const t of OWN) expect(data.taskById?.get(t.id)?.state).toBe('SUCCEEDED')
  })

  it('draws no step of a finished workflow as unread', async () => {
    const { container } = render(<WorkflowsScreen view={`wf=${WF}`} />)
    await waitFor(() => expect(container.querySelector('[data-step="ship"], .wf-node')).not.toBeNull(), WAIT)
    await waitFor(() => expect(container.textContent).toMatch(/succeeded/i), WAIT)
    expect(container.textContent).not.toMatch(/state unread/)
    expect(container.textContent).not.toMatch(/no task joined/)
  })

  it('gives the Attempts column room for "task unread"', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="wf-card"><div class="wf-table-box"><div class="ctl-table wf-table"><table><thead><tr>' +
      '<th data-col="attempts">Attempts</th></tr></thead><tbody><tr data-step="a"><td data-col="attempts" class="is-num">task unread</td></tr></tbody></table></div></div></div>'
    document.body.appendChild(host)
    const th = host.querySelector('th')!
    const td = host.querySelector('td')!
    const env: CascadeEnv = { width: 1440, container: 996 }
    const width = Number.parseFloat(painted(th, 'width', env) ?? '0')
    const st = cellStyle(td, env)
    expect(width - st.pl - st.pr).toBeGreaterThanOrEqual(textPx('task unread', td, env))
    host.remove()
  })
})

describe('R10: one money format, and Cost and Owner hold what they show', () => {
  it('prints two decimals from a dollar up and three significant figures below', () => {
    expect(usd(0.495)).toBe('$0.495')
    expect(usd(0.6024)).toBe('$0.602')
    expect(usd(7.6778)).toBe('$7.68')
    expect(usd(12.92)).toBe('$12.92')
    expect(usd(0.5)).toBe('$0.50')
    expect(usd(0.0312)).toBe('$0.0312')
    expect(usd(0.004)).toBe('$0.004')
    expect(usd(0)).toBe('$0.00')
    // A real cost never prints as free, however small; only an exact zero does.
    expect(usd(0.0000004)).toBe('<$0.000001')
    expect(usd(1e-25)).toBe('<$0.000001')
    expect(usd(-1e-25)).toBe('-<$0.000001')
    expect(usd(0.000001)).toBe('$0.000001')
    expect(usd(0.009999)).toBe('$0.01')
  })

  it('fits "$12.92 10/12" in Cost and a 14-character owner in Owner at 1440, the name keeping room', async () => {
    vi.resetModules()
    const { App } = await import('../App')
    window.history.replaceState(null, '', '/workflows')
    render(<App />)
    const t = await waitFor(() => {
      const el = document.querySelector<HTMLTableElement>('.wfl .wfl-table')
      expect(el!.querySelectorAll('tbody tr.wfl-row:not(.is-skel)').length).toBeGreaterThan(0)
      return el!
    }, WAIT)
    const { cols } = fixedColumns(t, 1440 - 84 - 236 - 64, WIDE)
    const row = t.querySelector('tbody tr.wfl-row')!
    const room = (col: string) => {
      const box = cols.find((c) => c.col === col)!
      const st = cellStyle(row.querySelector(`td[data-col="${col}"]`)!, WIDE)
      return box.end - box.start - st.pl - st.pr
    }
    const cost = row.querySelector('td[data-col="cost"]')!
    // The figure, its coverage and the 5px between them.
    expect(room('cost')).toBeGreaterThanOrEqual(textPx('$12.92', cost, WIDE) + 5 + textPx('10/12', cost, WIDE))
    expect(room('owner')).toBeGreaterThanOrEqual(textPx('swarm-pipeline', row.querySelector('td[data-col="owner"]')!, WIDE))
    expect(room('workflow'), 'the name').toBeGreaterThanOrEqual(128)
  })
})

describe('R16: the head\'s start reads as words', () => {
  it('says "started — not read", never "started start not read"', () => {
    const unread = workflowStartText(workflow(STEPS), null)
    expect(startedPhrase(unread)).toBe('started — not read')
    const never = workflowStartText(workflow([step('plan', [], null)]), new Map())
    expect(startedPhrase(never)).not.toMatch(/^started (never|not)/)
    const known = workflowStartText(workflow(STEPS), new Map(OWN.map((t) => [t.id, t])), T0)
    expect(startedPhrase(known)).toMatch(/^started \S/)
    expect(startedPhrase(known)).not.toMatch(/not read/)
  })
})
