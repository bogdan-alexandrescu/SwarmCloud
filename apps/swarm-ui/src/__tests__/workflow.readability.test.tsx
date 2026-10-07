/**
 * THE WORKFLOWS SCREEN, READ BY SOMEONE NEW TO IT (wave 3, lane B29): issues
 * #108, #110, #111 and #113, filed against the old console and checked here
 * against the rebuilt one (#432).
 *
 *  #108  the graph's three edge styles are named on screen, in a key.
 *  #110  a step picked in the Table or the Timeline is inspected next to its
 *        row -- docked in the right-hand column at 1100px and up, directly
 *        under the row below that. (The Graph has docked it since #330; the
 *        board test pins that.)
 *  #111  the list sorts by age and by failed count, and shows when each
 *        workflow started and how long it ran. Its state filter, `Failed`
 *        included, already exists and workflow.v2.test.tsx pins it.
 *  #113  while the list loads, its toolbar is already on screen, disabled, and
 *        the rows are skeletons in the table's own geometry, visible in both
 *        themes.
 */
import STYLES from '../styles.css?raw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { cascade } from './cssgate'
import { colour, contrast, resolveSheet, resolveVars, tokenTables, type RGBA } from './spaceprobe'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen } = await import('../Workflows')
const wl = await import('../workflowlist')

const T0 = Date.parse('2026-10-01T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

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

function workflow(id: string, state: TaskState, owner: string, steps: WorkflowStep[], ageSeconds = 600): Workflow {
  return {
    workflow_id: id,
    state,
    tenant_id: 'eng',
    stored_state: state,
    state_source: 'derived',
    rollup: {
      state,
      complete: true,
      reason: 'test',
      counts: {},
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: steps.length,
    },
    created_at: iso(-ageSeconds),
    updated_at: iso(-60),
    submitted_by: owner,
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps,
  }
}

/**
 * Four workflows. `wf_new` is running (submitted 10 minutes ago); `wf_one`
 * failed one step (submitted an hour ago, ended 20 minutes later); `wf_two`
 * failed two (submitted two hours ago); `wf_done` finished (three hours ago,
 * ran 1h 30m by its last step's end).
 */
function fixture(): WorkflowBoard {
  const running = workflow('wf_new', 'RUNNING', 'priya', [
    step('plan', [], { task_id: 't_plan' }),
    step('core', ['plan'], { task_id: 't_core', input_from: { plan: 'plan.md' } }),
    step('verify', ['core']),
  ])
  const one = workflow(
    'wf_one',
    'FAILED',
    'alex',
    [step('scan', [], { task_id: 't_scan' }), step('fix', ['scan'], { task_id: 't_fix' })],
    3600,
  )
  const two = workflow(
    'wf_two',
    'FAILED',
    'alex',
    [step('a', [], { task_id: 't_a' }), step('b', [], { task_id: 't_b' }), step('c', ['a', 'b'])],
    7200,
  )
  const done = workflow('wf_done', 'SUCCEEDED', 'priya', [step('lint', [], { task_id: 't_lint' })], 10800)
  const tasks = new Map<string, Task>([
    ['t_plan', task('t_plan', 'SUCCEEDED', { started_at: iso(-580), completed_at: iso(-400) })],
    ['t_core', task('t_core', 'RUNNING', { started_at: iso(-300) })],
    ['t_scan', task('t_scan', 'SUCCEEDED', { started_at: iso(-3500), completed_at: iso(-3000) })],
    ['t_fix', task('t_fix', 'FAILED', { started_at: iso(-2900), completed_at: iso(-2400), last_error: 'exit 1' })],
    ['t_a', task('t_a', 'FAILED', { started_at: iso(-7100), completed_at: iso(-7000), last_error: 'exit 2' })],
    ['t_b', task('t_b', 'FAILED', { started_at: iso(-7100), completed_at: iso(-6900), last_error: 'exit 3' })],
    ['t_lint', task('t_lint', 'SUCCEEDED', { started_at: iso(-10700), completed_at: iso(-10800 + 5400) })],
  ])
  return { workflows: [running, one, two, done], taskById: tasks, statesDetail: null }
}

/** `matchMedia` answering as a viewport `width` px wide would, for `(max-width: Npx)`. */
function viewport(width: number) {
  vi.stubGlobal('matchMedia', (query: string) => {
    const max = /max-width:\s*(\d+)px/.exec(query)
    return {
      matches: max !== null && width <= Number(max[1]),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }
  })
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: fixture(), fetchedAt: T0 } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadWorkflow.mockResolvedValue({
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no such workflow' },
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function Routed({ initial, onView }: { initial: string; onView?: (v: string) => void }) {
  const [view, setView] = useState(initial)
  return (
    <WorkflowsScreen
      view={view}
      onView={(v) => {
        onView?.(v)
        setView(v)
      }}
    />
  )
}

function rowIds(): string[] {
  return [...document.querySelectorAll<HTMLElement>('.wfl-table tbody tr')].map((r) => r.dataset.workflow ?? '')
}

type Theme = 'dark' | 'light'
const THEMES: readonly Theme[] = ['dark', 'light']
const SHEET_VARS = tokenTables(STYLES)
const SHEETS: Readonly<Record<Theme, string>> = {
  dark: resolveSheet(STYLES, 'dark'),
  light: resolveSheet(STYLES, 'light'),
}

function varColour(name: string, theme: Theme): RGBA {
  const v = SHEET_VARS[theme].get(name)
  expect(v, `${name} is not declared`).toBeDefined()
  const c = colour(resolveVars(v!, SHEET_VARS[theme]))
  expect(c, `${name} does not resolve to a colour`).not.toBeNull()
  return c!
}

const at = (el: Element, prop: string, width = 1440, theme: Theme = 'dark', reducedMotion = false) =>
  cascade(SHEETS[theme], el, prop, { width, theme, reducedMotion }).winner?.value ?? null

// ---------------------------------------------------------------------------
// #108 -- the key
// ---------------------------------------------------------------------------

describe('#108: the workflow graph has a key for its edge kinds', () => {
  it('names order, staged and declared beside the canvas, each drawn with the edge rule it names', async () => {
    render(<WorkflowsScreen view="wf=wf_new" />)
    await waitFor(() => expect(document.querySelector('.wf-canvas')).toBeTruthy())
    const key = screen.getByRole('list', { name: 'Edge key' })
    // NOT HIDDEN: the edge SVG is aria-hidden, so the key is the one place the
    // three kinds are named for a screen reader too.
    expect(key.closest('[aria-hidden="true"]')).toBeNull()
    const items = within(key).getAllByRole('listitem')
    expect(items.map((li) => li.querySelector('.wf-key-word')?.textContent)).toEqual(['order', 'staged', 'declared'])
    // EACH SAMPLE IS AN EDGE, painted by the same classes the canvas uses, so
    // the key cannot drift from what it explains.
    const groups = items.map((li) => li.querySelector('svg g')?.getAttribute('class'))
    expect(groups).toEqual(['wf-link is-order', 'wf-link is-data is-staged', 'wf-link is-data is-declared'])
    for (const li of items) {
      expect(li.querySelector('svg path.wf-edge')).toBeTruthy()
      expect(li.querySelector('svg')!.getAttribute('aria-hidden')).toBe('true')
    }
    // A first-time reader can say what a heavy dashed edge means.
    expect(items[2]!.getAttribute('title')).toMatch(/declared.*not.*reported/i)
    expect(items[1]!.getAttribute('title')).toMatch(/staged/i)
    expect(items[0]!.getAttribute('title')).toMatch(/order/i)
  })

  it('is drawn even where the zoom control is not, on a chain the zoom cannot change', async () => {
    render(<WorkflowsScreen view="wf=wf_one" />)
    await waitFor(() => expect(document.querySelector('.wf-canvas')).toBeTruthy())
    expect(document.querySelector('.wf-zoom-seg'), 'a two-step chain grew a zoom control').toBeNull()
    expect(screen.getByRole('list', { name: 'Edge key' })).toBeTruthy()
  })

  it('reads at the 12px floor with no letter-spacing, and the declared sample is dashed in both themes', async () => {
    render(<WorkflowsScreen view="wf=wf_new" />)
    await waitFor(() => expect(document.querySelector('.wf-key')).toBeTruthy())
    const word = document.querySelector('.wf-key-word')!
    for (const theme of THEMES) {
      const size = cascade(SHEETS[theme], word, ['font-size', 'font'], { width: 1440, theme }).winner?.value ?? ''
      const pxs = [...size.matchAll(/(\d+(?:\.\d+)?)px/g)].map((m) => Number(m[1]))
      expect(pxs.length, `no px size on the key's word in ${theme}`).toBeGreaterThan(0)
      expect(pxs[0]!).toBeGreaterThanOrEqual(12)
      expect(at(word, 'letter-spacing', 1440, theme) ?? 'normal').toMatch(/^(normal|0|0px)$/)
      const declared = document.querySelectorAll('.wf-key svg g')[2]!.querySelector('.wf-edge')!
      expect(at(declared, 'stroke-dasharray', 1440, theme)).toBe('6 4')
    }
  })
})

// ---------------------------------------------------------------------------
// #110 -- the inspector next to the picked step
// ---------------------------------------------------------------------------

describe('#110: the workflow step inspector opens next to the picked step', () => {
  it('docks in the right-hand column at 1440 in the Table, labelled with its workflow', async () => {
    viewport(1440)
    render(<Routed initial="wf=wf_one&tab=table" />)
    // The page arrives with the failed step picked.
    const panel = await waitFor(() => {
      const p = document.querySelector<HTMLElement>('.wf-split.has-panel > .wf-panel')
      expect(p).toBeTruthy()
      return p!
    })
    const split = panel.parentElement!
    const main = split.querySelector(':scope > .wf-split-main')!
    expect(main.querySelector('.wf-table tr[data-step="fix"]')).toBeTruthy()
    expect(main.querySelector('.wf-inspect'), 'the inspector is still under the table').toBeNull()
    const inspect = panel.querySelector<HTMLElement>('.wf-inspect')!
    expect(inspect.getAttribute('aria-label')).toBe('Step fix of wf_one')
    expect(inspect.querySelector('.wf-inspect-wf')!.textContent).toContain('wf_one')
    // And the shipped sheet docks it at 1100 and up.
    expect(at(split, 'display', 1440)).toBe('grid')
    expect(at(split, 'display', 1100)).toBe('grid')
  })

  it('docks in the Timeline too', async () => {
    viewport(1440)
    render(<Routed initial="wf=wf_one&tab=timeline" />)
    await waitFor(() => expect(document.querySelector('.wf-split.has-panel > .wf-panel .wf-inspect')).toBeTruthy())
    expect(document.querySelector('.wf-split-main .wf-timeline')).toBeTruthy()
  })

  it('opens directly under the picked row below 1100, in the Table', async () => {
    viewport(1099)
    render(<Routed initial="wf=wf_one&tab=table" />)
    const row = await waitFor(() => {
      const r = document.querySelector<HTMLElement>('.wf-table tr[data-step="fix"]')
      expect(r?.nextElementSibling?.querySelector('.wf-inspect')).toBeTruthy()
      return r!
    })
    const under = row.nextElementSibling as HTMLTableRowElement
    expect(under.className).toBe('wf-inline-row')
    const cell = under.querySelector('td')!
    expect(cell.colSpan).toBe(row.querySelectorAll('td').length)
    expect(document.querySelector('.wf-panel'), 'a docked panel as well').toBeNull()
    expect(document.querySelectorAll('.wf-inspect')).toHaveLength(1)
  })

  it('opens directly under the picked track below 1100, in the Timeline, across the whole grid', async () => {
    viewport(390)
    render(<Routed initial="wf=wf_one&tab=timeline" />)
    const track = await waitFor(() => {
      const t = document.querySelector<HTMLElement>('.wf-tl-track[data-step="fix"]')
      expect(t?.nextElementSibling?.querySelector('.wf-inspect')).toBeTruthy()
      return t!
    })
    const under = track.nextElementSibling as HTMLElement
    expect(under.className).toBe('wf-tl-inline')
    expect(at(under, 'grid-column', 390)).toBe('1 / -1')
    expect(document.querySelector('.wf-panel')).toBeNull()
  })

  it('moves the inspector when another row is picked, and puts it down when the row is picked again', async () => {
    viewport(800)
    render(<Routed initial="wf=wf_one&tab=table" />)
    await waitFor(() => expect(document.querySelector('.wf-inline-row .wf-inspect')).toBeTruthy())
    fireEvent.click(document.querySelector<HTMLButtonElement>('.wf-pick[data-step="scan"]')!)
    const scan = document.querySelector<HTMLElement>('.wf-table tr[data-step="scan"]')!
    await waitFor(() => expect(scan.nextElementSibling?.className).toBe('wf-inline-row'))
    expect(document.querySelectorAll('.wf-inspect')).toHaveLength(1)
    fireEvent.click(document.querySelector<HTMLButtonElement>('.wf-pick[data-step="scan"]')!)
    expect(document.querySelector('.wf-inspect')).toBeNull()
    expect(document.querySelector('.wf-inline-row')).toBeNull()
  })

  it('keeps the Table sort when a step is picked, because the table is not remounted', async () => {
    viewport(1440)
    render(<Routed initial="wf=wf_one&tab=table" />)
    await waitFor(() => expect(document.querySelector('.wf-panel')).toBeTruthy())
    const order = () => [...document.querySelectorAll<HTMLElement>('.wf-table tbody tr[data-step]')].map((r) => r.dataset.step)
    const before = order()
    // Sort by step id until the order is the reverse of the one the table
    // starts in, so a remount (which resets the sort) would show.
    const byStep = within(document.querySelector('.wf-table thead')!).getByRole('button', { name: /^Step/ })
    fireEvent.click(byStep)
    if (order()[0] === before[0]) fireEvent.click(byStep)
    const sorted = order()
    expect(sorted).toEqual([...before].reverse())
    // Put the pick down and pick again: the order survives both.
    fireEvent.click(document.querySelector<HTMLButtonElement>('.wf-pick[data-step="fix"]')!)
    expect(document.querySelector('.wf-panel')).toBeNull()
    expect(order()).toEqual(sorted)
    fireEvent.click(document.querySelector<HTMLButtonElement>('.wf-pick[data-step="scan"]')!)
    expect(document.querySelector('.wf-panel')).toBeTruthy()
    expect(order()).toEqual(sorted)
  })
})

// ---------------------------------------------------------------------------
// #111 -- sort, start and duration
// ---------------------------------------------------------------------------

describe('#111: the workflows list sorts, and shows start and duration', () => {
  it('carries the sort in the address, written only off its default', () => {
    expect(wl.parseWorkflowQuery('sort=failed').sort).toBe('failed')
    expect(wl.parseWorkflowQuery('sort=bogus').sort).toBe('state')
    expect(wl.parseWorkflowQuery('').sort).toBe('state')
    expect(wl.workflowQueryString({ ...wl.EMPTY_QUERY, sort: 'oldest', state: 'failed' })).toBe('state=failed&sort=oldest')
    expect(wl.workflowQueryString({ ...wl.EMPTY_QUERY, sort: 'state' })).toBe('')
    // The back link returns to the sorted list.
    expect(wl.listHref({ ...wl.EMPTY_QUERY, sort: 'failed' })).toBe('/workflows?sort=failed')
  })

  it('counts failed steps from the task read', () => {
    const b = fixture()
    expect(b.workflows.map((w) => wl.failedSteps(w, b.taskById))).toEqual([0, 1, 2, 0])
  })

  it('sorts by state, newest, oldest and failed count', () => {
    const b = fixture()
    const ids = (sort: import('../workflowlist').WorkflowSort) =>
      wl.sortWorkflows(b.workflows, sort, b.taskById).map((w) => w.workflow_id)
    // The V2 grouping (#503, workflows.html frame 0): running first, then succeeded, failed after them.
    expect(ids('state')).toEqual(['wf_new', 'wf_done', 'wf_one', 'wf_two'])
    expect(ids('newest')).toEqual(['wf_new', 'wf_one', 'wf_two', 'wf_done'])
    expect(ids('oldest')).toEqual(['wf_done', 'wf_two', 'wf_one', 'wf_new'])
    expect(ids('failed')).toEqual(['wf_two', 'wf_one', 'wf_new', 'wf_done'])
  })

  it('measures a workflow from its submission to its last step’s end, and a running one up to now', () => {
    const b = fixture()
    const [running, one, , done] = b.workflows
    expect(wl.workflowDuration(one!, b.taskById, T0)).toMatchObject({ ms: 1200_000, live: false, text: '20m 0s' })
    expect(wl.workflowDuration(done!, b.taskById, T0)).toMatchObject({ ms: 5400_000, live: false, text: '1h 30m' })
    expect(wl.workflowDuration(running!, b.taskById, T0)).toMatchObject({ ms: 600_000, live: true, text: '10m 0s so far' })
    // A finished workflow whose step tasks were not read has no end to measure to.
    expect(wl.workflowDuration(done!, null, T0)).toMatchObject({ ms: null, text: 'not read' })
    // Nor does one whose finished step recorded no end.
    const noEnd = new Map(b.taskById)
    noEnd.set('t_lint', task('t_lint', 'SUCCEEDED', { completed_at: null }))
    expect(wl.workflowDuration(done!, noEnd, T0)).toMatchObject({ ms: null, text: 'not recorded' })
  })

  it('draws Submitted and Duration columns, and one choice re-sorts the rows', async () => {
    const onView = vi.fn()
    render(<Routed initial="" onView={onView} />)
    await waitFor(() => expect(rowIds()).toHaveLength(4))
    const heads = [...document.querySelectorAll('.wfl-table thead th')].map((th) => th.textContent)
    // Submitted since QA G3-20 (2026-10-07): the column every order sorts by.
    expect(heads).toContain('Submitted')
    expect(heads).toContain('Duration')
    expect(heads).not.toContain('Age')
    const row = document.querySelector<HTMLElement>('tr[data-workflow="wf_one"]')!
    expect(row.querySelector('.wfl-dur')!.textContent).toBe('20m 0s')
    expect(row.querySelector('.wfl-submitted')!.getAttribute('title')).toMatch(/submitted/)
    expect(document.querySelector('tr[data-workflow="wf_new"] .wfl-dur')!.textContent).toBe('10m 0s so far')

    fireEvent.change(screen.getByRole('combobox', { name: /sort/ }), { target: { value: 'failed' } })
    expect(onView).toHaveBeenLastCalledWith('sort=failed')
    await waitFor(() => expect(rowIds()).toEqual(['wf_two', 'wf_one', 'wf_new', 'wf_done']))
    expect(document.querySelector('.wfl-foot')!.textContent).toMatch(/most failed steps first/)
  })

  it('shows only the failing workflows, each with its duration, in one click', async () => {
    render(<Routed initial="" />)
    await waitFor(() => expect(rowIds()).toHaveLength(4))
    fireEvent.click(within(screen.getByRole('group', { name: 'Which workflows' })).getByText('Failed'))
    await waitFor(() => expect(rowIds()).toEqual(['wf_one', 'wf_two']))
    for (const id of rowIds()) {
      expect(document.querySelector(`tr[data-workflow="${id}"] .wfl-dur`)!.textContent).toMatch(/^\d+[smhd]/)
    }
  })
})

// ---------------------------------------------------------------------------
// #113 -- loading
// ---------------------------------------------------------------------------

describe('#113: the workflows list loading skeleton', () => {
  it('draws the toolbar, disabled, and the table head before the read lands, then rows where the skeleton was', async () => {
    let land: (r: Result<WorkflowBoard>) => void = () => {}
    api.loadWorkflowBoard.mockReturnValue(new Promise<Result<WorkflowBoard>>((r) => (land = r)))
    render(<Routed initial="state=failed" />)
    const list = document.querySelector<HTMLElement>('.wfl.is-loading')
    expect(list, 'no list frame while loading').toBeTruthy()
    expect(list!.getAttribute('aria-busy')).toBe('true')
    // The shared full-width bars are not drawn here; the list's own are.
    expect(document.querySelectorAll('.skeleton')).toHaveLength(0)
    const filters = list!.querySelector('.wfl-filters')!
    const controls = [...filters.querySelectorAll<HTMLButtonElement | HTMLInputElement | HTMLSelectElement>('button, input, select')]
    expect(controls.length).toBeGreaterThanOrEqual(7)
    expect(controls.every((c) => c.disabled)).toBe(true)
    // The address's choice is already shown.
    expect(within(filters as HTMLElement).getByText('Failed').closest('button')!.getAttribute('aria-pressed')).toBe('true')
    const skelHeads = [...list!.querySelectorAll('.wfl-table thead th')].map((th) => th.textContent)
    const skelRows = [...list!.querySelectorAll('.wfl-table tbody tr')]
    expect(skelRows.length).toBeGreaterThan(0)
    for (const tr of skelRows) {
      expect(tr.className).toBe('wfl-row is-skel')
      expect(tr.querySelectorAll('td')).toHaveLength(skelHeads.length)
      expect(tr.querySelectorAll('.wfl-skel').length).toBeGreaterThan(0)
      // No number in a skeleton: it answers nothing.
      expect(tr.textContent).toBe('')
    }

    land({ status: 'ok', data: fixture(), fetchedAt: T0 })
    await waitFor(() => expect(rowIds()).toEqual(['wf_one', 'wf_two']))
    // The same toolbar and the same head: the first row lands where the
    // first skeleton row was.
    const loaded = document.querySelector<HTMLElement>('.wfl')!
    expect(loaded.className).toBe('wfl')
    expect([...loaded.querySelectorAll('.wfl-table thead th')].map((th) => th.textContent)).toEqual(skelHeads)
    expect(loaded.querySelectorAll('.wfl-filters button, .wfl-filters input, .wfl-filters select')).toHaveLength(controls.length)
  })

  it('is visible in both themes: the bars clear 1.5:1 against the page and the card, and they sweep', () => {
    render(
      <table>
        <tbody>
          <tr className="wfl-row is-skel">
            <td>
              <span className="wfl-skel" />
            </td>
          </tr>
        </tbody>
      </table>,
    )
    const bar = document.querySelector('.wfl-skel')!
    for (const theme of THEMES) {
      const fill = colour(at(bar, 'background-color', 1440, theme) ?? '')
      expect(fill, `the bar has no resolvable fill in ${theme}`).not.toBeNull()
      for (const ground of ['--bg', '--surface'] as const) {
        const ratio = contrast(fill!, varColour(ground, theme))
        expect(ratio, `${theme}: the bar against ${ground} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(1.5)
      }
      expect(at(bar, 'animation', 1440, theme)).toMatch(/ctl-sweep/)
      expect(at(bar, 'animation', 1440, theme, true)).toBe('none')
    }
  })
  // EVERY PAGE, NOT ONLY THIS ONE: the shared bar every other screen's
  // `SkeletonRows` draws sat on `--surface-2` at about 1.03:1 in light. The
  // issue asked for the contrast step itself to change, so the same floor holds
  // for it, against the same two grounds.
  it('the shared skeleton bar every other screen draws clears the same floor in both themes, and sweeps', () => {
    render(<div className="skeleton ctl-skeleton-row" />)
    const bar = document.querySelector('.skeleton')!
    for (const theme of THEMES) {
      const fill = colour(at(bar, 'background-color', 1440, theme) ?? '')
      expect(fill, `the shared bar has no resolvable fill in ${theme}`).not.toBeNull()
      for (const ground of ['--bg', '--surface', '--surface-2'] as const) {
        const ratio = contrast(fill!, varColour(ground, theme))
        expect(ratio, `${theme}: the shared bar against ${ground} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(ground === '--surface-2' ? 1.2 : 1.5)
      }
      expect(at(bar, 'animation', 1440, theme)).toMatch(/ctl-sweep/)
      expect(at(bar, 'animation', 1440, theme, true)).toBe('none')
    }
  })
})
