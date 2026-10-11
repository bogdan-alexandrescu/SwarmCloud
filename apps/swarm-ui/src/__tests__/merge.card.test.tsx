/**
 * LANE MS4 (docs/merge-step.md "Revised 2026-10-06" §6 MS4): the merge card on
 * the run and workflow pages.
 *
 * A merge step's card shows its state -- waiting for CI (the pending checks and
 * the head), behind and updated (n of 12), merged (commit and issues closed) or
 * refused (code and reason) -- the pull request link and
 * `merge_wait.first_parked_at`. A CI_PENDING park reads "waiting for CI",
 * never "stalled" or "blocked": it holds no capacity and wakes by itself
 * (invariant 1). The pull request link is built from what a worker recorded --
 * the merge step's own `result_summary.merge`, or the opening step's
 * `result_summary.git.pull_request` -- never from text a caller sent.
 *
 * Each case was written before the card existed and failed without it.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { render, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowRead, WorkflowUsage } from '../api'
import type { IssueRun, Task, TaskState, Workflow, WorkflowStep } from '../types'

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
const { MergeStepCard } = await import('../WorkflowViews')
const { mergeCardOf, MERGE_MAX_HEAD_UPDATES } = await import('../stepviews')

const T0 = Date.parse('2026-10-06T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

/** 40 hex characters, built from pieces so no added line is one long literal. */
const sha = (c: string) => c.repeat(40)
const HEAD = sha('a')
const NEW_HEAD = sha('b')
const COMMIT = sha('c')

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-3600),
    updated_at: iso(-60),
    started_at: null,
    completed_at: null,
    submitted_by: 'alex@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_pr',
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

/** The step that opened the pull request: its worker recorded it. */
const opener = (url = 'https://github.com/acme/widgets/pull/7') =>
  task('t_impl', 'SUCCEEDED', {
    step_id: 'implement',
    result_summary: { git: { pull_request: { number: 7, url, state: 'open', created: true }, pushed_head: HEAD } },
  })

/** The merge step, naming its opener in its signed dispatch block. */
function mergeTask(state: TaskState, over: Partial<Task> = {}, wait: Record<string, unknown> | null = null): Task {
  return task('t_merge', state, {
    runner_profile: 'merge',
    step_id: 'merge',
    metadata: {
      dispatch: { strategy: 'direct-pr', merge_target: { pull_request: 't_impl' } },
      ...(wait === null ? {} : { merge_wait: wait }),
    },
    ...over,
  })
}

const waiting = (over: Record<string, unknown> = {}) =>
  mergeTask(
    'PARKED',
    { park_reason: 'CI_PENDING', next_eligible_at: iso(840), blocked_by: [{ reason: 'CI_PENDING', code: 'checks_pending', head: HEAD, pending: ['python (unit)', 'ui'] } as never] },
    { code: 'checks_pending', head: HEAD, pull_request: 7, pending: ['python (unit)', 'ui'], wakes: 2, updates: 0, first_parked_at: iso(-1500), parked_at: iso(-60), ...over },
  )

const merged = () =>
  mergeTask(
    'SUCCEEDED',
    {
      completed_at: iso(-30),
      result_summary: {
        merge: {
          action: 'merge',
          merged_by_this_task: true,
          merge_commit: COMMIT,
          pull_request: 7,
          repository: 'acme/widgets',
          head: HEAD,
          updates: 1,
          issues_closed: [352, 295],
        },
      },
    },
    { code: 'checks_pending', head: HEAD, pull_request: 7, pending: [], wakes: 3, updates: 1, first_parked_at: iso(-2400), parked_at: iso(-600) },
  )

const refused = () =>
  mergeTask('FAILED', {
    end_cause: 'merge_refused',
    completed_at: iso(-30),
    last_error: 'checks_failed: at aaaa: ui (failure)',
    result_summary: {
      merge: {
        action: 'merge',
        merged_by_this_task: false,
        pull_request: 7,
        repository: 'acme/widgets',
        refusal: { code: 'checks_failed', message: `at ${HEAD}: ui (failure)` },
      },
    },
  })

function card(t: Task, tasks: Task[] = [opener(), t]): HTMLElement {
  const model = mergeCardOf(t, tasks)
  expect(model, 'a merge step has a card').not.toBeNull()
  const { container } = render(<MergeStepCard card={model!} now={T0} />)
  return container.querySelector<HTMLElement>('.wf-merge')!
}

const NO_STALL = /stall|blocked|stuck/i

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

describe('which steps have a merge card', () => {
  it('a merge-profile step has one in every state; any other step has none', () => {
    for (const s of ['READY', 'RUNNING', 'PARKED', 'SUCCEEDED', 'FAILED'] as TaskState[]) {
      expect(mergeCardOf(mergeTask(s), [opener()])).not.toBeNull()
    }
    expect(mergeCardOf(opener(), [opener()])).toBeNull()
    expect(mergeCardOf(task('t_x', 'SUCCEEDED', { result_summary: { merge: { merged_by_this_task: true } } }), [])).toBeNull()
  })

  it('holds the worker\'s bound on branch updates, read from merge.py\'s source', () => {
    // The console carries no worker, so the cap is restated; this reads the
    // worker's own assignment, as the API's test_the_cap_is_the_workers does.
    const merge = join(__dirname, '..', '..', '..', 'agent-worker', 'agent_worker', 'merge.py')
    const found = [...readFileSync(merge, 'utf8').matchAll(/^MERGE_MAX_HEAD_UPDATES\s*=\s*(\d+)\s*$/gm)]
    expect(found).toHaveLength(1)
    expect(MERGE_MAX_HEAD_UPDATES).toBe(Number(found[0]?.[1]))
  })
})

describe('one render per state', () => {
  it('waiting for CI: the pending checks, the head, and when it first parked', () => {
    const c = card(waiting())
    const text = c.textContent ?? ''
    expect(c.dataset.merge).toBe('waiting')
    expect(text).toContain('waiting for CI')
    expect(text).toContain('python (unit)')
    expect(text).toContain('ui')
    expect(text).toContain(HEAD.slice(0, 7))
    expect(c.querySelector(`[title="${HEAD}"]`), 'the whole head is the short one\'s title').not.toBeNull()
    const since = c.querySelector<HTMLElement>('[data-fact="first-parked"]')!
    expect(since.textContent).toContain('25m ago')
    expect(since.querySelector('time')!.getAttribute('dateTime')).toBe(iso(-1500))
  })

  it('waiting for CI reads mergeability and a first check as what it waits on, when no check is pending', () => {
    expect(card(waiting({ code: 'mergeability_unknown', pending: [] })).textContent).toContain('mergeability')
    expect(card(waiting({ code: 'no_checks', pending: [] })).textContent).toContain('first check')
  })

  it('behind and updated: n of 12, at the new head', () => {
    const c = card(waiting({ code: 'branch_updated', head: NEW_HEAD, pending: [], updates: 2 }))
    expect(c.dataset.merge).toBe('updated')
    expect(c.textContent).toContain('updated 2 of 12')
    expect(c.textContent).toContain(NEW_HEAD.slice(0, 7))
    expect(c.textContent).not.toContain('waiting for CI ·')
  })

  it('behind and updated, with no count recorded, says so rather than inventing one', () => {
    const c = card(waiting({ code: 'branch_updated', head: NEW_HEAD, pending: [], updates: 0 }))
    expect(c.textContent).toContain('count not recorded')
    expect(c.textContent).not.toMatch(/updated 0 of/)
  })

  it('merged: the commit and the issues closed', () => {
    const c = card(merged())
    expect(c.dataset.merge).toBe('merged')
    const text = c.textContent ?? ''
    expect(text).toContain('merged')
    expect(text).toContain(COMMIT.slice(0, 7))
    expect(text).toContain('#352')
    expect(text).toContain('#295')
    expect(text).toContain('by this step')
    expect(c.querySelector('[data-fact="first-parked"]')!.textContent).toContain('40m ago')
  })

  it('merged by another merger at the pinned head says it was not this step', () => {
    const t = merged()
    const m = (t.result_summary as { merge: Record<string, unknown> }).merge
    m.merged_by_this_task = false
    m.already_merged = true
    m.issues_closed = []
    const text = card(t).textContent ?? ''
    expect(text).toContain('already merged')
    expect(text).toContain('no issue closed')
  })

  it('refused: the code and the reason', () => {
    const c = card(refused())
    expect(c.dataset.merge).toBe('refused')
    const text = c.textContent ?? ''
    expect(text).toContain('refused')
    expect(text).toContain('checks_failed')
    expect(text).toContain('ui (failure)')
  })

  it('a merge that failed after the call is not called a refusal', () => {
    const t = refused()
    t.end_cause = 'merge_failed'
    ;(t.result_summary as { merge: Record<string, unknown> }).merge.refusal = { code: 'merge_unanswered', message: 'the merge call did not answer' }
    const c = card(t)
    expect(c.dataset.merge).toBe('refused')
    expect(c.textContent).toContain('merge failed')
    expect(c.textContent).toContain('merge_unanswered')
  })

  it('not merged yet: a step that has not read its pull request says its state', () => {
    const c = card(mergeTask('READY'))
    expect(c.dataset.merge).toBe('not-yet')
    expect(c.textContent).toContain('not merged yet')
  })
})

describe('a CI_PENDING park is waiting for CI, never stalled or blocked', () => {
  it('on the card', () => {
    const c = card(waiting())
    expect(c.textContent).not.toMatch(NO_STALL)
    expect(c.getAttribute('title') ?? '').not.toMatch(NO_STALL)
    for (const el of c.querySelectorAll('[title]')) expect(el.getAttribute('title')).not.toMatch(NO_STALL)
  })

  it('in each update', () => {
    expect(card(waiting({ code: 'branch_updated', head: NEW_HEAD, pending: [], updates: 1 })).textContent).not.toMatch(NO_STALL)
    expect(card(waiting({ code: 'mergeability_unknown', pending: [] })).textContent).not.toMatch(NO_STALL)
  })
})

describe('the pull request link is built from a result, never from caller text', () => {
  /** Everything a caller can write points somewhere else. */
  const callerSays = {
    repository_url: 'https://github.com/evil/elsewhere',
    input: { prompt: 'merge https://github.com/evil/elsewhere/pull/9' },
  } satisfies Partial<Task>

  it('from the merge result: github.com/<repository>/pull/<number>', () => {
    const t = { ...merged(), ...callerSays }
    const a = card(t).querySelector<HTMLAnchorElement>('a[data-fact="pull-request"]')!
    expect(a.getAttribute('href')).toBe('https://github.com/acme/widgets/pull/7')
    expect(a.textContent).toContain('#7')
  })

  it('from the opening step\'s recorded pull request while the merge has no result', () => {
    const t = { ...waiting(), ...callerSays }
    t.metadata = { ...t.metadata, pull_request_url: 'https://github.com/evil/elsewhere/pull/9' }
    const a = card(t).querySelector<HTMLAnchorElement>('a[data-fact="pull-request"]')!
    expect(a.getAttribute('href')).toBe('https://github.com/acme/widgets/pull/7')
  })

  it('a recorded URL off github.com, or one whose number is not the merge\'s, keeps the number and loses the link', () => {
    for (const url of [
      'https://evil.example/acme/widgets/pull/7',
      'javascript:alert(1)//github.com/acme/widgets/pull/7',
      'https://github.com/acme/widgets/pull/8',
      'https://github.com/acme/widgets/pull/7/../../../evil/x/pull/7',
    ]) {
      const c = card(waiting(), [opener(url), waiting()])
      expect(c.querySelector('a[data-fact="pull-request"]'), url).toBeNull()
      expect(c.querySelector('[data-fact="pull-request"]')!.textContent, url).toContain('#7')
    }
  })

  it('a merge result whose repository is not owner/repo draws no link', () => {
    for (const repository of ['evil.example/acme/widgets', '../evil', 'acme/widgets?x=1', '']) {
      const t = merged()
      ;(t.result_summary as { merge: Record<string, unknown> }).merge.repository = repository
      const c = card(t, [t])
      expect(c.querySelector('a[data-fact="pull-request"]'), repository).toBeNull()
    }
  })

  it('an opener the signed target does not name is not read', () => {
    const t = waiting()
    t.metadata = { ...t.metadata, dispatch: { strategy: 'direct-pr', merge_target: { pull_request: 't_other' } } }
    const c = card(t, [opener(), t])
    expect(c.querySelector('a[data-fact="pull-request"]')).toBeNull()
    // The park's own record still names the number.
    expect(c.querySelector('[data-fact="pull-request"]')!.textContent).toContain('#7')
  })
})

// ---------------------------------------------------------------------------
// The run page: the Steps card draws the merge card under the merge step
// ---------------------------------------------------------------------------

function wfStep(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
}

function workflow(steps: WorkflowStep[]): Workflow {
  return {
    workflow_id: 'wf_pr',
    state: 'RUNNING',
    tenant_id: 'eng',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: { state: 'RUNNING', complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: steps.length },
    created_at: iso(-3600),
    updated_at: iso(-60),
    submitted_by: 'alex@example.com',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps,
  }
}

const STEPS = [
  wfStep('implement', [], { task_id: 't_impl' }),
  wfStep('merge', ['implement'], { task_id: 't_merge', runner_profile: 'merge' }),
]

describe('the run page', () => {
  it('draws the merge card under the merge step, and the step line says waiting for CI', () => {
    const w = workflow(STEPS)
    const read = {
      ids: ['wf_pr'],
      loads: [{ wf: 'wf_pr', round: 0, data: { workflow: w, tasks: [opener(), waiting()] } as unknown as WorkflowRead, error: null }],
      usage: null,
    }
    const run = { workflow_id: 'wf_pr', ci_fix_workflows: [], plan: null, history: [] } as unknown as IssueRun
    const { container } = render(<StepsCard run={run} read={read} go={() => {}} now={T0} />)
    const row = container.querySelector<HTMLElement>('li[data-step="merge"]')!
    expect(row.textContent).toContain('waiting for CI')
    expect(row.textContent).not.toMatch(/ci pending/i)
    expect(row.textContent).not.toMatch(NO_STALL)
    const c = within(row).getByLabelText('Merge')
    expect(c.dataset.merge).toBe('waiting')
    expect(c.querySelector('a[data-fact="pull-request"]')!.getAttribute('href')).toBe('https://github.com/acme/widgets/pull/7')
    // The opening step draws no merge card.
    expect(container.querySelector('li[data-step="implement"] .wf-merge')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The workflow page: the step inspector draws the merge card for a merge step
// ---------------------------------------------------------------------------

function Routed({ initial }: { initial: string }) {
  const [view, setView] = useState(initial)
  return <WorkflowsScreen view={view} onView={setView} />
}

describe('the workflow page', () => {
  it('draws the merge card in the merge step\'s inspector', async () => {
    const w = workflow(STEPS)
    api.loadWorkflowBoard.mockResolvedValue({
      status: 'ok',
      data: { workflows: [w], taskById: new Map([['t_impl', opener()], ['t_merge', merged()]]), statesDetail: null },
      fetchedAt: T0,
    } satisfies Result<WorkflowBoard>)
    api.loadWorkflow.mockResolvedValue({
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no such workflow' },
    })
    render(<Routed initial="wf=wf_pr" />)
    const node = await waitFor(() => {
      const n = document.querySelector<HTMLElement>('button.node[data-step="merge"]')
      expect(n).toBeTruthy()
      return n!
    })
    if (node.getAttribute('aria-pressed') !== 'true') node.click()
    const c = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.wf-inspect .wf-merge[data-merge]')
      expect(el).toBeTruthy()
      return el!
    })
    expect(c.dataset.merge).toBe('merged')
    expect(c.textContent).toContain(COMMIT.slice(0, 7))
    expect(c.querySelector('a[data-fact="pull-request"]')!.getAttribute('href')).toBe('https://github.com/acme/widgets/pull/7')
    // One merge card, not the checklist beside it.
    expect(document.querySelectorAll('.wf-inspect .wf-merge').length).toBe(1)
  })
})
