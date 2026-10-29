/**
 * LANE QAFIX-WORKFLOW-HEAD (part of #1680, visual QA 2026-10-20): one
 * workflow's page, its head and its failed step's card.
 *
 *   QA-119  the head says what each figure counts: `6 steps in 2 stages
 *           (5 → 1) · 4 of 6 steps done`, where it read `6 steps · 5 → 1 ·
 *           4/6 done`. The list's narrow cells keep the short forms.
 *   QA-120  a failed workflow's page opens with the failed step selected (its
 *           card is not scrolled to, QA G3-28); the FIRST click on that node
 *           opens its card and brings it into view instead of closing it, and
 *           a second click closes it. The step's whole error is on the card.
 *
 *           Run 2 (wf_b6e2441b419f423a84eb) still saw the card below the fold
 *           on arrival, with nothing on screen saying why the step failed
 *           but the node's cut line (QAFIX2-WORKFLOWS). The page now leads
 *           its graph with the failed step's whole reason and a control that
 *           brings its card (Open agent, Logs, Changes) to the reader.
 *
 * Screenshot states for the next visual QA run: this fixture -- a FAILED
 * workflow, 5 → 1, its failed step picked on arrival -- on its Graph at 1440px
 * and at 390px, before and after the first click on the failed node.
 *
 * Each assertion that holds a change carries a MUTATION comment naming the
 * change that turns it red.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { ownFromBoard } from './workflowsById'

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

const T0 = Date.parse('2026-10-20T12:00:00.000Z')
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
    attempt_count: 1,
    max_attempts: 1,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_fail',
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

/** The whole error, longer than any node holds on one line, and on two lines. */
const ERROR = [
  'exit status 1: pytest collected 2 failures in tests/unit/scheduler/test_admission.py and tests/unit/scheduler/test_dispatch.py',
  'FAILED tests/unit/scheduler/test_admission.py::test_all_or_nothing - AssertionError: reserved 2 of 3 pools',
].join('\n')

/** Five independent steps, then a join: 4 succeeded, `test` failed, `join` never started. */
function failed(): { w: Workflow; tasks: Map<string, Task> } {
  const fan = ['lint', 'build', 'test', 'docs', 'scan']
  const steps = [...fan.map((id) => step(id, [], { task_id: `t_${id}` })), step('join', fan)]
  const tasks = new Map<string, Task>(
    fan.map((id) => [
      `t_${id}`,
      task(`t_${id}`, id === 'test' ? 'FAILED' : 'SUCCEEDED', { step_id: id, last_error: id === 'test' ? ERROR : null }),
    ]),
  )
  const w: Workflow = {
    workflow_id: 'wf_fail',
    state: 'FAILED',
    tenant_id: 'eng',
    stored_state: 'FAILED',
    state_source: 'derived',
    rollup: {
      state: 'FAILED',
      complete: true,
      reason: 'test',
      counts: { SUCCEEDED: 4, FAILED: 1, unstarted: 1 },
      unreadable_steps: [],
      unstarted_steps: ['join'],
      steps_read: 6,
    },
    created_at: iso(-600),
    updated_at: iso(-60),
    submitted_by: 'qa@example.com',
    priority: 0,
    on_step_failure: 'CONTINUE',
    cancel_requested: false,
    steps,
  }
  return { w, tasks }
}

let scroll: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  const f = failed()
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows: [f.w], taskById: f.tasks, statesDetail: null },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
  scroll = vi.fn()
  Object.defineProperty(Element.prototype, 'scrollIntoView', { value: scroll, configurable: true, writable: true })
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

async function page(): Promise<void> {
  render(<WorkflowsScreen view="wf=wf_fail" onView={() => {}} />)
  await waitFor(() => expect(document.querySelector('.wfp:not(.is-loading) .wfp-head')).toBeTruthy())
}

const headNote = () => (document.querySelector<HTMLElement>('.c-count-note')?.textContent ?? '').replace(/\s+/g, ' ')

// ---------------------------------------------------------------------------
// QA-119: the head says what each figure counts
// ---------------------------------------------------------------------------

describe('QA-119: the workflow page head names its stages and its steps', () => {
  it('reads `6 steps in 2 stages (5 → 1) · 4 of 6 steps done`, with no bare `5 → 1` or `4/6`', async () => {
    await page()
    await waitFor(() => expect(headNote()).toContain('4 of 6 steps done'))
    const note = headNote()
    // MUTATION: drop ` in ${shape.levels} stages (${shape.text})` from
    // `pageSummary` -- the shape goes back to a bare `· 5 → 1 ·`.
    expect(note).toContain('6 steps in 2 stages (5 → 1)')
    expect(note, 'the shape is printed bare, with nothing saying it counts stages').not.toContain('· 5 → 1 ·')
    // MUTATION: drop the `.replace(/^(\d+)\/(\d+) done/, ...)` in
    // `pageSummary` -- the census goes back to `4/6 done`.
    expect(note, 'the census is the bare fraction').not.toMatch(/\b4\/6 done/)
    // The rest of the census still follows it, unchanged.
    expect(note).toMatch(/4 of 6 steps done · 1 failed/)
  })

  it('keeps the short forms in the list’s narrow cells', async () => {
    render(<WorkflowsScreen view="" onView={() => {}} />)
    const row = await waitFor(() => {
      const r = document.querySelector<HTMLElement>('.wfl-table tbody tr[data-workflow="wf_fail"]')
      expect(r).toBeTruthy()
      return r!
    })
    // MUTATION: apply the head's long forms in `rollupLine` or `Shape` rather
    // than in `pageSummary` -- the list's cells grow `in 2 stages` and `of 6 steps`.
    await waitFor(() => expect(row.querySelector('.wfl-done small')?.textContent).toBe('4/6'))
    expect(row.querySelector('.wf-shape-text')?.textContent).toBe('5 → 1')
    expect(row.textContent).not.toContain('stages')
  })
})

// ---------------------------------------------------------------------------
// QA-120: a failed step's card opens on the first click
// ---------------------------------------------------------------------------

const panel = () => document.querySelector<HTMLElement>('.wf-panel')
const node = (id: string) => document.querySelector<HTMLButtonElement>(`button.node[data-step="${id}"]`)

describe('QA-120: the failed step’s card opens on the first click', () => {
  it('selects the failed step on arrival, and the first click on it opens and scrolls to its card; a second click closes it', async () => {
    await page()
    // Arrival: the failed step is the one selected, its card drawn, and the
    // page is not scrolled down to it (QA G3-28 holds).
    await waitFor(() => expect(panel()?.querySelector('.wf-inspect-id')?.textContent).toBe('test'))
    expect(scroll, 'the arrival pick scrolled the page down to the card').not.toHaveBeenCalled()
    // ITS STAGE STAYS FOLDED ON ARRIVAL (visual QA pass 2, overview-agents-63,
    // #2694): the step is the band's pressed chip, not a node on the canvas.
    const chip = await waitFor(() => {
      const c = [...document.querySelectorAll<HTMLButtonElement>('.wf-band-chips button')].find((b) => b.textContent?.includes('test'))
      expect(c?.getAttribute('aria-pressed')).toBe('true')
      return c!
    })
    expect(node('test')).toBeNull()

    // The first click: the card stays, and is brought to the reader.
    act(() => {
      fireEvent.click(chip)
    })
    // MUTATION: make `pickStep` in `WorkflowCard` toggle unconditionally, as
    // it did -- the first click closes the arrival card and nothing scrolls.
    await waitFor(() => expect(scroll).toHaveBeenCalled())
    expect(panel(), 'the first click on the arrival pick closed its card').not.toBeNull()
    expect(panel()!.querySelector('.wf-inspect-id')?.textContent).toBe('test')
    expect(panel()!.contains(document.activeElement), 'focus did not move into the card the click opened').toBe(true)
    // The reader's pick opens its stage.
    await waitFor(() => expect(node('test')).toBeTruthy())

    // The second click: closes it.
    act(() => {
      fireEvent.click(node('test')!)
    })
    // MUTATION: make the open-first branch in `pickStep` fire on every click
    // of the selected node -- the card can never be closed from its node.
    await waitFor(() => expect(panel(), 'the second click did not close the card').toBeNull())
  })

  it('closes the arrival card at once from its own ×, which is not a node click', async () => {
    await page()
    await waitFor(() => expect(panel()).not.toBeNull())
    // MUTATION: route `closePanel` through the open-first branch -- the ×
    // would scroll to the card instead of closing it.
    act(() => {
      fireEvent.click(panel()!.querySelector<HTMLButtonElement>('.wf-inspect-close')!)
    })
    await waitFor(() => expect(panel()).toBeNull())
  })

  it('prints the failed step’s whole error on the card, and keeps it whole in the node’s title', async () => {
    await page()
    const cause = await waitFor(() => {
      const el = panel()?.querySelector<HTMLElement>('.wf-inspect-cause')
      // MUTATION: drop the `.wf-inspect-cause` block from `InspectorSlot` --
      // the card says nothing of why the step failed until its attempts land.
      expect(el, 'the card does not state why the step failed').toBeTruthy()
      return el!
    })
    // In full: every line, nothing cut.
    expect(cause.querySelector('.wf-inspect-cause-text')?.textContent).toBe(ERROR)
    expect(cause.textContent).not.toContain('…')
    // The node's one line keeps the whole text in its title -- on the canvas
    // once a reader opens its stage (overview-agents-63).
    act(() => {
      fireEvent.click(document.querySelector<HTMLButtonElement>('.wfp-fail-lead-go')!)
    })
    await waitFor(() => expect(node('test')).toBeTruthy())
    expect(node('test')!.parentElement!.querySelector<HTMLElement>('.node-note')?.getAttribute('title')).toBe(ERROR)
  })

  it('wraps the card’s error rather than cutting it', async () => {
    const CSS = (await import('../styles/workflows.css?raw')).default
    // MUTATION: drop the `.wf-inspect-cause-text` rule from workflows.css --
    // the error is one unbroken line running out of the card.
    const rule = CSS.match(/\.wf-inspect-cause-text\s*\{([^}]*)\}/)
    expect(rule, 'no rule lays out the card’s error').not.toBeNull()
    expect(rule![1]).toMatch(/white-space:\s*pre-wrap/)
    expect(rule![1]).toMatch(/overflow-wrap:\s*anywhere/)
    expect(rule![1]).not.toMatch(/text-overflow|nowrap/)
  })
})

// ---------------------------------------------------------------------------
// QA-120 (run 2): why the step failed is on screen on arrival
// ---------------------------------------------------------------------------

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const lead = () => document.querySelector<HTMLElement>('.wfp-fail-lead')

describe('QA-120 (run 2): the failed step’s reason leads the graph, above the fold', () => {
  it('names the failed step and prints its whole reason above the graph on arrival', async () => {
    await page()
    // MUTATION: drop the `.wfp-fail-lead` block from `WorkflowCard` and the
    // only reason on screen at 1440x900 is the node's cut line.
    const region = await waitFor(() => screen.getByRole('region', { name: 'Why step test failed' }))
    // MUTATION: print `why.text` (the first line, as the node does) and this is cut.
    expect(region.querySelector('.wfp-fail-lead-text')?.textContent).toBe(ERROR)
    // MUTATION: render the lead after the split and it is under the graph,
    // below the fold again, as the card was.
    const split = document.querySelector('.wf-split')!
    expect(region.compareDocumentPosition(split) & Node.DOCUMENT_POSITION_FOLLOWING, 'the lead is not above the graph').toBeTruthy()
    // The card itself is still not scrolled to on arrival (QA G3-28 holds).
    expect(scroll).not.toHaveBeenCalled()
  })

  it('brings the step’s card to the reader from the lead, and the lead goes once it has', async () => {
    await page()
    const region = await waitFor(() => screen.getByRole('region', { name: 'Why step test failed' }))
    // MUTATION: drop the lead's button and nothing on screen points to the card below.
    act(() => {
      // 'Open the step’s card' since QAFIX5: from 1280 the card is beside the graph, not below it.
      fireEvent.click(within(region).getByRole('button', { name: /step’s card/ }))
    })
    // MUTATION: make the button toggle the pick and it CLOSES the card it points to.
    expect(panel(), 'the lead’s button closed the card').not.toBeNull()
    expect(scroll).toHaveBeenCalled()
    // MUTATION: leave focus on the button and a keyboard reader is not taken to the card.
    expect(panel()!.contains(document.activeElement), 'focus did not move into the card').toBe(true)
    // MUTATION: keep the lead after the reader has picked and the reason is printed twice, once over the card.
    await waitFor(() => expect(lead()).toBeNull())
  })

  it('wraps the lead’s reason at every width and inks it as a failure', async () => {
    await page()
    const text = await waitFor(() => {
      const el = lead()?.querySelector<HTMLElement>('.wfp-fail-lead-text')
      expect(el).toBeTruthy()
      return el!
    })
    for (const env of [WIDE, PHONE]) {
      // MUTATION: drop `white-space: pre-wrap` and the error's two lines run together.
      expect(painted(text, 'white-space', env)).toBe('pre-wrap')
      // MUTATION: drop `overflow-wrap: anywhere` and a long test path runs out of the column at 390.
      expect(painted(text, 'overflow-wrap', env)).toBe('anywhere')
    }
    // MUTATION: draw the lead with the neutral border and it does not read as the failure it names.
    expect(painted(lead()!, ['border-left', 'border-left-color'], WIDE)).toMatch(/var\(--bad\)/)
  })

  it('draws no lead once the reader picks another step, nor on a workflow with no failed pick', async () => {
    await page()
    await waitFor(() => expect(lead()).not.toBeNull())
    // The stage opens to its toggle, as a reader opens it: the page's own
    // pick leaves it folded (overview-agents-63).
    act(() => {
      fireEvent.click(document.querySelector<HTMLButtonElement>('.wf-band-toggle')!)
    })
    await waitFor(() => expect(node('lint')).toBeTruthy())
    act(() => {
      fireEvent.click(node('lint')!)
    })
    // MUTATION: key the lead on `picked` failing alone and it stays over a succeeded step's card.
    await waitFor(() => expect(lead()).toBeNull())
    expect(panel()!.querySelector('.wf-inspect-id')?.textContent).toBe('lint')
  })
})
