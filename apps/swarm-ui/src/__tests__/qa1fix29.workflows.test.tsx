// QA1-FIX-29 (part of #2596, the console visual QA pass 1 epic, and #2391):
// the findings of the ovag sweep lane on the fixture console, 2026-10-28,
// whose file is WorkflowChanges.tsx or WorkflowViews.tsx. The workflow below
// is the fixture's fan-in (`wf_5e5ad3b6f7da4299a839` in api.ts): five steps,
// `checkpoints` failed after three attempts, `synthesis` cancelled before it
// started, and no step with a git summary.
//
//   76  the Changes summary said '0 files across 6 steps' with no list known.
//   77  synthesis, never started, offered 'Agent’s changes ›' and 'patch not measured'.
//   78  the step chips wrapped their labels at 390.
//   66  'lease generation 3 · raised by every new lease …' on the card face.
//   67  'no other workflow of this shape' between two disabled arrows.
//   68  attempt 2 kept attempt 3's 'Why it failed' with nothing saying whose.
//   70  the Cost column mixed '$0.22' and '$0.231'.
//   71  a never-started step's Waited read '0s', its Cost and Tokens 'none'.
//   72  Inputs read 'none' for every prompt-driven step.
//   74  'attempt 1 of 1' beside the table's '1 of 3' (used of allowed).
//   75  Escape did not close an opened step.
//   88  a 30s run on a days-long axis was a 1px tick; a parked step drew 'on parents'.
//
// Every assertion names the mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import type { CascadeEnv } from './cssgate'
import { fireEvent } from '@testing-library/react'
import { NEVER_STARTED_WORD } from '../dag'
import type { Cell } from '../measure'
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

const { WorkflowsScreen } = await import('../Workflows')
const { WorkflowChangesTab } = await import('../WorkflowChanges')
const { tableRows } = await import('../WorkflowViews')

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
    data: { attempts: [attempt(1, { exit_code: 2 }), attempt(2, { exit_code: 1, error: 'exit status 1: lint failed' }), attempt(3, { exit_code: 2, cost_usd: 0.253 })] },
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

async function page(tab: string | null = null): Promise<void> {
  render(<WorkflowsScreen view={tab === null ? 'wf=wf_fan' : `wf=wf_fan&tab=${tab}`} onView={() => {}} />)
  await waitFor(() => expect(document.querySelector('.wfp:not(.is-loading) .wfp-head')).toBeTruthy())
}

const panel = () => document.querySelector<HTMLElement>('.wf-panel')
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function changes(step: string | null) {
  const f = fan()
  render(<WorkflowChangesTab workflow={f.w} taskById={f.tasks} at={{ step, file: null }} onAt={() => {}} />)
}

async function card(): Promise<HTMLElement> {
  await page()
  return waitFor(() => {
    const el = panel()?.querySelector<HTMLElement>('.wf-inspect')
    expect(el?.querySelector('.wf-inspect-facts')).toBeTruthy()
    return el!
  })
}

describe('76: no file list known, no file total', () => {
  it('says no step’s list was collected instead of "0 files across 6 steps"', async () => {
    changes(null)
    const sum = await waitFor(() => {
      const p = document.querySelector<HTMLElement>('.chg-sum')
      expect(p).toBeTruthy()
      return p!
    })
    // MUTATION: drop the `known === 0 && all.length === 0` branch in
    // `ChangesMatrix` -- the summary reads '0 files across 6 steps' again.
    expect(flat(sum)).not.toMatch(/0 files/)
    expect(flat(sum)).toContain('File lists not collected for any step')
    // The count of known lists said the same thing a second time.
    expect(flat(sum)).not.toContain('known for 0 of')
  })
})

describe('77: a step that never started offers no changes', () => {
  it('draws synthesis as not started, with no Agent’s changes link and no patch fact', async () => {
    changes(null)
    const table = await screen.findByRole('table', { name: 'Files by step' })
    const head = table.querySelector<HTMLElement>('th[data-step="synthesis"]')!
    // MUTATION: drop the never-started branch in `columnFacts` -- the column
    // says 'patch not measured · file list not collected' again.
    expect(flat(head)).toContain('not started')
    expect(flat(head)).not.toMatch(/patch/)
    // MUTATION: drop `facts[i]!.kind !== 'not-started'` from the link's guard.
    expect(head.querySelector('a.chg-mx-agent')).toBeNull()
    // A step that ran keeps its link.
    expect(table.querySelector('th[data-step="checkpoints"] a.chg-mx-agent')).toBeTruthy()
  })

  it('keeps its step name and "not started" whole, though the column has little else in it', async () => {
    changes(null)
    const table = await screen.findByRole('table', { name: 'Files by step' })
    const head = table.querySelector<HTMLElement>('th[data-step="synthesis"]')!
    // MUTATION: put back `overflow-wrap: anywhere` on `.chg-mx-step > .mono`
    // -- the column shrinks below its name and reads 'synthe / sis'.
    expect(painted(head.querySelector('.mono')!, 'overflow-wrap', WIDE)).toBe('break-word')
    // MUTATION: drop `.chg-mx-say > .ctl-sub { white-space: nowrap }` --
    // 'not / started'.
    expect(painted(head.querySelector('.chg-mx-say > .ctl-sub')!, 'white-space', WIDE)).toBe('nowrap')
  })
})

describe('78: the step chips keep their labels on one line', () => {
  it('sets nowrap on each chip, at 390 as at 1440', () => {
    const chip = build('div.chg-mx-chips > button.chg-mx-chip.mono', hosts)
    // MUTATION: drop `white-space: nowrap` from `.chg-mx-chip` -- 'cold- /
    // start' wraps inside its chip in the phone's scrolling strip.
    expect(painted(chip, 'white-space', PHONE)).toBe('nowrap')
    expect(painted(chip, 'white-space', WIDE)).toBe('nowrap')
  })
})

describe('66: the lease generation folds under More about this attempt', () => {
  it('keeps the card face to the attempt’s figures, the generation one press away', async () => {
    const el = await card()
    const face = el.querySelector<HTMLElement>('.wf-inspect-facts')!
    // MUTATION: empty `FOLDED_FACTS` -- 'lease generation 3 · raised by every
    // new lease …' is on the card's face again.
    expect(flat(face)).not.toContain('lease generation')
    const fold = el.querySelector<HTMLDetailsElement>('details.wf-inspect-more')
    expect(fold).toBeTruthy()
    expect(flat(fold!.querySelector('summary'))).toBe('More about this attempt')
    expect(flat(fold)).toMatch(/lease generation ?3/)
    expect(fold!.open).toBe(false)
  })
})

describe('67: nothing to page, no pager', () => {
  it('says there is no other workflow of this shape in one sentence, with no arrows and no strip', async () => {
    const el = await card()
    const row = el.querySelector<HTMLElement>('[data-scrub="workflow"]')!
    // MUTATION: draw the pager whatever `siblings.length` is -- two disabled
    // arrows and a lone strip mark around the sentence.
    expect(row.querySelectorAll('button')).toHaveLength(0)
    expect(row.querySelector('.wf-scrub-strip')).toBeNull()
    expect(flat(row)).toContain('No other workflow of this shape on this board')
  })
})

describe('68 and 74: the attempt stepper', () => {
  it('says "of N made", so it is not read as the table’s used-of-allowed', async () => {
    const el = await card()
    const pos = await waitFor(() => {
      const p = el.querySelector<HTMLElement>('[data-scrub="attempt"] .wf-scrub-pos')
      expect(p).toBeTruthy()
      return p!
    })
    // MUTATION: print 'attempt {k} of {n}' again.
    expect(flat(pos)).toBe('attempt 3 of 3 made')
  })

  it('says Why it failed is the step’s final reason when an older attempt is picked', async () => {
    const el = await card()
    const scrub = el.querySelector<HTMLElement>('[data-scrub="attempt"]')!
    expect(el.querySelector('.wf-scrub-note'), 'the newest attempt is the reason’s own').toBeNull()
    fireEvent.click(within(scrub).getByRole('button', { name: 'Previous attempt' }))
    await waitFor(() => expect(flat(scrub.querySelector('.wf-scrub-pos'))).toBe('attempt 2 of 3 made'))
    // MUTATION: drop the `!newest` note -- attempt 2 shows exit code 1 under
    // a Why it failed that is attempt 3's exit status 2, with nothing saying so.
    const note = el.querySelector<HTMLElement>('.wf-scrub-note')!
    expect(flat(note)).toContain('Why it failed is the step’s final reason, from attempt 3')
    // ITS OWN LINE, NOT A PIECE OF THE ATTEMPT ROW (review of QA1-FIX-29).
    // MUTATION: render the note inside `[data-scrub="attempt"]` again -- it
    // takes the row's one line, clipped at 390, and reads as the fold's label.
    expect(scrub.contains(note)).toBe(false)
    expect(note.parentElement?.classList.contains('wf-inspect')).toBe(true)
    // MUTATION: drop `white-space: normal` from `.wf-inspect > .wf-scrub-note`.
    expect(painted(note, 'white-space', PHONE)).toBe('normal')
    // Its own error is listed, not swallowed as a repeat of the final reason.
    expect(flat(scrub)).toContain('lint failed')
  })
})

describe('70 and 71: the table prints one precision and a dash where a figure does not apply', () => {
  const times = { spans: [], mark: null, attempts: null, waitedMs: 0, waitedOpen: false, parentsMs: null, ranMs: null, sentence: '' }
  const measured = (text: string): Cell => ({ kind: 'measured', text, note: '' })
  const ran = (text: string): Cell => ({ kind: text === NEVER_STARTED_WORD ? 'absent' : 'measured', text, note: '' })
  const rows = [
    { ran: ran('1m'), cost: measured('$0.22'), tokens: measured('1.2k'), pending: false, times },
    { ran: ran('1m'), cost: measured('$0.231'), tokens: measured('1.2k'), pending: false, times },
    { ran: ran(NEVER_STARTED_WORD), cost: measured('none'), tokens: measured('none'), pending: false, times },
  ]

  it('pads every dollar figure in the Cost column to its most decimals', () => {
    // MUTATION: return `r.cost` unpadded in `tableRows` -- '$0.22' beside '$0.231'.
    expect(tableRows(rows).map((r) => r.cost.text).slice(0, 2)).toEqual(['$0.220', '$0.231'])
  })

  it('prints a dash, with its reason, for a never-started step’s Waited, Cost and Tokens', () => {
    const r = tableRows(rows)[2]!
    // MUTATION: drop the `neverStarted` branch -- '0s', 'none', 'none'.
    expect([r.waited.text, r.cost.text, r.tokens.text]).toEqual(['—', '—', '—'])
    expect(r.waited.note).toMatch(/without ever starting/)
    expect(tableRows(rows)[0]!.waited.text).toBe('0s')
  })

  it('draws the dash in the table’s synthesis row', async () => {
    await page('table')
    const row = await waitFor(() => {
      const tr = document.querySelector<HTMLElement>('.wf-table tr[data-step="synthesis"]')
      expect(tr).toBeTruthy()
      return tr!
    })
    // MUTATION: draw `waitedCell(r.times)` in the table again.
    expect(flat(row.querySelector('td[data-col="waited"]'))).toBe('—')
    expect(flat(row.querySelector('td[data-col="cost"]'))).toBe('—')
  })
})

describe('72: the prompt is an input', () => {
  it('says "prompt" for a step that reads no file from another step', async () => {
    await page('table')
    const cell = await waitFor(() => {
      const td = document.querySelector<HTMLElement>('.wf-table tr[data-step="cold-start"] td[data-col="inputs"]')
      expect(td).toBeTruthy()
      return td!
    })
    // MUTATION: print 'none' again in `InputsSummary`.
    expect(flat(cell)).toBe('prompt')
  })
})

describe('75: Escape closes an opened step', () => {
  it('closes the step opened from the table, focus staying on its name', async () => {
    await page('table')
    const name = await waitFor(() => {
      const b = document.querySelector<HTMLButtonElement>('.wf-table button.wf-pick[data-step="cold-start"]')
      expect(b).toBeTruthy()
      return b!
    })
    fireEvent.click(name)
    name.focus()
    await waitFor(() => expect(document.querySelector('.wf-inspect[aria-label^="Step cold-start"]')).toBeTruthy())
    // MUTATION: drop `PickButton`'s Escape handler -- the opened step stays open.
    fireEvent.keyDown(name, { key: 'Escape' })
    await waitFor(() => expect(document.querySelector('.wf-inspect[aria-label^="Step cold-start"]')).toBeNull())
    expect(document.activeElement).toBe(name)
  })
})

describe('88: the timeline draws a short run readably and a parked step as parked', () => {
  const DAY = 86_400
  function long(): { w: Workflow; tasks: Map<string, Task> } {
    const tasks = new Map<string, Task>([
      ['t_plan', task('t_plan', 'SUCCEEDED', { workflow_id: 'wf_long', step_id: 'plan', attempt_count: 1, current_generation: 1, created_at: iso(-8 * DAY), started_at: iso(-8 * DAY + 10), completed_at: iso(-8 * DAY + 40) })],
      ['t_build', task('t_build', 'RUNNING', { workflow_id: 'wf_long', step_id: 'build', attempt_count: 1, current_generation: 1, created_at: iso(-8 * DAY), started_at: iso(-8 * DAY + 60), completed_at: null, depends_on: ['plan'] })],
      ['t_report', task('t_report', 'PARKED', { workflow_id: 'wf_long', step_id: 'report', attempt_count: 0, current_generation: 0, created_at: iso(-8 * DAY), started_at: null, completed_at: null, depends_on: ['build'], park_reason: 'DEPENDENCY_INCOMPLETE' })],
    ])
    const w: Workflow = {
      workflow_id: 'wf_long', state: 'RUNNING', tenant_id: 'eng', stored_state: 'RUNNING', state_source: 'derived',
      rollup: { state: 'RUNNING', complete: true, reason: 'running', counts: { SUCCEEDED: 1, RUNNING: 1, PARKED: 1 }, unreadable_steps: [], unstarted_steps: [], steps_read: 3 },
      created_at: iso(-8 * DAY), updated_at: iso(-60), submitted_by: 'qa@example.com', priority: 0, on_step_failure: 'FAIL_WORKFLOW', cancel_requested: false,
      steps: [step('plan', [], { task_id: 't_plan' }), step('build', ['plan'], { task_id: 't_build' }), step('report', ['build'], { task_id: 't_report' })],
    }
    return { w, tasks }
  }

  async function timeline(): Promise<HTMLElement> {
    const f = long()
    api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: { workflows: [f.w], taskById: f.tasks, statesDetail: null }, fetchedAt: T0 } satisfies Result<WorkflowBoard>)
    render(<WorkflowsScreen view="wf=wf_long&tab=timeline" onView={() => {}} />)
    return waitFor(() => {
      const el = document.querySelector<HTMLElement>('.wf-timeline')
      expect(el?.querySelector('.wf-tl-track[data-step="report"] .wf-tl-span')).toBeTruthy()
      return el!
    })
  }

  it('keeps a 30s run on an eight-day axis a visible bar, with its length beside it', async () => {
    const el = await timeline()
    const track = el.querySelector<HTMLElement>('.wf-tl-track[data-step="plan"]')!
    const bar = track.querySelector<HTMLElement>('.wf-tl-span.is-ran')!
    // MUTATION: drop `minWidth` from the span's style -- a 0.004% sliver.
    expect(bar.style.minWidth).toBe('4px')
    // MUTATION: drop the `wf-tl-short` label -- no figure anywhere on the row.
    const label = track.querySelector<HTMLElement>('.wf-tl-short')!
    expect(flat(label)).toBe('30s')
    // THE LABEL DOES NOT COVER THE BAR (review of QA1-FIX-29): it starts its
    // width plus the 4px floor from the bar's start, past the drawn edge.
    expect(label.style.left).toMatch(/% \+ 4px\)$/)
    // MUTATION: drop `.wf-tl-cut.wf-tl-short` -- the cut label's surface
    // backdrop and -0.5ch pull paint over the 4px bar in both themes.
    expect(painted(label, 'transform', WIDE)).toBe('translateY(-50%)')
    expect(painted(label, 'background', WIDE)).toBe('none')
    // A long run needs neither.
    expect(el.querySelector('.wf-tl-track[data-step="build"] .wf-tl-short')).toBeNull()
  })

  it('draws a step parked on its parents in the key’s parked hatch, not the dotted on-parents outline', async () => {
    const el = await timeline()
    const span = el.querySelector<HTMLElement>('.wf-tl-track[data-step="report"] .wf-tl-span.is-open')!
    // MUTATION: keep `is-${s.kind}` and the `s.kind === 'waiting'` test for
    // `is-parked` -- the span is `is-parents`, the dotted outline.
    expect(span.classList.contains('is-parked')).toBe(true)
    expect(span.classList.contains('is-parents')).toBe(false)
    // The hatch the key's swatch is drawn with is the one this span paints.
    expect(painted(span, 'background', WIDE)).toContain('repeating-linear-gradient')
  })
})
