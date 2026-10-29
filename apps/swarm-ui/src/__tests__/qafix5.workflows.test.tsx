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
//   r3:work-a-010 (6314117148) at 1440 the Cost column wrapped '3 of 4' /
//          'reported' although c28 sized it for one line: 128px left the
//          coverage exactly its own width (15 x 0.6em x 12px = 108px in a
//          108px box), no slack for a rounded glyph advance.
//
// Every assertion names the mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'
import { attempt } from './runfixture'
import { cellStyle, lengthPx, textPx } from './tablefit'
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

const { WorkflowsScreen } = await import('../Workflows')
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
  for (const h of hosts.splice(0)) h.remove()
})

async function page(): Promise<void> {
  render(<WorkflowsScreen view="wf=wf_fan" onView={() => {}} />)
  await waitFor(() => expect(document.querySelector('.wfp:not(.is-loading) .wfp-head')).toBeTruthy())
}

const panel = () => document.querySelector<HTMLElement>('.wf-panel')
const WIDE: CascadeEnv = { width: 1440 }

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
    await page()
    await waitFor(() => expect(panel()?.querySelector('.wf-inspect-id')?.textContent).toBe('checkpoints'))
    const head = panel()!.querySelector<HTMLElement>('.wf-inspect-head')!
    // MUTATION: drop the `stepHeading` branch in `StepInspector` -- the head
    // leads with the workflow's id again and has no heading at all.
    // Named by the step, then its state: the mark before it is decoration.
    expect(within(head).getByRole('heading', { level: 3, name: 'checkpoints failed' })).toBeTruthy()
    expect(head.textContent, 'the card repeats the workflow the page is titled by').not.toContain('wf_fan')
  })

  it('says the arrival control opens the card, not that it is "below"', async () => {
    await page()
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
  it('says "why it failed" as a heading in the body ink, not a grey caption', async () => {
    await page()
    const cause = await waitFor(() => {
      const el = panel()?.querySelector<HTMLElement>('.wf-inspect-cause')
      expect(el).toBeTruthy()
      return el!
    })
    // MUTATION: put the `<b>why it failed</b>` caption back -- no heading.
    const h = within(cause).getByRole('heading', { level: 4, name: 'Why it failed' })
    // MUTATION: give `.wf-inspect-cause-h` `color: var(--text-dim)` (the
    // caption's) -- the label reads grey again.
    expect(painted(h, 'color', WIDE)).toBe('var(--text)')
    // The failure's ink is the block's left rule, as the lead above the graph.
    // MUTATION: drop the cause's `border-left`.
    expect(painted(cause, 'border-left', WIDE)).toBe('2px solid var(--bad)')
  })

  it('says the same-step row compares this step across workflows of this shape, and says when there is no other', async () => {
    await page()
    const row = await waitFor(() => {
      const el = panel()?.querySelector<HTMLElement>('[data-scrub="workflow"]')
      expect(el).toBeTruthy()
      return el!
    })
    const text = (row.textContent ?? '').replace(/\s+/g, ' ')
    // MUTATION: print the bare `same step` key again.
    expect(text).toContain('same step in other workflows')
    // MUTATION: print `workflow 1 of 1` when the workflow is the only one of
    // its shape -- a position in a list of one, unexplained.
    expect(text).not.toContain('workflow 1 of 1')
    // One sentence since QA1-FIX-29 (overview-agents-67).
    expect(text).toContain('No other workflow of this shape on this board')
  })

  it('names the generation as the lease’s and says what it is for, and the cost as this attempt’s', async () => {
    await page()
    const facts = await waitFor(() => {
      const el = panel()?.querySelector<HTMLElement>('.wf-inspect-facts')
      expect(el).toBeTruthy()
      return el!
    })
    const text = (facts.textContent ?? '').replace(/\s+/g, ' ')
    // MUTATION: label `gen` 'generation' again in `FACT_LABEL`.
    // Folded under More about this attempt since QA1-FIX-29 (overview-agents-66).
    const more = (facts.parentElement!.querySelector('details.wf-inspect-more')?.textContent ?? '').replace(/\s+/g, ' ')
    expect(more).toMatch(/lease generation ?3/)
    // MUTATION: drop the `FACT_HINT` line -- '3' with nothing saying what it counts.
    expect(more).toContain('raised by every new lease')
    // MUTATION: label `cost` 'cost' -- '$0.253' read beside the head's
    // workflow total as a second figure for the same thing (D2).
    expect(text).toMatch(/this attempt’s cost ?\$0\.253/)
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
    await page()
    const link = await waitFor(() => {
      const a = panel()?.querySelector<HTMLAnchorElement>('a.wf-inspect-changes')
      expect(a).toBeTruthy()
      return a!
    })
    // MUTATION: drop "in this workflow" -- the link reads like the agent's own Changes.
    expect(link.textContent).toMatch(/^This step’s changes in this workflow/)
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
    const cost = document.querySelector<HTMLElement>('.wfp-head .wfp-cost')!
    // MUTATION: drop the `reading` branch in `WorkflowHead` -- 'cost not
    // reported' while the read is in flight, '$1.21' once it lands.
    expect(cost.textContent).toMatch(/cost: reading/)
    expect(cost.textContent).not.toMatch(/not reported/)
  })

  it('draws "reading" in the list’s Cost cell while the read is in flight, not the "—" of an absence', async () => {
    api.loadWorkflowUsage.mockReturnValue(new Promise(() => {}))
    render(<WorkflowsScreen view="" onView={() => {}} />)
    const cell = await waitFor(() => {
      const td = document.querySelector<HTMLElement>('.wfl-table tr[data-workflow="wf_fan"] td[data-col="cost"]')
      expect(td).toBeTruthy()
      return td!
    })
    // MUTATION: drop the `reading` prop from the row's `Spend`.
    expect(cell.textContent).toBe('reading')
    expect(within(cell).getByRole('img', { name: /still being read/ })).toBeTruthy()
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

// ---------------------------------------------------------------------------
// work-a-010: c28 with slack
// ---------------------------------------------------------------------------

describe('work-a-010: at 1440 the Cost column holds "3 of 4 reported" with a glyph to spare', () => {
  it('leaves at least one character’s advance beyond the coverage', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="wfl"><div class="table-wrap is-scroll wfl-scroll"><table class="wfl-table"><thead><tr><th data-col="cost">Cost</th></tr></thead>' +
      '<tbody><tr class="wfl-row"><td class="num" data-col="cost"><span class="wf-spend">$0.518<span class="wf-spend-cov">3 of 4 reported</span></span></td></tr></tbody></table></div></div>'
    document.body.appendChild(host)
    hosts.push(host)
    const th = host.querySelector('th')!
    const td = host.querySelector('td')!
    const st = cellStyle(td, WIDE)
    const room = (lengthPx(painted(th, 'width', WIDE), 0) ?? 0) - st.pl - st.pr
    // MEASURED IN THE FACE IT IS DRAWN IN. It was `.wf-spend`'s DM Mono
    // (0.6em), 108px; since visual QA pass 2 ovag2-15 (#2694) the coverage is
    // a note in the body face (`.wf-spend-cov { font-family: var(--font) }`),
    // 100.8px, and the 136px column holds it with more to spare.
    const cov = host.querySelector('.wf-spend-cov')!
    const text = textPx('3 of 4 reported', cov, WIDE)
    expect(text).toBeCloseTo(100.8, 5)
    // MUTATION: put the 1280 head back at 128px -- the room is exactly the
    // text's 108px, and a browser that rounds one advance up wraps 'reported'.
    expect(room - text).toBeGreaterThanOrEqual(text / 15)
  })
})
