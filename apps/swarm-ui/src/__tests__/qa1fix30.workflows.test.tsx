/**
 * LANE QA1-FIX-30 (part of #2596, the console's visual QA pass 1, and #2391):
 * the findings whose main file is Workflows.tsx, each pinned on the FIXTURE
 * console's own data (`api.ts` fixtures, which dev mode serves), so the screen
 * a reviewer renders is the screen these assertions read.
 *
 *   61  the list's titles were cut by a raw id beside them
 *   62  `state not derived · this API did not derive the state from the steps`
 *   63  the graph scrolled to centre the picked step and cut its neighbours
 *   64  the synthesis node listed every input file, ~260px of card
 *   73  the Graph tab drew the whole Steps table under the graph
 *   80  the loading head had no Copy id, so the loaded head shifted
 *   81  `stored as queued · steps running · stored copy corrected`
 *   82  `$0.518 3 of 4 reported`: no unit, no space
 *   83  stop / Stop step / Cancel workflow / Stop: one action, three words
 *   84  a recorded stop changed nothing on the page
 *   85  the stop dialog's hint asked for a name where the id is typed (QA1C-FOLLOWUPS)
 *   89  two screens named Timeline
 *   C-10 Submitted and started as local clock times with no zone
 *   C-11 a one-step chip cut to `claude-code-browser …`
 *
 * Every assertion that holds a change carries a MUTATION comment naming the
 * change that turns it red.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'

import type { CancelWorkflowResult, WorkflowRead } from '../api'
import type { Result } from '../fetch'

const api = vi.hoisted(() => ({
  loadWorkflow: vi.fn(),
  cancelWorkflow: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  api.loadWorkflow.mockImplementation(real.loadWorkflow)
  return { ...real, ...api }
})

const real = await vi.importActual<typeof import('../api')>('../api')
const { WorkflowsScreen, WorkflowCard, stageKey, utcStamp } = await import('../Workflows')
const { levelsOf } = await import('../dag')

const AUDIT = 'wf_audit_01'
const AUDIT_TITLE = 'Audit the operator scripts and the Terraform'
const SYNTH = 'wf_5e5ad3b6f7da4299a839'
const STORED = 'wf_bcdc9180e4fb4a209f31'

const restore: (() => void)[] = []

beforeEach(() => {
  api.loadWorkflow.mockImplementation(real.loadWorkflow)
  api.cancelWorkflow.mockReset()
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

afterEach(() => {
  cleanup()
  atWidth(realWidth)
  for (const r of restore.splice(0).reverse()) r()
})

/** Shadow a layout property on HTMLElement for this test only. */
function stubBox(prop: 'clientWidth' | 'scrollWidth' | 'offsetWidth', of: (el: HTMLElement) => number): void {
  Object.defineProperty(HTMLElement.prototype, prop, {
    configurable: true,
    get(this: HTMLElement) {
      return of(this)
    },
  })
  restore.push(() => {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>)[prop]
  })
}

/** jsdom's `scrollLeft` ignores writes; this one keeps them. */
function keepScroll(): void {
  const at = new WeakMap<HTMLElement, number>()
  Object.defineProperty(HTMLElement.prototype, 'scrollLeft', {
    configurable: true,
    get(this: HTMLElement) {
      return at.get(this) ?? 0
    },
    set(this: HTMLElement, v: number) {
      at.set(this, v)
    },
  })
  restore.push(() => {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollLeft
  })
}

const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

async function list(): Promise<void> {
  render(<WorkflowsScreen view="" onView={() => {}} />)
  await waitFor(() => expect(document.querySelector(`.wfg-table tr[data-workflow="${AUDIT}"]`)).toBeTruthy(), { timeout: 4000 })
}

const row = (id: string) => document.querySelector<HTMLElement>(`.wfg-table tr[data-workflow="${id}"]`)!

/**
 * The row's summary in the docked inspector (the picked Work redesign, frame
 * graph-workflows-rest), opened by a click on the row's state cell: the id,
 * the runners and the submission moved there from the row.
 */
async function summary(id: string): Promise<HTMLElement> {
  fireEvent.click(row(id).querySelector<HTMLElement>('[data-col="state"]')!)
  return waitFor(() => {
    const s = document.querySelector<HTMLElement>(`aside[aria-label="Workflow inspector"] .wfg-sum[data-workflow="${id}"]`)
    expect(s).toBeTruthy()
    return s!
  })
}

/** The summary's `dd` for one `dt`. */
const fact = (sum: HTMLElement, term: string) =>
  ([...sum.querySelectorAll('dt')].find((d) => d.textContent === term)?.nextElementSibling ?? null) as HTMLElement | null

async function page(id: string, tab = ''): Promise<void> {
  render(<WorkflowsScreen view={`wf=${id}${tab ? `&tab=${tab}` : ''}`} onView={() => {}} />)
  await waitFor(() => expect(document.querySelector('.wfp:not(.is-loading) .wfp-head')).toBeTruthy(), { timeout: 4000 })
}

const realWidth = window.innerWidth
function atWidth(px: number): void {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: px })
}

/** The Overview, the inspector at rest (graph-workflow-rest): open at 1440,
 *  where the dock is not folded. */
async function overview(id: string, tab = ''): Promise<HTMLElement> {
  atWidth(1440)
  await page(id, tab)
  return waitFor(() => {
    const d = document.querySelector<HTMLElement>('aside.wi-dock[aria-label="Workflow overview"]')
    expect(d, 'the Overview did not dock').toBeTruthy()
    return d!
  })
}

/** The fixture's own workflow and tasks, off the real (unmocked) api. */
async function fixtureOf(id: string): Promise<{ workflow: WorkflowRead['workflow']; taskById: Map<string, WorkflowRead['tasks'][number]> }> {
  const r = await real.loadWorkflow(id)
  if (r.status !== 'ok') throw new Error(`fixture ${id} did not load: ${r.status}`)
  return { workflow: r.data.workflow, taskById: new Map(r.data.tasks.map((t) => [t.id, t])) }
}

/**
 * THE CANVAS, RENDERED DIRECTLY (the Graph Work redesign): node-level detail
 * (`.node-dep`) is canvas chrome, and the page's Graph tab draws the stage
 * grid instead, so these cases render `WorkflowCard` the way the old
 * workflow page used to, every stage open so no node is folded into a band.
 */
async function canvasOf(id: string): Promise<HTMLElement> {
  const { workflow, taskById } = await fixtureOf(id)
  const stages = Object.fromEntries(levelsOf(workflow.steps).map((_, i) => [stageKey(id, i), true]))
  const { container } = render(
    <WorkflowCard workflow={workflow} taskById={taskById} usage={{ kind: 'ready', usage: null }} openStages={stages} onToggleStage={() => {}} reload={() => {}} />,
  )
  return container
}

/** The same canvas, as the old workflow page drew it: `page`, a step already picked on arrival. */
function CanvasPageCard({ workflow, taskById, initialPicked }: { id: string; workflow: WorkflowRead['workflow']; taskById: Map<string, WorkflowRead['tasks'][number]>; initialPicked: string }) {
  const [stages, setStages] = useState<Record<string, boolean>>({})
  const [picked, setPicked] = useState<string | null>(initialPicked)
  return (
    <WorkflowCard
      workflow={workflow}
      taskById={taskById}
      usage={{ kind: 'ready', usage: null }}
      openStages={stages}
      onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
      picked={picked}
      onPick={(_wid, stepId) => setPicked((p) => (p === stepId ? null : stepId))}
      page
      reload={() => {}}
    />
  )
}

async function canvasPageOf(id: string, initialPicked: string): Promise<HTMLElement> {
  const { workflow, taskById } = await fixtureOf(id)
  const { container } = render(<CanvasPageCard id={id} workflow={workflow} taskById={taskById} initialPicked={initialPicked} />)
  return container
}

// ---------------------------------------------------------------------------
// The list
// ---------------------------------------------------------------------------

describe('61: the id is the line under the title, not a cut beside it', () => {
  it('draws the title alone on its line and the id under it, whole in its title', async () => {
    await list()
    await waitFor(() => expect(row(AUDIT).querySelector('td[data-col="workflow"] > a')?.textContent).toBe(AUDIT_TITLE))
    const td = row(AUDIT).querySelector<HTMLElement>('td[data-col="workflow"]')!
    // The one-line row (the picked Work redesign, frame graph-workflows-rest)
    // draws the title alone; the id is the summary's line under the title.
    // MUTATION: put `<Id>` back in the name cell of `WorkflowListRow` -- the
    // id takes its 10ch of the title's line and the title is cut at 1440.
    expect(td.querySelector('.id'), 'the id is still beside the title').toBeNull()
    const sum = await summary(AUDIT)
    const id = sum.querySelector<HTMLElement>(':scope > .id')
    expect(id?.textContent).toBe(AUDIT)
    expect(id?.getAttribute('title')).toBe(AUDIT)
  })

  it('gives the id its own one-line micro rule, and the sheet no longer cuts it at 10ch beside the title', async () => {
    const CSS = (await import('../styles/workflows.css?raw')).default
    // MUTATION: drop the `.wfl .wfl-name > .id` rule -- the id is an inline
    // span run on after the title.
    const rule = CSS.match(/\.wfl \.wfl-name > \.id\s*\{([^}]*)\}/)
    expect(rule, 'no rule lays the id under the title').not.toBeNull()
    expect(rule![1]).toMatch(/display:\s*block/)
    expect(rule![1]).toMatch(/white-space:\s*nowrap/)
    expect(rule![1]).toMatch(/font-size:\s*var\(--t-micro\)/)
    expect(CSS).not.toMatch(/\.wfl-line > \.id/)
  })
})

describe('C-11: a lone runner chip keeps its step count', () => {
  it('draws the profile and the count as two boxes, the count never cut', async () => {
    await list()
    // Runners moved from the row into the summary (frame graph-workflows-rest).
    const sum = await summary(STORED)
    const chip = await waitFor(() => {
      const c = fact(sum, 'Runners')?.querySelector<HTMLElement>('.wf-mix > .c-chip')
      expect(c).toBeTruthy()
      return c!
    })
    // MUTATION: print `{m.profile}` bare in `Mix` -- one text run, cut at the
    // chip's end, and `· 3 steps` is what the ellipsis takes.
    expect(chip.querySelector(':scope > .wf-mix-p')?.textContent).toBe('claude-code')
    expect(flat(chip.querySelector(':scope > .wf-mix-n'))).toBe('· 3 steps')
    const CSS = (await import('../styles/workflows.css?raw')).default
    // MUTATION: drop the `.wf-mix-n { flex: none }` rule -- the count shrinks with the profile.
    expect(CSS).toMatch(/\.wf-mix > \.c-chip > \.wf-mix-n\s*\{[^}]*flex:\s*none/)
    expect(CSS).toMatch(/\.wf-mix > \.c-chip > \.wf-mix-p\s*\{[^}]*text-overflow:\s*ellipsis/)
    expect(CSS).toMatch(/span\.wf-mix > \.c-chip\s*\{[^}]*display:\s*flex/)
  })
})

describe('C-10: times are UTC, written with Z', () => {
  it('writes an instant on the same UTC day with seconds and Z, and another day with its date', () => {
    const now = Date.parse('2026-10-28T09:30:00Z')
    // MUTATION: format with getHours()/getMinutes() in `utcStamp` -- a
    // non-UTC browser prints its local clock.
    expect(utcStamp('2026-10-28T02:20:01Z', now)).toBe('02:20:01Z')
    expect(utcStamp('2026-10-27T23:05:09Z', now)).toBe('Oct 27 23:05Z')
    expect(utcStamp(null, now)).toBeNull()
    expect(utcStamp('not a time', now)).toBeNull()
  })

  it('prints the list’s Submitted fact and the page’s start in UTC with Z', async () => {
    await list()
    // Submitted moved from the row into the summary (frame graph-workflows-rest).
    const sum = await summary(AUDIT)
    // MUTATION: print `started.submitted` (`clockTime`, local, no zone) in
    // the Submitted fact, as before.
    await waitFor(() => expect(fact(sum, 'Submitted')?.textContent).toMatch(/Z$/))
    expect(fact(sum, 'Submitted')?.getAttribute('title')).toMatch(/submitted \d{4}-\d\d-\d\dT/)
    cleanup()
    await page(AUDIT)
    // MUTATION: use `workflowStartText` in `pageSummary` -- `started 02:21:40`.
    await waitFor(() => expect(flat(document.querySelector('.c-count-note'))).toMatch(/started (\d\d:\d\d:\d\dZ|[A-Z][a-z]{2} \d{1,2} \d\d:\d\dZ)$/))
  })
})

describe('62: a state the API did not derive reads `state unknown`, the why in its title', () => {
  it('says `state unknown` on the mark and draws no API sentence under the title', async () => {
    await list()
    const mark = await waitFor(() => {
      const m = row(STORED).querySelector<HTMLElement>('td[data-col="state"] [data-mark="unknown"]')
      expect(m).toBeTruthy()
      return m!
    })
    // MUTATION: pass `h.word` straight through in `WorkflowStateMark`.
    expect(flat(row(STORED).querySelector('td[data-col="state"]'))).toBe('state unknown')
    expect(mark.closest('[title]')?.getAttribute('title') ?? mark.getAttribute('title')).toMatch(/did not derive the state/)
    expect(flat(row(STORED))).not.toMatch(/not derived/)
    // The why line is the summary's now (frame graph-workflows-rest).
    // MUTATION: drop the not-derived arm from `listRowWhy` -- the summary's why
    // line says `this API did not derive the state from the steps` again.
    const sum = await summary(STORED)
    expect(sum.querySelector('.wfl-why'), 'the API’s explanation is still the why line').toBeNull()
    expect(flat(sum)).not.toMatch(/not derived/)
  })
})

// ---------------------------------------------------------------------------
// One workflow's page
// ---------------------------------------------------------------------------

describe('80: the loading head reserves what the loaded head draws', () => {
  it('draws no buttons in the head while the page reads, as the loaded head has none: Copy id, Copy link and Stop are the Overview’s', async () => {
    api.loadWorkflow.mockImplementation(() => new Promise<Result<WorkflowRead>>(() => {}))
    render(<WorkflowsScreen view={`wf=${AUDIT}`} onView={() => {}} />)
    const head = await waitFor(() => {
      const h = document.querySelector<HTMLElement>('.wfp.is-loading .wfp-head')
      expect(h).toBeTruthy()
      return h!
    })
    // MUTATION: put `<Button disabled>Copy id</Button>` back in `WorkflowPageSkeleton`'s head.
    expect(head.querySelectorAll('button')).toHaveLength(0)
    expect(head.querySelector('.wfp-actions')).toBeNull()
  })
})

describe('81: the page says one state', () => {
  it('draws no stored-state line on a workflow whose stored copy disagreed', async () => {
    await page(AUDIT)
    await waitFor(() => expect(document.querySelector('.wfp.wfp-main')).toBeTruthy())
    // THE DRIFT LINE IS BEHIND "Workflow id and reads" NOW (the redesign,
    // graph-workflow-rest), not drawn directly under the head any more.
    const main = document.querySelector<HTMLElement>('.wfp.wfp-main')!
    // MUTATION: render `<StateDrift>` in the main pane again -- `stored as
    // queued · steps running · stored copy corrected` beside a head that
    // says running.
    expect(main.querySelector('.wf-drift')).toBeNull()
    expect(flat(main)).not.toMatch(/stored as|stored copy/)
  })
})

describe('82: the cost pill names its unit and keeps a space', () => {
  it('reads `$… N of M steps that ran reported`, with a space before the count', async () => {
    await page(AUDIT)
    // THE COST IS ONE OF THE HEAD'S `.wfp-fig`s now, not a dedicated `.wfp-cost`.
    const spend = await waitFor(() => {
      const s = document.querySelector<HTMLElement>('.wfp-head .wfp-fig .wf-spend:not(.is-reading)')
      expect(s?.querySelector('.wf-spend-cov')).toBeTruthy()
      return s!
    }, { timeout: 4000 })
    // MUTATION: drop the `{' '}` before `.wf-spend-cov` in `Spend` -- the
    // copied text reads `$0.5183 of 4 reported`.
    expect(spend.textContent).toMatch(/^\$[\d.,]+ \d+ of \d+ steps? that ran reported$/)
  })
})

describe('83: one verb, Stop, for every stop on the page', () => {
  it('labels the Overview’s trigger Stop workflow and the step card Stop', async () => {
    // Stop moved off the head into the Overview (graph-workflow-rest); the
    // running node's own inline Stop is canvas chrome, not drawn by the
    // page's Graph tab any more (the stage grid's chips are plain pick
    // buttons) -- so the two surfaces the page still offers are the
    // Overview's trigger and the picked step's card.
    const ov = await overview(AUDIT)
    // MUTATION: put `Cancel workflow` back on the Overview's trigger.
    await waitFor(() => expect(within(ov).getByRole('button', { name: 'Stop workflow' })).toBeTruthy())
    expect(within(ov).queryByRole('button', { name: /cancel/i })).toBeNull()
    // The running step's card, opened from the grid.
    const chip = await waitFor(() => {
      const b = document.querySelector<HTMLButtonElement>('.sg [data-step="scan-terraform"]')
      expect(b).toBeTruthy()
      return b!
    })
    fireEvent.click(chip)
    const dock = await waitFor(() => {
      const d = document.querySelector<HTMLElement>('aside.wi-dock[aria-label="Step scan-terraform"]')
      expect(d).toBeTruthy()
      return d!
    })
    // MUTATION: put `label="Stop step"` back on the card's `StopRun`.
    const card = await waitFor(() => {
      const c = dock.querySelector<HTMLElement>('.wf-inspect-acts')
      expect(c).toBeTruthy()
      return c!
    })
    expect(flat(card.querySelector('button'))).toBe('Stop')
  })
})

describe('overview-agents-85: the stop dialog asks for the id, and its hint says id', () => {
  it('says the button unlocks when the id matches exactly, then that the id matches', async () => {
    const ov = await overview(AUDIT)
    fireEvent.click(await waitFor(() => within(ov).getByRole('button', { name: 'Stop workflow' })))
    const dialog = await screen.findByRole('dialog')
    // MUTATION: drop `noun="id"` from CancelWorkflow's TypedConfirm -- the
    // hint asks for a "name" the reader is told nowhere to type (QA1C-FOLLOWUPS).
    expect(flat(dialog.querySelector('.c-hint'))).toBe('The button unlocks when the id matches exactly.')
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: AUDIT } })
    expect(flat(dialog.querySelector('.c-hint'))).toBe('The id matches.')
  })
})

describe('84: a recorded stop is said, and the head shows it pending', () => {
  it('keeps the API’s answer across the page’s re-read and says what was stopped', async () => {
    api.cancelWorkflow.mockResolvedValue({
      status: 'ok',
      fetchedAt: Date.now(),
      data: { workflow_id: AUDIT, tasks_cancelled: ['task_wf_scan_a', 'task_wf_scan_b'], tasks_already_terminal: ['task_wf_plan'] },
    } satisfies Result<CancelWorkflowResult>)
    const ov = await overview(AUDIT)
    fireEvent.click(await waitFor(() => within(ov).getByRole('button', { name: 'Stop workflow' })))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: AUDIT } })
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Stop the workflow' }))
    })
    expect(api.cancelWorkflow).toHaveBeenCalledWith(AUDIT)
    // The stop's own reload remounts the page; the answer is held above it.
    // The trigger and the recorded answer moved into the Overview with it
    // (graph-workflow-rest).
    // MUTATION: drop `setSent`/`onSent` in `CancelWorkflow.send` -- the dialog
    // closes and the Overview is exactly as it was, `Stop workflow` and all.
    const said = await waitFor(
      () => {
        const s = document.querySelector<HTMLElement>('aside.wi-dock[aria-label="Workflow overview"] .wfp-cancel-sent')
        expect(s).toBeTruthy()
        return s!
      },
      { timeout: 4000 },
    )
    expect(said.getAttribute('role')).toBe('status')
    expect(flat(said)).toBe('Stop recorded: 2 steps asked to stop, 1 step had already ended.')
    // "stop requested" is `CancelSent`'s own fallback tag, beside the
    // recorded line, while the read itself has not caught up with
    // `cancel_requested` yet -- both moved into the Overview with the
    // control (graph-workflow-rest), not the head.
    const ovAfter = document.querySelector<HTMLElement>('aside.wi-dock[aria-label="Workflow overview"]')!
    expect(flat(ovAfter)).toContain('stop requested')
    expect(within(ovAfter).queryByRole('button', { name: 'Stop workflow' }), 'the trigger is still offered after the stop was recorded').toBeNull()
  })

  it('says only that the stop was recorded when the API names no step', async () => {
    api.cancelWorkflow.mockResolvedValue({
      status: 'ok',
      fetchedAt: Date.now(),
      data: { workflow_id: AUDIT, tasks_cancelled: [], tasks_already_terminal: [] },
    } satisfies Result<CancelWorkflowResult>)
    const ov = await overview(AUDIT)
    fireEvent.click(await waitFor(() => within(ov).getByRole('button', { name: 'Stop workflow' })))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: AUDIT } })
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Stop the workflow' }))
    })
    // MUTATION: count `tasks_cancelled` whatever its length -- `0 steps asked to stop`.
    await waitFor(
      () =>
        expect(flat(document.querySelector('aside.wi-dock[aria-label="Workflow overview"] .wfp-cancel-sent'))).toBe('Stop recorded.'),
      { timeout: 4000 },
    )
  })
})

describe('89: the workflow’s time view is not a second Timeline', () => {
  it('names the tab Steps over time and keeps its address', async () => {
    await page(AUDIT)
    const tabs = document.querySelector<HTMLElement>('.wfp')!
    // MUTATION: use `VIEW_LABEL` for the page's tabs again -- `Timeline`, the
    // nav's platform-lanes screen's name.
    const tab = within(tabs).getByRole('tab', { name: 'Steps over time' })
    expect(tab.getAttribute('href')).toMatch(/\/timeline/)
    expect(within(tabs).queryByRole('tab', { name: 'Timeline' })).toBeNull()
  })
})

describe('73: the Graph tab draws the graph, the Table tab the table', () => {
  it('draws no steps table under the graph', async () => {
    await page(AUDIT)
    // The Graph tab is the stage grid now (graph-workflow-rest), not the canvas.
    await waitFor(() => expect(document.querySelector('.sg')).toBeTruthy())
    // MUTATION: put the `.wfp-steps` `WorkflowSteps` back under the grid.
    expect(document.querySelector('.wfp-steps')).toBeNull()
    expect(document.querySelector('.wfp table')).toBeNull()
  })
})

describe('64: a step that reads many files says how many', () => {
  it('draws `↑ 5 steps · 5 files` on the synthesis node, the whole list its title', async () => {
    // `.node-dep` is canvas chrome (the page's Graph tab is the stage grid
    // now): render `WorkflowCard` directly, as the old workflow page did.
    const container = await canvasOf(SYNTH)
    const dep = await waitFor(() => {
      const d = container.querySelector<HTMLElement>('.node-slot:has(button.node[data-step="synthesis"]) .node-dep')
      expect(d).toBeTruthy()
      return d!
    })
    // MUTATION: drop the `DEP_FILES_LISTED` arm in `StepNode` -- the node
    // names all five parents and their files, ~260px of card.
    expect(flat(dep)).toBe('↑ 5 steps · 5 files')
    expect(dep.getAttribute('title')).toMatch(/fencing \(fencing\.md\),/)
    expect(dep.getAttribute('aria-label')).toBe(dep.getAttribute('title'))
  })

  it('keeps naming the file of a step that reads one', async () => {
    const container = await canvasOf(AUDIT)
    const deps = await waitFor(() => {
      const d = [...container.querySelectorAll<HTMLElement>('.node-dep')]
      expect(d.length).toBeGreaterThan(0)
      return d
    })
    for (const d of deps) expect(d.classList.contains('is-count')).toBe(false)
  })
})

describe('63: the graph brings the picked step round without cutting its neighbours', () => {
  it('opens a 595px canvas on whole cards, never mid-card at its left edge', async () => {
    // The 1440 page with the step card open: the ~595px wrapper of the report, the 72px rail.
    keepScroll()
    stubBox('clientWidth', (el) => (el.classList.contains('wf-canvas-wrap') ? 595 : 0))
    stubBox('scrollWidth', (el) =>
      el.classList.contains('wf-canvas-wrap') ? 72 + Number.parseFloat(el.querySelector<HTMLElement>('.wf-canvas')?.style.width ?? '0') : 0,
    )
    stubBox('offsetWidth', (el) => (el.classList.contains('wf-levels') ? 72 : 0))
    // The arrival pick and the failure line are canvas chrome (the page's
    // Graph tab is the stage grid now): `WorkflowCard`, `page`, rendered as
    // the old workflow page used to.
    const container = await canvasPageOf(SYNTH, 'checkpoints')
    // THE READER BRINGS IT ROUND (visual QA pass 2, overview-agents-63,
    // #2694): the page's own pick on arrival opens no stage and scrolls
    // nothing, so the scroll measured here is the one the failure line's
    // control makes.
    fireEvent.click(await waitFor(() => container.querySelector<HTMLButtonElement>('.wfp-fail-lead-go')!))
    const wrap = await waitFor(() => {
      const w = container.querySelector<HTMLElement>('.wf-canvas-wrap')
      expect(w?.querySelector('.node.is-picked')).toBeTruthy()
      return w!
    })
    const left = wrap.scrollLeft
    const slots = [...wrap.querySelectorAll<HTMLElement>('.node-slot')]
    expect(slots.length).toBeGreaterThan(0)
    // MUTATION: centre the picked stage again in the arrival scroll -- a card
    // straddles the canvas's left edge (`encing`).
    for (const s of slots) {
      const x = Number.parseFloat(s.style.left)
      const w = Number.parseFloat(s.style.width)
      const cut = x < left && left < x + w
      expect(cut, `${s.querySelector('[data-step]')?.getAttribute('data-step')} is cut at the canvas's left edge (scrollLeft ${left})`).toBe(false)
    }
    // And the picked card is whole in view.
    const picked = slots.find((s) => s.querySelector('.node.is-picked'))!
    expect(72 + Number.parseFloat(picked.style.left) + Number.parseFloat(picked.style.width) - left).toBeLessThanOrEqual(595)
  })
})
