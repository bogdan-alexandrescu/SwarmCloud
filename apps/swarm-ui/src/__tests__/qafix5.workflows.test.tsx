// QAFIX5-WORKFLOWS (part of #1680; visual QA run 5b, wf_025ae735511546d99eff,
// the fixture build of main at 58cd0c1e, 2026-10-22, 1440 and 390 px, light
// and dark). Each block names its finding and its #1680 comment:
//
//   QA-120 (6283740207)       a failed workflow's step card sat under the
//          graph, off screen at 1440x900; a click scrolled the clicked node
//          away; the card's heading was the workflow's title, not the step.
//   r2:WG-4, WG-6 (6298221306) 'same step / workflow 1 of 1' and 'generation
//          3' said nothing of what they count; 'why it failed' was a grey
//          caption.
//   r2:WG-7 (6298221770)      the Changes tab, 'This step’s changes ›' and
//          'Agent’s changes ›' said nothing of how their scopes differ.
//   5b:wg-light-L6 (6343766227) the Changes page said 'not measured' on every
//          step without saying whether nothing changed or nothing was
//          collected; a selected step still drew all six columns.
//   5b:wg-dark-D2 (6343767840) the head read 'cost not reported' while the
//          attempt read was in flight, and the list '—', then '$1.21'.
//
// Every assertion names the mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'
import { attempt } from './runfixture'
import { ownFromBoard } from './workflowsById'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
  loadArtifactContent: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen, WorkflowCard } = await import('../Workflows')
const { WorkflowChangesTab } = await import('../WorkflowChanges')

const T0 = Date.parse('2026-10-22T12:00:00.000Z')
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
    started_at: iso(-500),
    completed_at: iso(-100),
    submitted_by: 'qa@example.com',
    attempt_count: 3,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_fan',
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
    current_generation: 3,
    current_lease_id: null,
    ...over,
  }
}

const ERROR = 'exit status 2: pytest was interrupted: 1 error during collection'
const FAN = ['cold-start', 'fencing', 'allornothing', 'checkpoints', 'absentzero']

/** The fixture's fan-in: five steps, `checkpoints` failed, then `synthesis` on all five. No step reports a cost of its own. */
function fan(): { w: Workflow; tasks: Map<string, Task> } {
  const steps = [...FAN.map((id) => step(id, [], { task_id: `t_${id}` })), step('synthesis', FAN, { task_id: 't_synthesis' })]
  const tasks = new Map<string, Task>([
    ...FAN.map((id): [string, Task] => [
      `t_${id}`,
      task(`t_${id}`, id === 'checkpoints' ? 'FAILED' : 'SUCCEEDED', { step_id: id, last_error: id === 'checkpoints' ? ERROR : null }),
    ]),
    ['t_synthesis', task('t_synthesis', 'CANCELLED', { step_id: 'synthesis', started_at: null, attempt_count: 0, current_generation: 0 })],
  ])
  const w: Workflow = {
    workflow_id: 'wf_fan',
    state: 'FAILED',
    tenant_id: 'eng',
    stored_state: 'FAILED',
    state_source: 'derived',
    rollup: {
      state: 'FAILED',
      complete: true,
      reason: 'checkpoints',
      counts: { SUCCEEDED: 4, FAILED: 1, CANCELLED: 1 },
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: 6,
    },
    created_at: iso(-600),
    updated_at: iso(-60),
    submitted_by: 'qa@example.com',
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps,
  }
  return { w, tasks }
}

const hosts: HTMLElement[] = []

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  const f = fan()
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows: [f.w], taskById: f.tasks, statesDetail: null },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({
    status: 'ok',
    fetchedAt: T0,
    data: { attempts: [attempt(1, { exit_code: 2 }), attempt(2, { exit_code: 2 }), attempt(3, { exit_code: 2, cost_usd: 0.253 })] },
  })
  api.loadArtifactContent.mockReturnValue(new Promise(() => {}))
  api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
  Object.defineProperty(Element.prototype, 'scrollIntoView', { value: vi.fn(), configurable: true, writable: true })
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

afterEach(() => {
  cleanup()
  atWidth(realWidth)
  for (const h of hosts.splice(0)) h.remove()
})

async function page(): Promise<void> {
  render(<WorkflowsScreen view="wf=wf_fan" onView={() => {}} />)
  await waitFor(() => expect(document.querySelector('.wfp:not(.is-loading) .wfp-head')).toBeTruthy())
}

const WIDE: CascadeEnv = { width: 1440 }

const realWidth = window.innerWidth
function atWidth(px: number): void {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: px })
}

/**
 * THE CANVAS, RENDERED DIRECTLY (the Graph Work redesign, part of #2688): the
 * arrival pick and the `.wfp-fail-lead` banner are canvas chrome -- the
 * page's Graph tab is the stage grid now, with no arrival pick and no
 * banner at all -- so this renders `WorkflowCard` the way the old workflow
 * page used to, the failed step already picked.
 */
function PageCard() {
  const f = fan()
  const [stages, setStages] = useState<Record<string, boolean>>({})
  const [picked, setPicked] = useState<string | null>('checkpoints')
  return (
    <WorkflowCard
      workflow={f.w}
      taskById={f.tasks}
      usage={{ kind: 'ready', usage: null }}
      openStages={stages}
      onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
      picked={picked}
      onPick={(_id, stepId) => setPicked((p) => (p === stepId ? null : stepId))}
      page
      reload={() => {}}
    />
  )
}

/** The real page, the checkpoints chip picked from the grid: the inspector
 *  the page docks once a reader opens a step (there is no arrival pick). */
async function openOnPage(): Promise<HTMLElement> {
  atWidth(1440)
  await page()
  const chip = await waitFor(() => {
    const b = document.querySelector<HTMLButtonElement>('.sg [data-step="checkpoints"]')
    expect(b, 'no checkpoints chip on the stage grid').toBeTruthy()
    return b!
  })
  fireEvent.click(chip)
  return waitFor(() => {
    const d = document.querySelector<HTMLElement>('aside.wi-dock[aria-label="Step checkpoints"]')
    expect(d, 'the inspector did not open on checkpoints').toBeTruthy()
    return d!
  })
}

// ---------------------------------------------------------------------------
// QA-120: the card beside the graph, headed by its step
// ---------------------------------------------------------------------------

describe('QA-120: from 1280 the page docks the step card beside the graph, headed by the step', () => {
  it('lays the page’s graph and its card side by side at 1440, the card sticking in view; stacked below 1280', () => {
    const split = build('div.wf-card > div.wf-split.is-stack.has-panel', hosts)
    const card = build('div.wf-split.is-stack > aside.wf-panel', hosts)
    // MUTATION: put back the unscoped `.wf-card .wf-split.is-stack.has-panel
    // { display: block }` -- at 1440 the card is under the graph, ~900px down.
    expect(painted(split, 'display', WIDE)).toBe('grid')
    // MUTATION: put back the unscoped `position: static` on the stacked panel
    // -- the docked card scrolls away with the page instead of staying in view.
    expect(painted(card, 'position', WIDE)).toBe('sticky')
    // Below 1280 the graph needs the width: the card stacks under it (#503).
    // MUTATION: drop the 1279 block -- the stacked layout is gone at 1100.
    expect(painted(split, 'display', { width: 1100 })).toBe('block')
    expect(painted(card, 'position', { width: 1100 })).toBe('static')
  })

  it('heads the card with the step, not with the workflow the page is already titled by', async () => {
    // No arrival pick on the page any more: the reader opens checkpoints
    // themselves, from the stage grid, and the inspector draws its same head.
    const dock = await openOnPage()
    const head = dock.querySelector<HTMLElement>('.wi-head')!
    // The page's step card is the action bar and stat tiles (owner's pick,
    // 2026-11-05): the card itself (`section.sc-card`) draws no head of its
    // own any more -- the dock's own `.wi-head` already names the step and
    // its state, in plain text rather than a heading, so the card does not
    // repeat it.
    expect(head.querySelector('.wi-title-text')?.textContent).toBe('checkpoints')
    expect(head.querySelector('.wi-state')?.textContent).toMatch(/failed/)
    expect(head.textContent, 'the card repeats the workflow the page is titled by').not.toContain('wf_fan')
  })

  it('says the arrival control opens the card, not that it is "below"', async () => {
    // The arrival pick and its lead banner are canvas chrome, gone from the
    // page on purpose; `WorkflowCard` still carries the behaviour.
    render(<PageCard />)
    const lead = await screen.findByRole('region', { name: 'Why step checkpoints failed' })
    // MUTATION: put back 'Step details below: agent, logs, changes ↓' -- at
    // 1440 the card is beside the graph, and "below" sends the reader down.
    expect(within(lead).getByRole('button').textContent).not.toMatch(/below|↓/)
  })
})

// ---------------------------------------------------------------------------
// WG-4, WG-6: the card's words
// ---------------------------------------------------------------------------

describe('WG-4, WG-6: the step card says what its figures count', () => {
  it('says "why it failed" as an accessible group, with the whole error shown, not a grey caption', async () => {
    const dock = await openOnPage()
    const cause = await waitFor(() => {
      const el = dock.querySelector<HTMLElement>('.sc-err')
      expect(el).toBeTruthy()
      return el!
    })
    // The page's step card is the action bar and stat tiles (owner's pick,
    // 2026-11-05): `.sc-err`'s own `role="group" aria-label="Why it failed"`
    // names it now, in place of the `<h4>Why it failed</h4>` caption heading.
    expect(cause.getAttribute('role')).toBe('group')
    expect(cause.getAttribute('aria-label')).toBe('Why it failed')
    const text = cause.querySelector<HTMLElement>('.sc-err-text')!
    expect(text.textContent).toContain(ERROR)
    // The failure's ink is the body's own, not a dim caption's.
    expect(painted(text, 'color', WIDE)).toBe('var(--text)')
  })

  // The page's step card is the action bar and stat tiles (owner's pick,
  // 2026-11-05): the "same step in other workflows" row and its "no other
  // workflow of this shape" sentence are gone outright when there is nothing
  // to switch to (siblings.length <= 1) -- `.sc-others` and its pager are
  // drawn only when there is more than one, not restated as a message.
  it('drops the "other workflows" row from the Details fold entirely when this is the only one of its shape', async () => {
    const dock = await openOnPage()
    const details = await waitFor(() => {
      const el = dock.querySelector<HTMLElement>('.sc-facts')
      expect(el).toBeTruthy()
      return el!
    })
    expect(details.querySelector('.sc-others')).toBeNull()
    expect((details.textContent ?? '')).not.toMatch(/other workflow/i)
  })

  // The page's step card is the action bar and stat tiles (owner's pick,
  // 2026-11-05): "lease generation 3 · raised by every new lease" and "this
  // attempt's cost $0.253" are gone -- Generation is a plain dt/dd fact in
  // the Details fold (its note a title, not body text), and Cost is one of
  // the bar's own stat tiles, not words beside the head's workflow total.
  it('shows the generation as a Details fact and the cost as a stat tile', async () => {
    const dock = await openOnPage()
    const details = await waitFor(() => {
      const el = dock.querySelector<HTMLElement>('.sc-facts')
      expect(el).toBeTruthy()
      return el!
    })
    const genDt = [...details.querySelectorAll('dt')].find((d) => d.textContent === 'Generation')!
    const genDd = genDt.nextElementSibling as HTMLElement
    expect(genDd.textContent).toBe('3')
    expect(genDd.getAttribute('title')).toMatch(/fencing generation/)
    const cost = dock.querySelector<HTMLElement>('.sc-tile[data-tile="cost"] .sc-tile-v')!
    expect(cost.textContent).toBe('$0.253')
  })
})

// ---------------------------------------------------------------------------
// WG-7, L6: the Changes page says its scope and what was collected
// ---------------------------------------------------------------------------

function changes(step: string | null) {
  const f = fan()
  render(<WorkflowChangesTab workflow={f.w} taskById={f.tasks} at={{ step, file: null }} onAt={() => {}} />)
}

describe('WG-7: each Changes scope says what it covers', () => {
  it('says the tab is every step’s files and that an agent’s whole patch is its own Changes', async () => {
    changes(null)
    const note = await screen.findByRole('note', { name: 'What this page shows' })
    // MUTATION: drop the scope note from `ChangesMatrix`.
    expect(note.textContent).toMatch(/every step’s files, side by side/i)
    expect(note.textContent).toMatch(/Agent’s changes ›/)
    expect(note.textContent).toMatch(/whole patch/)
  })

  it('says a filtered page shows one step, and how to see them all', async () => {
    changes('checkpoints')
    const note = await screen.findByRole('note', { name: 'What this page shows' })
    // MUTATION: word the note the same whatever the filter.
    // 'the files of checkpoints', not 'checkpoints’s files', since QAFIX6
    // (run 6, F5): the possessive of a step id read awkwardly.
    expect(note.textContent).toMatch(/only the files of checkpoints/i)
    expect(note.textContent).toMatch(/All steps/)
  })

  it('names the step card’s link for the workflow’s Changes tab it opens', async () => {
    // The page's step card is the action bar and stat tiles (owner's pick,
    // 2026-11-05): the step's Changes link is now the bar's compact
    // "Changes" button, scoped by its `href` to this step's column, with the
    // sentence that used to be its visible text moved to its `title` (the
    // reason nothing was counted, when there is one, as here).
    const dock = await openOnPage()
    const link = await waitFor(() => within(dock).getByRole('link', { name: /^Changes/ }) as HTMLAnchorElement)
    expect(link.getAttribute('href')).toBe('/workflows/wf_fan/changes?step=checkpoints')
    expect(link.getAttribute('title')).toBe('This run wrote no git summary, so what it changed is not known.')
  })
})

describe('L6: the Changes page narrows to a selected step and says what was not collected', () => {
  it('draws only the selected step’s column', async () => {
    changes('checkpoints')
    const table = await screen.findByRole('table', { name: 'Files by step' })
    const heads = within(table).getAllByRole('columnheader').map((th) => th.getAttribute('data-step') ?? 'File')
    // MUTATION: draw every column and dim the rest (`is-dim`), as before --
    // selecting a step leaves six columns on screen.
    expect(heads).toEqual(['File', 'checkpoints'])
  })

  it('says how many steps’ file lists are known, beside the 0+', async () => {
    changes(null)
    const sum = await waitFor(() => {
      const p = document.querySelector<HTMLElement>('.chg-sum')
      expect(p).toBeTruthy()
      return p!
    })
    // '0+ files' is the console's kept notation (run-1 QA-118): '+' is a list
    // still incomplete. What it lacked is how incomplete.
    // MUTATION: drop the `known` clause from the summary.
    // With NO list known the summary says so in one sentence, and prints no
    // '0 files' total, since QA1-FIX-29 (overview-agents-76).
    expect((sum.textContent ?? '').replace(/\s+/g, ' ')).toContain('File lists not collected for any step')
  })

  it('says, by count, why no file is known: nothing collected, not nothing changed', async () => {
    changes(null)
    const state = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.chg-mx-card')
      // 'file list not recorded', not 'no file known yet', since QAFIX6 (run
      // 6, F5): every step here has ended, and 'yet' said the list was coming.
      expect(el?.textContent).toMatch(/file list not recorded/)
      return el!
    })
    const text = (state.textContent ?? '').replace(/\s+/g, ' ')
    // MUTATION: put back the fixed sentence listing four possible causes.
    expect(text).toContain('5 steps ended without a git summary, so nothing was collected')
    expect(text).toContain('1 step never started')
    // The column says the same in a word.
    // MUTATION: put back 'no summary' in `ColumnSay`.
    expect(within(state).getAllByText(/not collected/).length).toBeGreaterThan(0)
  })
})

// ---------------------------------------------------------------------------
// D2: a cost still being read says so
// ---------------------------------------------------------------------------

describe('D2: a workflow cost still being read reads "reading", never "not reported" or "—"', () => {
  it('heads the page with "reading" while the attempt read is in flight', async () => {
    api.loadWorkflowUsage.mockReturnValue(new Promise(() => {}))
    await page()
    // The cost is one of the head's `.wfp-fig`s now, not a dedicated `.wfp-cost`.
    const figs = [...document.querySelectorAll<HTMLElement>('.wfp-head .wfp-fig')]
    const cost = figs.find((el) => /reading|not reported|\$/.test(el.textContent ?? ''))!
    // MUTATION: drop the `reading` branch in `WorkflowHead` -- 'cost not
    // reported' while the read is in flight, '$1.21' once it lands.
    expect(cost.textContent).toMatch(/reading/)
    expect(cost.textContent).not.toMatch(/not reported/)
  })

  // Cost is the Spent column since the redesign (frame graph-workflows-rest).
  it('the list’s Spent cell waits on no attempt read: its steps’ totals come with the list (console load speed, 2026-11-01)', async () => {
    api.loadWorkflowUsage.mockReturnValue(new Promise(() => {}))
    render(<WorkflowsScreen view="" onView={() => {}} />)
    const cell = await waitFor(() => {
      const td = document.querySelector<HTMLElement>('.wfg-table tr[data-workflow="wf_fan"] td[data-col="spent"]')
      expect(td).toBeTruthy()
      return td!
    })
    // MUTATION: put the list's `useWorkflowUsage` back -- the cell reads
    // `reading` while the attempt read the list no longer makes is out.
    expect(cell.textContent).not.toBe('reading')
    expect(api.loadWorkflowUsage).not.toHaveBeenCalled()
  })

  it('draws reading upright and faint, apart from the italic absence', () => {
    const reading = build('span.wf-spend.is-reading', hosts)
    const absent = build('span.wf-spend.absent', hosts)
    // MUTATION: drop `.wf-spend.is-reading` -- reading takes `.wf-spend`'s
    // dim ink, one step from a figure; or style it as the absence.
    expect(painted(reading, 'color', WIDE)).toBe('var(--text-faint)')
    expect(painted(absent, 'font-style', WIDE)).toBe('italic')
    expect(painted(reading, 'font-style', WIDE)).toBe('normal')
  })
})
