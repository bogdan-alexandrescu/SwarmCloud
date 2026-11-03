/**
 * THE WORKFLOWS LIST, REDRAWN AS THE PICKED WORK REDESIGN (owner's pick:
 * "Graph with a docked inspector, plus the Attention inbox's row of what needs
 * you"; frame graph-workflows-rest; part of #2688).
 *
 * All, Running, Parked, Failed and Done with counts; "Needs you first" as the
 * default order; rows of state, name, the step bar with done of total, a
 * Needs-you column, spend, owner and changed; a row opens the workflow's
 * summary in the docked inspector, its name opens the page. The reads stay
 * the list read alone.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowBoard } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { painted } from './marks'
import type { CascadeEnv } from './cssgate'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflow: vi.fn(),
  loadWorkflowUsage: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen } = await import('../Workflows')
const wl = await import('../workflowlist')

const T0 = Date.parse('2026-11-03T12:00:00.000Z')
const iso = (s: number) => new Date(T0 + s * 1000).toISOString()

function step(step_id: string, task_id: string | null, over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id, ...over }
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
    started_at: iso(-500),
    completed_at: null,
    submitted_by: 'priya@example.com',
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

function workflow(id: string, state: TaskState | null, steps: WorkflowStep[], over: Partial<Workflow> = {}): Workflow {
  return {
    workflow_id: id,
    state: state ?? 'RUNNING',
    tenant_id: 'eng',
    stored_state: state ?? 'RUNNING',
    state_source: state === null ? 'stored' : 'derived',
    ...(state === null
      ? {}
      : {
          rollup: {
            state,
            complete: true,
            reason: 'test',
            counts: {},
            unreadable_steps: [],
            unstarted_steps: [],
            steps_read: steps.length,
          },
        }),
    created_at: iso(-3600),
    updated_at: iso(-120),
    submitted_by: 'priya@example.com',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps,
    ...over,
  }
}

const running = workflow('wf_run', 'RUNNING', [step('plan', 't_plan'), step('build', 't_build')], { created_at: iso(-60) })
const parked = workflow('wf_park', 'PARKED', [step('ask', 't_ask'), step('merge', 't_merge')], { created_at: iso(-600) })
const failed = workflow('wf_fail', 'FAILED', [step('scan', 't_scan'), step('fix', 't_fix')], { created_at: iso(-1200), submitted_by: 'alex@example.com' })
const done = workflow('wf_done', 'SUCCEEDED', [step('lint', 't_lint')], { created_at: iso(-30) })
const unknown = workflow('wf_unknown', null, [step('x', null)], { created_at: iso(-10) })

const tasks = new Map<string, Task>([
  ['t_plan', task('t_plan', 'SUCCEEDED', { workflow_id: 'wf_run' })],
  ['t_build', task('t_build', 'RUNNING', { workflow_id: 'wf_run' })],
  ['t_ask', task('t_ask', 'PARKED', { workflow_id: 'wf_park', park_reason: 'AWAITING_INPUT' })],
  ['t_merge', task('t_merge', 'PARKED', { workflow_id: 'wf_park', park_reason: 'HUMAN_REQUIRED' })],
  ['t_scan', task('t_scan', 'SUCCEEDED', { workflow_id: 'wf_fail' })],
  ['t_fix', task('t_fix', 'FAILED', { workflow_id: 'wf_fail', last_error: 'exit 1: 4 tests failing' })],
  ['t_lint', task('t_lint', 'SUCCEEDED', { workflow_id: 'wf_done' })],
])

function board(): WorkflowBoard {
  return { workflows: [running, parked, failed, done, unknown], taskById: tasks, statesDetail: null }
}

beforeEach(() => {
  cleanup()
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: board(), fetchedAt: T0 } satisfies Result<WorkflowBoard>)
  api.loadWorkflow.mockReset()
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

describe('the buckets', () => {
  it('are All, Running, Parked, Failed and Done; a workflow of unknown state is in All only', () => {
    expect(wl.BUCKET_FILTERS).toEqual(['all', 'running', 'parked', 'failed', 'done'])
    expect(wl.listBucketOf(running)).toBe('running')
    expect(wl.listBucketOf(parked)).toBe('parked')
    expect(wl.listBucketOf(failed)).toBe('failed')
    expect(wl.listBucketOf(done)).toBe('done')
    expect(wl.listBucketOf(unknown)).toBeNull()
    expect(wl.bucketCounts([running, parked, failed, done, unknown])).toEqual({ all: 5, running: 1, parked: 1, failed: 1, done: 1 })
  })

  it('reads an old `state=finished` link as Done', () => {
    expect(wl.parseWorkflowQuery('state=finished').state).toBe('done')
    expect(wl.parseWorkflowQuery('state=parked').state).toBe('parked')
  })
})

describe('what needs you, per workflow', () => {
  it('counts failed steps, steps waiting on an answer and steps held for a person, from the list read', () => {
    expect(wl.needsOf(failed, tasks)).toEqual({ failed: 1, questions: 0, held: 0 })
    expect(wl.needsOf(parked, tasks)).toEqual({ failed: 0, questions: 1, held: 1 })
    expect(wl.needsOf(running, tasks)).toEqual({ failed: 0, questions: 0, held: 0 })
  })

  it('is not known, never zero, without the steps read', () => {
    expect(wl.needsOf(running, null)).toBeNull()
    expect(wl.needsOf(unknown, tasks)).toBeNull()
  })

  it('orders "Needs you first" by what waits on the person, then by state', () => {
    const order = wl.sortWorkflows([done, running, failed, parked], 'state', tasks).map((w) => w.workflow_id)
    expect(order).toEqual(['wf_park', 'wf_fail', 'wf_run', 'wf_done'])
    expect(wl.SORT_LABEL.state).toBe('needs you first')
  })
})

async function list() {
  render(<WorkflowsScreen view="" />)
  await waitFor(() => expect(document.querySelectorAll('.wfg-table tbody tr').length).toBe(5))
}

describe('the list', () => {
  it('draws the five buckets with counts and the needs-you-first order', async () => {
    await list()
    const seg = screen.getByRole('group', { name: 'Which workflows' })
    for (const [word, n] of [['All', 5], ['Running', 1], ['Parked', 1], ['Failed', 1], ['Done', 1]] as const) {
      expect(within(seg).getByRole('button', { name: new RegExp(`^${word}\\b.*${n}`) })).toBeTruthy()
    }
    expect((screen.getByLabelText('sort') as HTMLSelectElement).selectedOptions[0]?.textContent).toBe('Needs you first')
    const heads = [...document.querySelectorAll('.wfg-table thead th')].map((th) => th.textContent)
    expect(heads).toEqual(['State', 'Workflow', 'Steps', 'Needs you', 'Spent', 'Owner', 'Changed'])
  })

  it('draws a row with its step bar, done of total, its needs and when it changed', async () => {
    await list()
    const row = document.querySelector<HTMLElement>('tr[data-workflow="wf_park"]')!
    expect(row.querySelector('[data-col="steps"]')?.textContent).toContain('0/2')
    const needs = row.querySelector<HTMLElement>('[data-col="needs"]')!
    expect(needs.querySelector('[data-need="questions"]')?.textContent).toContain('1')
    expect(needs.querySelector('[data-need="held"]')?.textContent).toContain('1')
    expect(row.querySelector('[data-col="changed"]')?.textContent).toBe('2m ago')
    const fail = document.querySelector<HTMLElement>('tr[data-workflow="wf_fail"] [data-col="needs"]')!
    expect(fail.querySelector('[data-need="failed"]')?.textContent).toContain('1')
    // Nothing waits on the person: a dash, not a zero.
    expect(document.querySelector('tr[data-workflow="wf_done"] [data-col="needs"]')?.textContent).toBe('—')
    // Unknown: said so.
    expect(document.querySelector('tr[data-workflow="wf_unknown"] [data-col="needs"]')?.textContent).toBe('not known')
  })

  it('opens the summary in the inspector on a row, folds Owner and Changed into it, and makes no read of its own', async () => {
    await list()
    const reads = api.loadWorkflowBoard.mock.calls.length
    expect(document.querySelector('.wfg.has-inspector')).toBeNull()
    fireEvent.click(document.querySelector<HTMLElement>('tr[data-workflow="wf_fail"] [data-col="state"]')!)
    const dock = await screen.findByRole('complementary', { name: 'Workflow inspector' })
    expect(within(dock).getByRole('link', { name: 'Open workflow' }).getAttribute('href')).toBe('/workflows/wf_fail')
    expect(dock.textContent).toContain('changed 2m ago')
    expect(dock.textContent).toContain('alex')
    expect(dock.textContent).toContain('failed at fix')
    expect(document.querySelector('.wfg.has-inspector')).not.toBeNull()
    // A plain table's row ignores aria-selected; aria-current says which row is open (as AccountsTable's does).
    expect(document.querySelector('tr[data-workflow="wf_fail"]')?.getAttribute('aria-selected')).toBeNull()
    expect(document.querySelector('tr[data-workflow="wf_fail"]')?.getAttribute('aria-current')).toBe('true')
    expect(api.loadWorkflow).not.toHaveBeenCalled()
    expect(api.loadWorkflowBoard.mock.calls.length).toBe(reads)
  })

  it('opens the page from the row name, not the inspector', async () => {
    await list()
    const link = document.querySelector<HTMLAnchorElement>('tr[data-workflow="wf_run"] [data-col="workflow"] a')!
    expect(link.getAttribute('href')).toBe('/workflows/wf_run')
  })
})

describe('review findings on the list (part of #2688)', () => {
  // The comment on `open` promised that a pick the filters drop closes the
  // inspector; it was looked up among every row read, so it stayed open.
  // MUTATION: look the pick up in `all` again.
  it('closes the inspector when the filters drop the open workflow', async () => {
    await list()
    fireEvent.click(document.querySelector<HTMLElement>('tr[data-workflow="wf_fail"] [data-col="state"]')!)
    await screen.findByRole('complementary', { name: 'Workflow inspector' })
    expect(document.querySelector('.wfg.has-inspector')).not.toBeNull()
    fireEvent.change(screen.getByLabelText('Find a workflow or step'), { target: { value: 'wf_run' } })
    await waitFor(() => expect(document.querySelectorAll('.wfg-table tbody tr').length).toBe(1))
    expect(document.querySelector('.wfg.has-inspector')).toBeNull()
  })
})

describe('the list draws at 1440, 580 beside the inspector and 390 (review findings, part of #2688)', () => {
  const WIDE: CascadeEnv = { width: 1440, container: 1100 }
  const BESIDE: CascadeEnv = { width: 1440, container: 580 }
  const PHONE: CascadeEnv = { width: 390, container: 356 }

  function table(open = false): HTMLElement {
    const root = document.createElement('div')
    root.className = `wfg${open ? ' has-inspector' : ''}`
    root.innerHTML =
      '<div class="wfg-tools"><label class="wfg-pick"><span class="vh">owner</span><select></select></label></div>' +
      '<div class="table-wrap wfg-scroll"><table class="wfg-table"><thead><tr>' +
      '<th data-col="state">State</th><th data-col="workflow">Workflow</th><th data-col="steps">Steps</th><th data-col="needs">Needs you</th>' +
      '<th data-col="spent">Spent</th><th data-col="owner">Owner</th><th data-col="changed">Changed</th></tr></thead><tbody>' +
      '<tr class="wfg-row"><td class="wfg-state" data-col="state"><span class="sk-st is-bad"><svg></svg><span class="sk-st-w">failed</span></span></td>' +
      '<td class="wfg-name" data-col="workflow"><a>name</a></td><td class="wfg-steps" data-col="steps"><span class="wfg-bar"></span></td>' +
      '<td class="wfg-needs" data-col="needs"><span class="wfg-need is-bad"><span>✗</span> 1<span class="vh"> failed</span></span></td>' +
      '<td data-col="spent">$1</td><td data-col="owner">alex</td><td data-col="changed">2m ago</td></tr></tbody></table></div>'
    document.body.replaceChildren(root)
    return root
  }

  // The class `vh` was drawn only inside `.pp-page`: on the list the words
  // "owner", "profile", "sort" and every Needs-you word were painted.
  // MUTATION: drop the `.wfg .vh` rule.
  it('hides the filter labels and the Needs-you words visually, inside a positioned box', () => {
    const root = table()
    for (const vh of root.querySelectorAll('.vh')) {
      for (const env of [WIDE, PHONE]) {
        expect(painted(vh, 'position', env)).toBe('absolute')
        expect(painted(vh, 'width', env)).toBe('1px')
        expect(painted(vh, 'overflow', env)).toBe('hidden')
        expect(painted(vh.parentElement!, 'position', env)).toBe('relative')
      }
    }
  })

  // At 1440 with the inspector open the left pane is about 580px: only
  // Owner is hidden and Changed moved to the inspector; Spent stays (frame
  // graph-workflows-rest). MUTATION: hide Spent at 600 again.
  it('keeps Spent beside the open inspector and hides only Owner and Changed', () => {
    const root = table(true)
    const td = (col: string) => root.querySelector(`td[data-col="${col}"]`)!
    expect(painted(td('spent'), 'display', BESIDE)).not.toBe('none')
    expect(painted(td('owner'), 'display', BESIDE)).toBe('none')
    expect(painted(td('changed'), 'display', BESIDE)).toBe('none')
    for (const col of ['state', 'workflow', 'steps', 'needs']) expect(painted(td(col), 'display', BESIDE)).not.toBe('none')
  })

  // At 390 the phone row was 411px in a 356px box and the Needs-you cell
  // was clipped off its right edge. The row is laid out fixed: the state is
  // its glyph (the word stays read), steps and needs hold their widths and
  // the name takes the rest. MUTATION: drop the phone's fixed layout or a
  // column width.
  it('fits state, name, steps and needs in a 356px box at 390', () => {
    const root = table()
    const tbl = root.querySelector('table')!
    expect(painted(tbl, 'table-layout', PHONE)).toBe('fixed')
    expect(painted(tbl, 'table-layout', WIDE)).not.toBe('fixed')
    const th = (col: string) => root.querySelector(`th[data-col="${col}"]`)!
    const px = (v: string | null) => {
      const m = /^(\d+)px$/.exec(v ?? '')
      expect(m, `${v} is a width in px`).not.toBeNull()
      return Number(m![1])
    }
    const fixed = px(painted(th('state'), 'width', PHONE)) + px(painted(th('steps'), 'width', PHONE)) + px(painted(th('needs'), 'width', PHONE))
    // What is left for the name, at least 120px of a 356px box.
    expect(356 - fixed).toBeGreaterThanOrEqual(120)
    expect(painted(root.querySelector('td[data-col="workflow"]')!, 'min-width', PHONE)).toBe('0')
    const word = root.querySelector('td[data-col="state"] .sk-st-w')!
    expect(painted(word, 'position', PHONE)).toBe('absolute')
    expect(painted(word, 'position', WIDE)).not.toBe('absolute')
    expect(painted(root.querySelector('td[data-col="state"]')!, 'position', PHONE)).toBe('relative')
    for (const col of ['steps', 'needs']) {
      const td = root.querySelector(`td[data-col="${col}"]`)!
      expect(painted(td, 'overflow', PHONE)).toBe('hidden')
      expect(painted(td, 'text-overflow', PHONE)).toBe('ellipsis')
    }
    for (const col of ['spent', 'owner', 'changed']) expect(painted(root.querySelector(`td[data-col="${col}"]`)!, 'display', PHONE)).toBe('none')
  })
})
