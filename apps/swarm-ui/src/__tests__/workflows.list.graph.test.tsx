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
import { lengthPx, paddingX, textPx } from './tablefit'

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

// A step with a task carries the needs block the list serves
// (`codec.step_needs`, PR #3147); `blockless` is one an API older than it served.
function step(step_id: string, task_id: string | null, over: Partial<WorkflowStep> = {}): WorkflowStep {
  const block = task_id === null ? {} : { task_summary: { needs: {} } }
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id, ...block, ...over }
}
const blockless = (step_id: string, task_id: string): WorkflowStep => step(step_id, task_id, { task_summary: { state: 'PARKED' } } as Partial<WorkflowStep>)

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
  })

  // THE ROW AND THE BAR CLASSIFY ALIKE (third review of PR #3147): a step
  // served without its needs block counted 0 questions and 0 held, and a
  // PARKED HUMAN_REQUIRED one '1 held', while the bar read "not known".
  // MUTATION: count a block-less step's kinds as 0, or list its park as a hand-off.
  it('reads questions and held not known, never 0, for a step served without its needs block', () => {
    const old = workflow('wf_old', 'PARKED', [blockless('merge', 't_merge'), step('fix', 't_fix')])
    expect(wl.needsOf(old, tasks)).toEqual({ failed: 1, questions: null, held: null })
    const only = workflow('wf_old', 'PARKED', [blockless('merge', 't_merge')])
    expect(wl.needsOf(only, tasks)).toEqual({ failed: 0, questions: null, held: null })
  })

  // A workflow whose state could not be derived still has its steps read:
  // the bar counted its failed step while the row said "not known".
  // MUTATION: put back `derivedStateOf(w) === null` in `needsOf`.
  it('counts a failed step of a workflow whose state could not be derived, as the bar does', () => {
    const underived = workflow('wf_underived', null, [step('scan', 't_scan'), step('fix', 't_fix')])
    expect(wl.derivedStateOf(underived)).toBeNull()
    expect(wl.needsOf(underived, tasks)).toEqual({ failed: 1, questions: 0, held: 0 })
    expect(wl.needsOf(unknown, tasks)).toEqual({ failed: 0, questions: 0, held: 0 })
  })

  // MUTATION: rank a row whose needs are not known as 0.
  it('ranks a row whose needs are not known after a row with some and before one with none', () => {
    const old = workflow('wf_old', 'PARKED', [blockless('merge', 't_merge')], { created_at: iso(-5000) })
    const order = wl.sortWorkflows([done, running, old, failed], 'state', tasks).map((w) => w.workflow_id)
    expect(order).toEqual(['wf_fail', 'wf_old', 'wf_run', 'wf_done'])
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
    // A workflow whose state was not derived, with no step that ran: nothing waits.
    expect(document.querySelector('tr[data-workflow="wf_unknown"] [data-col="needs"]')?.textContent).toBe('—')
  })

  // MUTATION: draw a block-less step's row as '—', or drop the "+" after a counted mark.
  it("draws a block-less step's kinds not known in its row, as the bar does", async () => {
    const old = workflow('wf_old', 'PARKED', [blockless('merge', 't_merge')])
    const mixed = workflow('wf_mixed', 'FAILED', [blockless('merge', 't_merge'), step('fix', 't_fix')])
    api.loadWorkflowBoard.mockResolvedValue({
      status: 'ok',
      data: { workflows: [old, mixed], taskById: tasks, statesDetail: null },
      fetchedAt: T0,
    } satisfies Result<WorkflowBoard>)
    render(<WorkflowsScreen view="" />)
    await waitFor(() => expect(document.querySelectorAll('.wfg-table tbody tr').length).toBe(2))
    const cell = (id: string) => document.querySelector<HTMLElement>(`tr[data-workflow="${id}"] [data-col="needs"]`)!
    expect(cell('wf_old').querySelector('[data-need="held"]')).toBeNull()
    expect(cell('wf_old').textContent).toBe('not known: questions and holds')
    expect(cell('wf_mixed').querySelector('[data-need="failed"]')?.textContent).toContain('1')
    expect(cell('wf_mixed').querySelector('[data-need="unknown"]')?.textContent).toBe('+ questions and holds not known')
    const bar = document.querySelector<HTMLElement>('.wfg section[aria-label="Needs you"]')!
    const chip = (word: string) => [...bar.querySelectorAll('.ny-chip')].find((c) => c.querySelector('.ny-word')?.textContent === word)!
    for (const word of ['Question', 'Merge waiting', 'Your turn in a browser']) expect(chip(word).textContent).toContain('not known')
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

// THE TABLE NEVER EXCEEDS ITS PANE (review finding on PR #3147, part of
// #2688). With the inspector open at 1440 the pane was 580px and the table
// 627px: Spent's "$0.518 3 of 4 reported" set its column to 179px and the
// pane, `overflow: hidden`, cut the header, the figures and the note. jsdom
// lays nothing out, so this sums what the cascade declares: below 1024px of
// pane the table is laid out fixed, every column but the name has a width,
// and the shown columns' widths and paddings leave the name its floor inside
// the pane's 1px borders, at every pane width the docked inspector can leave
// (MAIN_MIN is 360px less the pane's 12px padding), open or folded. Widths
// are tokens-free px, so the two themes lay out the same.
// MUTATION: put the table back to `table-layout: auto`, drop a column's
// width, put the note back on the value's line, or move a threshold so a
// column stays shown where the sum no longer fits.
describe('the list never exceeds its pane, with the inspector open or folded (review finding on PR #3147)', () => {
  const NOTE = '3 of 4 reported'
  function row(open: boolean): HTMLElement {
    const root = document.createElement('div')
    root.className = `wfg${open ? ' has-inspector' : ''}`
    root.innerHTML =
      '<div class="table-wrap wfg-scroll"><table class="wfg-table"><thead><tr>' +
      '<th data-col="state">State</th><th data-col="workflow">Workflow</th><th data-col="steps">Steps</th><th data-col="needs">Needs you</th>' +
      '<th class="num" data-col="spent">Spent</th><th data-col="owner">Owner</th><th data-col="changed">Changed</th></tr></thead><tbody>' +
      '<tr class="wfg-row"><td class="wfg-state" data-col="state"><span class="sk-st is-live"><svg></svg><span class="sk-st-w">awaiting input</span></span></td>' +
      '<td class="wfg-name" data-col="workflow"><a>name</a></td>' +
      '<td class="wfg-steps" data-col="steps"><span class="wfg-bar"><span class="c-bar"></span><small>12/12</small></span></td>' +
      '<td class="wfg-needs" data-col="needs"><span class="wfg-need is-bad"><span>✗</span> 1<span class="vh"> failed</span></span></td>' +
      `<td class="num wfg-spent" data-col="spent"><span class="wf-spend">$0.518 <span class="wf-spend-cov">${NOTE}</span></span></td>` +
      '<td class="wfg-owner" data-col="owner">swarm-pipeline</td><td class="wfg-changed" data-col="changed">not recorded</td></tr></tbody></table></div>'
    document.body.replaceChildren(root)
    return root
  }
  const ORDER = ['owner', 'changed', 'spent']
  const px = (v: string | null, what: string): number => {
    const n = lengthPx(v, 0)
    expect(n, `${what}: ${v} is a width in px`).not.toBeNull()
    return n!
  }

  /**
   * A fixed column's width as the browser lays it out: the stylesheet's `*`
   * rule makes every cell `box-sizing: border-box`, so its 8px paddings sit
   * inside its width (the fourth review measured State at 116px, its declared
   * width), and a content-box cell adds them.
   */
  function columnPx(th: HTMLElement, env: CascadeEnv, what: string): number {
    const [pl, pr] = paddingX(th, env)
    const width = px(painted(th, 'width', env), what)
    return painted(th, 'box-sizing', env) === 'border-box' ? Math.max(width, pl + pr) : width + pl + pr
  }

  /** What the shown columns leave the name in a pane `pane` px wide, and which columns are hidden. */
  function fit(root: HTMLElement, env: CascadeEnv, pane: number): { name: number; hidden: string[] } {
    const tbl = root.querySelector('table')!
    expect(painted(tbl, 'table-layout', env), `fixed at ${pane}`).toBe('fixed')
    let used = 0
    const hidden: string[] = []
    for (const th of root.querySelectorAll<HTMLElement>('thead th')) {
      const col = th.getAttribute('data-col')!
      if (painted(th, 'display', env) === 'none') {
        hidden.push(col)
        expect(painted(root.querySelector(`td[data-col="${col}"]`)!, 'display', env), `${col} td at ${pane}`).toBe('none')
        continue
      }
      if (col === 'workflow') continue
      used += columnPx(th, env, `${col} at ${pane}`)
    }
    // The scroll box's two 1px borders.
    return { name: pane - 2 - used, hidden }
  }

  // Each band's narrowest pane is where its sum is tightest (the shown
  // columns are the same across the band and the pane only shrinks), and the
  // pane one pixel under each threshold is where the next column must have
  // gone; the cascade costs seconds a width, so these are the widths read.
  // [pane, hidden folded, hidden open]: the same, since a column gives way
  // by the pane's width alone, never by a pick being open (third review).
  const O = 'owner'
  const C = 'changed'
  const S = 'spent'
  const LADDER: readonly [number, string[], string[]][] = [
    [1024, [], []],
    [861, [], []],
    [860, [O], [O]],
    [721, [O], [O]],
    [720, [O, C], [O, C]],
    [613, [O, C], [O, C]],
    [612, [O, C], [O, C]],
    [580, [O, C], [O, C]],
    [571, [O, C], [O, C]],
    [570, [O, C], [O, C]],
    [546, [O, C], [O, C]],
    [545, [O, C], [O, C]],
    [420, [O, C], [O, C]],
    [394, [O, C], [O, C]],
    [393, [O, C, S], [O, C, S]],
    [348, [O, C, S], [O, C, S]],
  ]
  it('leaves the name its floor from 1024 down to 348, open and folded, giving way Owner, Changed, Spent', { timeout: 240_000 }, () => {
    for (const open of [false, true]) {
      const root = row(open)
      let before: string[] = []
      for (const [pane, folded, opened] of LADDER) {
        const env: CascadeEnv = { width: 1440, container: pane }
        const { name, hidden } = fit(root, env, pane)
        const what = `${open ? 'open' : 'folded'} at ${pane}`
        expect([...hidden].sort(), what).toEqual([...(open ? opened : folded)].sort())
        expect(name, `${what}: the name has ${name}px`).toBeGreaterThanOrEqual(112)
        // Columns give way in the documented order, and none comes back narrower.
        const dropped = ORDER.filter((c) => hidden.includes(c))
        expect(dropped, what).toEqual(ORDER.slice(0, dropped.length))
        for (const c of before) expect(hidden, `${c} came back ${what}`).toContain(c)
        before = hidden
      }
    }
  })

  // THE FOURTH REVIEW OF PR #3147: at 1440 with the inspector docked the box
  // is 580px, and a row not in the inspector showed its partial figure with
  // "3 of 4 reported" clipped to 1px, so the caveat was drawn nowhere; yet
  // with Spent at its full 112px and the note drawn the name keeps
  // 580 - 2 - (116 + 116 + 88 + 112) = 146px, above its 112px floor.
  // MUTATION: hide the note at a fixed pane above the name's floor (612 again).
  it("draws a partial row's note under its figure beside the docked inspector, in a 580px box", () => {
    const root = row(true)
    const env: CascadeEnv = { width: 1440, container: 580 }
    const { name, hidden } = fit(root, env, 580)
    expect(hidden).not.toContain('spent')
    expect(name).toBeGreaterThanOrEqual(112)
    const th = root.querySelector<HTMLElement>('th[data-col="spent"]')!
    const width = px(painted(th, 'width', env), 'spent')
    expect(width).toBe(112)
    expect(textPx('Spent', th, env)).toBeLessThanOrEqual(width)
    expect(textPx('$12.92', root.querySelector('.wf-spend')!, env)).toBeLessThanOrEqual(width)
    const note = root.querySelector('.wf-spend-cov')!
    expect(painted(note, 'display', env)).toBe('block')
    expect(painted(note, 'position', env)).not.toBe('absolute')
    expect(painted(note, 'width', env)).not.toBe('1px')
    expect(painted(note, 'clip-path', env)).toBeNull()
    expect(textPx(NOTE, note, env), `"${NOTE}" on its own line`).toBeLessThanOrEqual(width)
  })

  // The note gives way where the name would lose its floor with Spent at its
  // full width, and nowhere above: the threshold is the ladder's own sum (the
  // name's 112px floor, the box's two 1px borders, State, Steps, Needs and
  // Spent at their full widths), read from the cascade, not a fixed number.
  // Below it the figure stays drawn at 72px and the note is read, not drawn;
  // the figure's title and accessible label carry the caveat (Workflows.tsx's
  // spend cell). MUTATION: move the note's threshold off the derived sum.
  it('hides the note only where the name would lose its floor, from the ladder', () => {
    const root = row(true)
    // A band where Owner and Changed are gone and Spent is at its full width.
    const full: CascadeEnv = { width: 1440, container: 700 }
    let shown = 0
    for (const col of ['state', 'steps', 'needs', 'spent']) {
      shown += columnPx(root.querySelector<HTMLElement>(`th[data-col="${col}"]`)!, full, col)
    }
    const threshold = 112 + 2 + shown
    expect(threshold, 'the name keeps its floor at the review\'s 580px box').toBeLessThanOrEqual(580)
    const note = root.querySelector('.wf-spend-cov')!
    const td = root.querySelector<HTMLElement>('td[data-col="spent"]')!
    const th = root.querySelector<HTMLElement>('th[data-col="spent"]')!
    for (const pane of [threshold, threshold + 1, 571, 580, 592, 612, 613, 640]) {
      const env: CascadeEnv = { width: 1440, container: pane }
      expect(painted(note, 'position', env), `drawn at ${pane}`).not.toBe('absolute')
      expect(painted(note, 'width', env), `drawn at ${pane}`).not.toBe('1px')
      expect(px(painted(th, 'width', env), `spent at ${pane}`), `${pane}`).toBe(112)
      if (painted(th, 'display', env) !== 'none') {
        expect(fit(root, env, pane).name, `the name with the note drawn at ${pane}`).toBeGreaterThanOrEqual(112)
      }
    }
    const below: CascadeEnv = { width: 1440, container: threshold - 1 }
    expect(painted(note, 'position', below)).toBe('absolute')
    expect(painted(note, 'width', below)).toBe('1px')
    expect(painted(note, 'overflow', below)).toBe('hidden')
    expect(painted(td, 'position', below)).toBe('relative')
    expect(px(painted(th, 'width', below), 'spent below')).toBe(72)
  })

  // THE FIFTH REVIEW OF PR #3147, which the owner chose to follow up (4
  // November 2026): at 1280 with the inspector docked the pane's box is 420px,
  // and the phone row's rule (≤570) hid Spent on every row before the note's
  // rule (≤545) could draw the figure alone; yet a 72px figure-only Spent
  // leaves the name 420 - 2 - (28 + 92 + 88 + 72) = 138px, above its 112px
  // floor. A real partial row from the list: its figure is drawn in 72px, its
  // "3 of 4 reported" is read, not drawn, and the figure's title and
  // accessible label carry the caveat. MUTATION: hide Spent at 570 again, or
  // put the figure back at its full 112px in the phone row.
  it("draws a partial row's figure-only Spent with its caveat labelled beside the docked inspector at 1280, in a 420px box", async () => {
    const spentTask = (id: string, state: TaskState, cost: number | null) =>
      task(id, state, { workflow_id: 'wf_partial', ...(cost === null ? {} : { cost_usd_total: cost, attempts: 1, attempts_read: 'ok' }) } as Partial<Task>)
    const partial = workflow('wf_partial', 'RUNNING', [step('a', 't_pa'), step('b', 't_pb'), step('c', 't_pc'), step('d', 't_pd')])
    const byId = new Map<string, Task>([
      ['t_pa', spentTask('t_pa', 'SUCCEEDED', 0.2)],
      ['t_pb', spentTask('t_pb', 'SUCCEEDED', 0.2)],
      ['t_pc', spentTask('t_pc', 'SUCCEEDED', 0.118)],
      ['t_pd', spentTask('t_pd', 'RUNNING', null)],
    ])
    api.loadWorkflowBoard.mockResolvedValue({
      status: 'ok',
      data: { workflows: [partial], taskById: byId, statesDetail: null },
      fetchedAt: T0,
    } satisfies Result<WorkflowBoard>)
    render(<WorkflowsScreen view="" />)
    await waitFor(() => expect(document.querySelector('tr[data-workflow="wf_partial"]')).not.toBeNull())
    // The rendered list's own `.wfg`, not a fixture an earlier test left in the body.
    const root = document.querySelector('tr[data-workflow="wf_partial"]')!.closest<HTMLElement>('.wfg')!
    const td = root.querySelector<HTMLElement>('tr[data-workflow="wf_partial"] td[data-col="spent"]')!
    const figure = td.querySelector<HTMLElement>('.wf-spend')!
    const caveat = td.querySelector<HTMLElement>('.wf-spend-cov')!
    expect(caveat.textContent).toBe('3 of 4 reported')
    for (const attr of ['title', 'aria-label']) {
      expect(figure.getAttribute(attr), attr).toMatch(/^3 of 4 steps that ran reported a cost\b.*not reported one/)
    }
    const env: CascadeEnv = { width: 1280, container: 420 }
    const th = root.querySelector<HTMLElement>('th[data-col="spent"]')!
    expect(painted(th, 'display', env)).not.toBe('none')
    expect(painted(td, 'display', env)).not.toBe('none')
    const width = columnPx(th, env, 'spent at 420')
    expect(width).toBe(72)
    const { name, hidden } = fit(root, env, 420)
    expect(hidden).not.toContain('spent')
    expect(name, `the name has ${name}px`).toBeGreaterThanOrEqual(112)
    const [pl, pr] = paddingX(td, env)
    const money = figure.firstChild!.textContent!.trim()
    expect(money).toBe('$0.518')
    for (const text of [money, '$12.92']) expect(textPx(text, figure, env), `"${text}" in the figure-only column`).toBeLessThanOrEqual(width - pl - pr)
    // Read, not drawn: still in the accessible text, never display: none.
    expect(painted(caveat, 'display', env)).not.toBe('none')
    expect(painted(caveat, 'visibility', env)).not.toBe('hidden')
    expect(painted(caveat, 'position', env)).toBe('absolute')
    expect(painted(caveat, 'width', env)).toBe('1px')
    expect(painted(td, 'position', env)).toBe('relative')
  })

  // Spent gives way only where even its figure-only width would cost the name
  // its floor: the threshold is the phone row's own sum (the name's 112px
  // floor, the box's two 1px borders, the state's glyph, Steps, Needs and the
  // figure-only Spent), read from the cascade, not a fixed number.
  // MUTATION: move Spent's threshold off the derived sum.
  it('hides Spent only where its figure-only width breaks the name\'s floor, from the ladder', () => {
    const root = row(true)
    const phone: CascadeEnv = { width: 1280, container: 500 }
    expect(painted(root.querySelector('thead')!, 'clip-path', phone), 'the phone row at 500').toBe('inset(50%)')
    let shown = 0
    for (const col of ['state', 'steps', 'needs', 'spent']) {
      shown += columnPx(root.querySelector<HTMLElement>(`th[data-col="${col}"]`)!, phone, col)
    }
    const threshold = 112 + 2 + shown
    expect(threshold, "the figure fits the fifth review's 420px box").toBeLessThanOrEqual(420)
    const th = root.querySelector<HTMLElement>('th[data-col="spent"]')!
    for (const pane of [threshold, threshold + 1, 420, 500, 545, 546, 570]) {
      const env: CascadeEnv = { width: 1280, container: pane }
      expect(painted(th, 'display', env), `spent at ${pane}`).not.toBe('none')
      expect(fit(root, env, pane).name, `the name with Spent drawn at ${pane}`).toBeGreaterThanOrEqual(112)
    }
    const below: CascadeEnv = { width: 1280, container: threshold - 1 }
    expect(painted(th, 'display', below)).toBe('none')
    expect(painted(root.querySelector('td[data-col="spent"]')!, 'display', below)).toBe('none')
  })

  // THE THIRD REVIEW OF PR #3147: with a workflow picked and the inspector
  // folded to its rail, `.wi-main` is 1005px at 1440 and its content box,
  // which the container queries measure, 993px (padding-right 12), yet
  // `.wfg.has-inspector` hid every row's coverage note and dropped Owner and
  // Changed, so a partial figure showed without its caveat. At 993px the
  // table is laid out fixed (≤1024) with every column drawn.
  // MUTATION: key a rule on `.has-inspector`.
  it('draws Owner, Changed and the note under the figure with a workflow picked and the inspector folded', () => {
    const root = row(true)
    const env: CascadeEnv = { width: 1440, container: 993 }
    expect(painted(root.querySelector('table')!, 'table-layout', env)).toBe('fixed')
    for (const col of ['owner', 'changed', 'spent']) {
      expect(painted(root.querySelector(`td[data-col="${col}"]`)!, 'display', env), col).not.toBe('none')
    }
    const note = root.querySelector('.wf-spend-cov')!
    expect(painted(note, 'display', env)).toBe('block')
    expect(painted(note, 'position', env)).not.toBe('absolute')
    expect(painted(note, 'width', env)).not.toBe('1px')
  })

  it('wraps the note under the value, never setting the column, while the inspector is folded', () => {
    const root = row(false)
    for (const pane of [1024, 900, 760, 640]) {
      const env: CascadeEnv = { width: 1440, container: pane }
      const note = root.querySelector('.wf-spend-cov')!
      expect(painted(root.querySelector('th[data-col="spent"]')!, 'display', env)).not.toBe('none')
      expect(painted(note, 'display', env), `${pane}`).toBe('block')
      expect(painted(note, 'contain', env), `${pane}`).toBe('inline-size')
      expect(painted(note, 'white-space', env), `${pane}`).toBe('normal')
      const width = px(painted(root.querySelector('th[data-col="spent"]')!, 'width', env), 'spent')
      expect(textPx(NOTE, note, env), `"${NOTE}" on its own line at ${pane}`).toBeLessThanOrEqual(width)
    }
  })

  // THE STEPS CELL NEVER CUTS ITS WORDS (review finding on PR #3147: the
  // 116px column held the 72px bar and "12/12" but cut an untrusted census's
  // "3 steps" to "3 step", at 1440 and 1280 open or folded, and at 390 in
  // 92px). The fixed layout lets the words go under the bar and wrap at their
  // spaces, so the column holds the bar, "12/12" and every word whole.
  // MUTATION: drop the wrap, or put `nowrap` back on the cell.
  it('holds the bar and every word of the step census whole, at every fixed pane, open and folded', { timeout: 180_000 }, () => {
    const LONGEST = ['12/12', '12 steps', '12 of 12 steps unread']
    for (const open of [false, true]) {
      const root = row(open)
      const small = root.querySelector<HTMLElement>('td[data-col="steps"] small')!
      for (const [width, pane] of [[1440, 1024], [1440, 580], [1280, 900], [1280, 520], [390, 356]] as const) {
        const env: CascadeEnv = { width, container: pane }
        const what = `${open ? 'open' : 'folded'} at ${width}, a ${pane}px pane`
        const th = root.querySelector<HTMLElement>('th[data-col="steps"]')!
        const td = root.querySelector<HTMLElement>('td[data-col="steps"]')!
        const [pl, pr] = paddingX(td, env)
        const inner = px(painted(th, 'width', env), `steps ${what}`) - pl - pr
        const bar = root.querySelector<HTMLElement>('td[data-col="steps"] .wfg-bar')!
        expect(painted(bar, 'flex-wrap', env), what).toBe('wrap')
        expect(painted(td, 'white-space', env), what).toBe('normal')
        expect(px(painted(root.querySelector('td[data-col="steps"] .c-bar')!, 'width', env), `the bar ${what}`), what).toBeLessThanOrEqual(inner)
        for (const text of LONGEST) {
          for (const word of text.split(' ')) expect(textPx(word, small, env), `"${word}" of "${text}" ${what}`).toBeLessThanOrEqual(inner)
        }
      }
    }
  })

  it('fits the phone row at 390 with the head kept read', () => {
    const root = row(false)
    const env: CascadeEnv = { width: 390, container: 356 }
    const { name, hidden } = fit(root, env, 356)
    expect(hidden).toEqual(expect.arrayContaining(ORDER))
    expect(name).toBeGreaterThanOrEqual(100)
  })
})
