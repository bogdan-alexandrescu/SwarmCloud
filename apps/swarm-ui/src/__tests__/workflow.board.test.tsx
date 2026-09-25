// THE WORKFLOW BOARD, AS BEHAVIOUR.
//
// Every claim here is made against the DOM the board actually produced, or
// against the computed style the SHIPPED `styles.css` actually resolves to.
// None of it is a source grep, and that is deliberate: a verifier on an earlier
// wave neutered a guard in this app with `false &&` and the suite stayed green,
// because the test only checked that a string appeared in the SOURCE. Every
// string this file looks for is also written in a comment somewhere above the
// code that produces it.
//
// THE FOUR CLAIMS:
//
//  1. The COLLAPSED row tells a fan-out from a chain. Five steps in parallel
//     and five steps in a line have the same id, the same state, the same
//     fraction done and the same spend; if the row draws them identically it
//     has thrown away the only field that is not recoverable from the others.
//  2. A step that HAS NOT STARTED shows an absence, never `0s`. And the other
//     half of the same rule, which is the half that gets lost while fixing the
//     first: a step that really did take zero seconds shows the digit `0`.
//  3. An expanded node still carries the NAME, the STATUS and the RUNNER
//     PROFILE -- the three facts it has always carried.
//  4. Spend that nobody reported reads "not reported", never `$0.00`; a
//     reported zero reads as a number.

import STYLES from '../styles.css?raw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import { WorkflowCard, WorkflowsScreen } from '../Workflows'
import {
  CANVAS_COLUMN,
  MONO_ADVANCE_EM,
  NODE_CHROME_W,
  NODE_W,
  PAD,
  SIB_GAP,
  STAGE_FITS,
  autoTier,
  foldMix,
  layoutOf,
  mixChipW,
  monoW,
  moreChipW,
  profileMix,
  nodeHeightAt,
  nodeWidthAt,
  shapeOf,
  stageCensus,
  stageFitsAt,
  stepDuration,
  type ZoomTier,
} from '../dag'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'

// ---------------------------------------------------------------------------
// Fixtures. Hand-built rather than reused from `api.ts`, because the cases this
// file is about -- a chain, a wide fan-out, a step that never started, a run
// that really cost zero -- are precisely the ones the development fixture does
// not contain.
// ---------------------------------------------------------------------------

const T0 = Date.parse('2026-09-23T12:00:00.000Z')
const iso = (offsetMs: number) => new Date(T0 + offsetMs).toISOString()

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return {
    step_id,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    depends_on,
    input_from: {},
    task_id: null,
    ...over,
  }
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'u-bogdan',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-600_000),
    updated_at: iso(-60_000),
    started_at: null,
    completed_at: null,
    submitted_by: 'bogdan@saga.xyz',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_test',
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

function workflow(workflow_id: string, steps: WorkflowStep[], over: Partial<Workflow> = {}): Workflow {
  return {
    workflow_id,
    state: 'RUNNING',
    tenant_id: 'u-bogdan',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: {
      state: 'RUNNING',
      complete: true,
      reason: 'steps_hold_capacity',
      counts: { SUCCEEDED: 1, unstarted: Math.max(0, steps.length - 1) },
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: steps.length,
    },
    created_at: iso(-900_000),
    updated_at: iso(-60_000),
    submitted_by: 'bogdan@saga.xyz',
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps,
    ...over,
  }
}

/** Three steps, one after another. Widths `1 → 1 → 1`. */
function chain(): Workflow {
  return workflow('wf_chain', [
    step('plan', []),
    step('build', ['plan']),
    step('ship', ['build']),
  ])
}

/**
 * The measured six-step shape from a real run: one step opens into five that
 * run in parallel, and they join back into one. Widths `1 → 5 → 1`.
 */
function fanOut(): Workflow {
  const scans = ['scan-a', 'scan-b', 'scan-c', 'scan-d', 'scan-e']
  return workflow('wf_fan', [
    step('plan', []),
    ...scans.map((s) => step(s, ['plan'], { runner_profile: 'codex' })),
    step('report', scans),
  ])
}

const noop = () => {}

/**
 * Pin the wall clock to the instant the fixtures are written against.
 *
 * `Date.now` is SPIED rather than the timers faked: `vi.useFakeTimers()` also
 * fakes the clock `waitFor` and `findBy*` measure their own timeouts with, and
 * one describe below genuinely waits on the development fixture's 300ms. This
 * touches only the reading the components take. `restoreMocks: true` in
 * vitest.config.ts puts it back after every test.
 */
function pinTheClock(): void {
  beforeEach(() => {
    vi.spyOn(Date, 'now').mockReturnValue(T0)
  })
}

/**
 * The card, with a real store behind its stage expansion.
 *
 * IT IS A HARNESS RATHER THAN A BARE `render`, AND THAT IS THE POINT OF THE
 * SHAPE IT TESTS. `WorkflowCard` does not own which stages are open --
 * `WorkflowsScreen` holds that above the `key` a stop-and-reload bumps, so a
 * stage the reader opened is not closed by the board re-reading itself. A test
 * that passed a frozen `openStages={{}}` could never click a band open, and a
 * `WorkflowCard` that quietly took the state back into a `useState` of its own
 * would still pass it. This harness is the store, and
 * `test('survives the card being remounted')` below is what proves the
 * component is not secretly keeping a second copy.
 */
function CardHarness({
  workflow,
  taskById,
  expanded,
  cardKey = 0,
  zoom = 'auto',
}: {
  workflow: Workflow
  taskById: Map<string, Task> | null
  expanded: boolean
  cardKey?: number
  /** The semantic-zoom tier to start at. `auto` is what the board lands on and
   *  is what every case that is not specifically about the zoom uses. */
  zoom?: 'auto' | ZoomTier
}) {
  const [stages, setStages] = useState<Record<string, boolean>>({})
  // THE STORE, HERE FOR THE SAME REASON THE STAGE STORE IS. The real screen
  // holds the zoom above the `key` a stop-and-reload bumps, so the control has
  // to be driven from outside the card -- a test that passed a frozen `zoom`
  // could never click a segment, and a card that had quietly taken the choice
  // into a `useState` of its own would still pass.
  const [choice, setChoice] = useState<'auto' | ZoomTier>(zoom)
  return (
    <WorkflowCard
      key={cardKey}
      workflow={workflow}
      taskById={taskById}
      expanded={expanded}
      zoom={choice}
      onZoom={(_id, next) => setChoice(next)}
      // `ready` with a null usage is "the attempt read landed and this board
      // was outside its sample", which is the state these cases are about --
      // not `reading`, which would put every figure behind a placeholder and
      // make the assertions below pass for the wrong reason.
      usage={{ kind: 'ready', usage: null }}
      onToggle={noop}
      openStages={stages}
      onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
      reload={noop}
    />
  )
}

function card(
  w: Workflow,
  taskById: Map<string, Task> | null = new Map(),
  expanded = false,
  zoom: 'auto' | ZoomTier = 'auto',
) {
  return render(
    <CardHarness workflow={w} taskById={taskById} expanded={expanded} zoom={zoom} />,
  )
}

/**
 * Open every collapsed stage in the rendered graph, and say how many there
 * were.
 *
 * IT RE-QUERIES RATHER THAN ITERATING A SNAPSHOT, and it returns the count so
 * a caller can assert it actually did something. A loop over a `NodeList`
 * captured before the first click is the shape that silently visits one element
 * -- the repository has shipped "clean sweep" reports from exactly that -- and
 * a helper that expanded nothing would make every assertion after it pass for
 * the wrong reason.
 */
function openEveryBand(root: ParentNode): number {
  let opened = 0
  for (;;) {
    const band = root.querySelector<HTMLButtonElement>('.wf-band[aria-expanded="false"]')
    if (band === null) return opened
    fireEvent.click(band)
    opened += 1
    // A band that re-renders still collapsed would spin here for ever. Fail
    // loudly instead: no fixture in this file has more than a handful of
    // levels.
    if (opened > 50) throw new Error('clicking a band did not expand it')
  }
}

function withStyles(): HTMLStyleElement {
  const el = document.createElement('style')
  el.textContent = STYLES
  document.head.appendChild(el)
  return el
}

// ---------------------------------------------------------------------------
// 1. The collapsed row carries the topology
// ---------------------------------------------------------------------------

describe('the collapsed row', () => {
  it('distinguishes a fan-out from a chain', () => {
    const a = card(chain())
    const chainBar = a.container.querySelector('.wf-bar')!
    const b = card(fanOut())
    const fanBar = b.container.querySelector('.wf-bar')!

    // The widths, in words. This is the part a screen reader gets.
    expect(chainBar.textContent).toContain('1 → 1 → 1')
    expect(fanBar.textContent).toContain('1 → 5 → 1')
    expect(chainBar.textContent).not.toContain('1 → 5 → 1')

    // NO DRAWING ON THIS ROW ANY MORE. The mini-map was a ~60px thumbnail of
    // the DAG, and at that size a six-node graph is a smudge -- it took the
    // width a legible fact could have used and gave back a picture nobody can
    // read. The graph is what EXPANDING is for, and the expanded canvas is
    // pinned by 'draws a real edge for every real dependency' below.
    expect(chainBar.querySelector('.wf-mini')).toBeNull()
    expect(fanBar.querySelector('.wf-mini')).toBeNull()

    // So the shape text above is the whole of the collapsed row's claim about
    // topology, and it has to carry the distinction on its own. It does: the
    // widths differ, and the hover sentence names the kind.
    const shapeTitle = (bar: Element) =>
      bar.querySelector('.wf-shape')!.getAttribute('title') ?? ''
    expect(shapeTitle(chainBar)).toMatch(/chain/i)
    expect(shapeTitle(fanBar)).toMatch(/fan-out|join/i)
  })

  it('names the shape in words a reader can hover', () => {
    const a = card(chain())
    expect(a.container.querySelector('.wf-shape')!.getAttribute('title')).toContain('chain')
    const b = card(fanOut())
    const label = b.container.querySelector('.wf-shape')!.getAttribute('title')!
    expect(label).toContain('parallel')
    expect(label).toContain('converge')
  })

  it('carries the id, the derived state and the progress on one line', () => {
    const { container } = card(chain())
    const bar = container.querySelector('.wf-bar')!
    expect(bar.querySelector('.id')!.textContent).toBe('wf_chain')
    expect(bar.querySelector('.wf-state')!.textContent).toContain('running')
    expect(bar.querySelector('.wf-progress-text')!.textContent).toContain('1/3 done')
    // One row, one bar: the graph is NOT drawn until it is opened.
    expect(container.querySelector('.wf-canvas')).toBeNull()
  })

  it('draws NO FILL over a rollup the server could not complete, and hatches the track', () => {
    // RE-POINTED, and it pins a STRONGER claim than it used to.
    //
    // WHAT MOVED. This asserted `.wf-meter` was absent and that the amber
    // words read `2 of 3 steps: state unread`. Drawing no track at all was a
    // defect at 390px: `.wf-progress-text` is one of the columns that drops
    // below 560px, so a workflow whose census could not be read rendered an
    // EMPTY CELL on a phone -- the strongest available way of saying "nothing
    // is wrong here". The track is now drawn and HATCHED, which is
    // design-system.md §6.4's `.is-unknown`: no fill and no axis, because
    // there is no scale to start.
    //
    // WHERE THE WORDS WENT. `state unread` is still on the surface, in
    // `.wf-progress-text`, shortened to `steps unread` -- the colon and the
    // restatement were the only part a reader could not get from the hatch.
    // The sentence that explains WHY is the track's `aria-label`, and the
    // argument is at `#help/read-failed`.
    //
    // WHAT IS PINNED, and it is the half that carries the invariant: the
    // track exists, it is hatched, and NO FILL ELEMENT IS RENDERED. A fill
    // would be a width, and a width is a measurement of a census that failed.
    const w = workflow('wf_partial', chain().steps, {
      rollup: {
        state: 'UNKNOWN',
        complete: false,
        reason: 'step_read_budget_exhausted',
        counts: {},
        unreadable_steps: ['build', 'ship'],
        unstarted_steps: [],
        steps_read: 1,
      },
    })
    const { container } = card(w)
    const meter = container.querySelector('.wf-meter')!
    expect(meter, 'no track is drawn at all, so a phone shows an empty cell').toBeTruthy()
    expect(meter.className).toContain('is-unknown')
    // The shared primitive draws it, so the hatch and the missing axis are one
    // rule rather than a second hand-rolled bar (§6.4).
    expect(meter.className).toContain('ctl-track')
    expect(
      meter.querySelector('.wf-meter-fill'),
      'a fill was rendered over a census the server could not complete',
    ).toBeNull()
    expect(container.querySelector('.wf-progress.untrusted')!.textContent).toBe(
      '2 of 3 steps unread',
    )
    // No digit may reach the reader as a proportion: the only figures here are
    // the counts, which were read off an array and are exact.
    expect(meter.getAttribute('style')).toBeNull()
    // And the sentence is reachable without a mouse.
    expect(meter.getAttribute('aria-label')).toContain('not a stalled workflow')
  })

  it('opens on click and closes again', () => {
    function Harness() {
      const [open, setOpen] = useState(false)
      const [stages, setStages] = useState<Record<string, boolean>>({})
      return (
        <WorkflowCard
          workflow={fanOut()}
          taskById={new Map()}
          expanded={open}
          usage={{ kind: 'ready', usage: null }}
          onToggle={(_id, was) => setOpen(!was)}
          openStages={stages}
          onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
          reload={noop}
        />
      )
    }
    const { container } = render(<Harness />)
    const bar = container.querySelector('.wf-bar')!
    expect(bar.getAttribute('aria-expanded')).toBe('false')
    expect(container.querySelector('.wf-canvas')).toBeNull()

    fireEvent.click(bar)
    expect(bar.getAttribute('aria-expanded')).toBe('true')
    expect(container.querySelector('.wf-canvas')).toBeTruthy()

    fireEvent.click(bar)
    expect(container.querySelector('.wf-canvas')).toBeNull()
  })

  it('is what the board lands on, before anything is clicked', async () => {
    // Through the real screen and the real fixture, so "collapsed by default"
    // is a property of the product rather than of a prop passed in a test.
    render(<WorkflowsScreen />)
    await screen.findByText('wf_audit_01', {}, { timeout: 4000 })
    await waitFor(() => expect(document.querySelectorAll('.wf-bar').length).toBeGreaterThan(0))
    expect(document.querySelector('.wf-canvas')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// 2. Duration: the absent one, the measured zero, and the three in between
// ---------------------------------------------------------------------------

/** One workflow with every kind of step timing on it at once. */
function timings(): { w: Workflow; tasks: Map<string, Task> } {
  const steps = [
    step('done', [], { task_id: 'task_done' }),
    step('instant', [], { task_id: 'task_instant' }),
    step('live', ['done'], { task_id: 'task_live' }),
    step('waiting', ['done'], { task_id: 'task_waiting' }),
    step('held', ['done'], { task_id: 'task_held' }),
    // NO task_id at all: the workflow has not reached it.
    step('publish', ['live', 'waiting', 'held']),
  ]
  const tasks = new Map<string, Task>([
    [
      'task_done',
      task('task_done', 'SUCCEEDED', {
        started_at: iso(-160_000),
        completed_at: iso(-92_000),
      }),
    ],
    [
      // Started and finished inside the same second. A MEASURED zero.
      'task_instant',
      task('task_instant', 'SUCCEEDED', {
        started_at: iso(-160_000),
        completed_at: iso(-160_000),
      }),
    ],
    ['task_live', task('task_live', 'RUNNING', { started_at: iso(-68_000) })],
    ['task_waiting', task('task_waiting', 'LEASED', { created_at: iso(-153_000) })],
    [
      'task_held',
      task('task_held', 'PARKED', {
        park_reason: 'DEPENDENCY_INCOMPLETE',
        updated_at: iso(-431_000),
      }),
    ],
  ])
  return { w: workflow('wf_time', steps), tasks }
}

function nodeNamed(root: ParentNode, name: string): HTMLElement {
  const found = [...root.querySelectorAll<HTMLElement>('.node')].find(
    (n) => n.querySelector('.node-id')?.textContent?.startsWith(name),
  )
  expect(found, `no node named ${name} was rendered`).toBeTruthy()
  return found!
}

/**
 * The positioned box a node is laid out in.
 *
 * WHAT MOVED: `.node` used to BE the positioned element and carried the
 * layout's inline `left`/`top`. The node is the link now, so `.node` is an
 * `<a>`, and a `<button>` may not live inside an `<a>` -- the stop control had
 * to become its sibling. `.node-slot` is the parent that holds both and is
 * what `layoutOf` now positions. The coordinates are the same coordinates.
 */
function slotOf(root: ParentNode, name: string): HTMLElement {
  const slot = nodeNamed(root, name).closest<HTMLElement>('.node-slot')
  expect(slot, `the node named ${name} is not inside a .node-slot`).toBeTruthy()
  return slot!
}

// AT THE `details` TIER, AND THAT IS WHAT THESE CASES ARE ABOUT. The duration
// line (`.node-dur`) is the subject here, and `details` is the tier that draws
// it for every kind. At `figures` the node draws the run in its `ran` figure
// instead and keeps the line only for a WAIT -- `ran 1m 8s` over `ran 1m 8s`
// was one duration printed twice -- which `a node at the Figures tier` in the
// QA section below pins. These rendered at `auto`, which is `figures` for this
// fixture; the claims are the same claims, read where the line is drawn.
describe('how long a step has taken', () => {
  pinTheClock()

  it('shows an ABSENCE for a step that has not started, and never 0s', () => {
    const { w, tasks } = timings()
    const { container } = card(w, tasks, true, 'details')
    const publish = nodeNamed(container, 'publish')
    const dur = publish.querySelector('.node-dur')!

    expect(dur.textContent).toBe('not started')
    // THE WHOLE RULE, IN ONE LINE: no digit reaches this cell. A `0s` here
    // would be a measurement of something nobody measured.
    expect(dur.textContent, 'an unstarted step rendered a number').not.toMatch(/\d/)
    expect(dur.className).toContain('is-none')
    expect(dur.getAttribute('title')).toContain('absence, not 0s')
  })

  it('shows a MEASURED zero as a digit, which is the half that gets lost', () => {
    const { w, tasks } = timings()
    const { container } = card(w, tasks, true, 'details')
    const dur = nodeNamed(container, 'instant').querySelector('.node-dur')!
    expect(dur.textContent).toBe('ran 0s')
    expect(dur.className).toContain('is-ran')
  })

  it('keeps waiting, running, parked and finished as four different things', () => {
    const { w, tasks } = timings()
    const { container } = card(w, tasks, true, 'details')
    const text = (name: string) => nodeNamed(container, name).querySelector('.node-dur')!.textContent

    // The measured run this was designed against: five parallel steps each
    // waited exactly 153s and then ran 68-92s, and the join waited 431s.
    expect(text('done')).toBe('ran 1m 8s')
    expect(text('live')).toBe('running 1m 8s')
    expect(text('waiting')).toBe('queued 2m 33s')
    expect(text('held')).toBe('parked 7m 11s')

    // Four figures, four different sentences -- collapsing them into one
    // number is what this component exists to stop.
    const notes = ['done', 'live', 'waiting', 'held'].map(
      (n) => nodeNamed(container, n).querySelector('.node-dur')!.getAttribute('title')!,
    )
    expect(new Set(notes).size).toBe(4)
    expect(notes[2]).toContain('Time spent waiting. Nothing has started.')
    expect(notes[3]).toContain('never time worked')
  })

  it('will not time a step whose state was never read', () => {
    // A task_id with no task behind it. Its timings were not looked at, which
    // is not the same as its having taken no time.
    const w = workflow('wf_unread', [step('ghost', [], { task_id: 'task_missing' })])
    const { container } = card(w, new Map(), true, 'details')
    const dur = nodeNamed(container, 'ghost').querySelector('.node-dur')!
    expect(dur.textContent).toBe('duration unread')
    expect(dur.textContent).not.toMatch(/\d/)
  })

  it('has no number to print for an absence, by construction', () => {
    // The type-level half of the same rule: `stepDuration`'s absent arm has no
    // `seconds` field, so a renderer cannot reach for one. This asserts the
    // shape at runtime; `tsc` asserts it at compile time.
    const d = stepDuration({ kind: 'unstarted' }, T0)
    expect(d.kind).toBe('none')
    expect('seconds' in d).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// 3. The expanded canvas
// ---------------------------------------------------------------------------

describe('the expanded canvas', () => {
  pinTheClock()

  it('keeps the NAME, the STATUS and the RUNNER PROFILE on every node', () => {
    const w = fanOut()
    const tasks = new Map<string, Task>()
    const steps = w.steps.map((s, i) =>
      i === 1 ? { ...s, task_id: 'task_scan_a' } : s,
    )
    tasks.set('task_scan_a', task('task_scan_a', 'RUNNING', { started_at: iso(-92_000) }))
    const { container } = card({ ...w, steps }, tasks, true)
    // FIVE PARALLEL STEPS ARE NO LONGER A BAND, AND THAT IS SEMANTIC ZOOM.
    // They were: five is one over `STAGE_FITS`, so this canvas used to land with
    // that stage collapsed and this test used to open it. Five `details` nodes
    // are 5 x 144 + 4 x 28 + 20 = 852px, inside the 1,054px column, so they are
    // DRAWN -- collapsing a five-step fan was only ever necessary because a node
    // was 255px wide whatever it had to say. The stage that fits no tier at all
    // (13 steps) is still a band; `a stage too wide to draw` below covers it.
    expect(autoTier(steps)).toBe('details')
    expect(openEveryBand(container)).toBe(0)
    // The guard the band count used to be: every step is on the canvas, so
    // nothing below is passing over an empty graph.
    expect(container.querySelectorAll('.node')).toHaveLength(7)

    const node = nodeNamed(container, 'scan-a')
    expect(node.querySelector('.node-id')!.textContent).toContain('scan-a')
    expect(node.querySelector('.node-state')!.textContent).toContain('running')
    // The llm used, which the owner asked to keep exactly as it is drawn today.
    expect(node.querySelector('.node-meta')!.textContent).toBe('codex')
    // And the field that was missing.
    expect(node.querySelector('.node-dur')!.textContent).toBe('running 1m 32s')

    // Every node, not only the one under test.
    for (const n of container.querySelectorAll('.node')) {
      expect(n.querySelector('.node-id')!.textContent!.length).toBeGreaterThan(0)
      expect(n.querySelector('.node-state')!.textContent!.trim().length).toBeGreaterThan(0)
      expect(n.querySelector('.node-meta')!.textContent!.trim().length).toBeGreaterThan(0)
    }
  })

  it('draws a real edge for every real dependency', () => {
    const { container } = card(fanOut(), new Map(), true)
    const edges = container.querySelectorAll('.wf-edge')
    expect(edges).toHaveLength(10)
    for (const e of edges) expect(e.getAttribute('d')).toMatch(/^M [\d.]+ [\d.]+ C /)
    // Flow direction is drawn, not implied: every edge ends in an arrowhead.
    expect(container.querySelector('marker')).toBeTruthy()
    for (const e of edges) expect(e.getAttribute('marker-end')).toContain('url(#arrow-')
  })

  it('draws them into and out of a stage that IS a band', () => {
    // THE HALF OF STAGE COLLAPSING THAT IS EASIEST TO LOSE, and it needs a
    // fixture that still collapses. `fanOut` no longer does -- semantic zoom
    // draws its five parallel steps -- so the case moved to the 13-step stage,
    // which fits no tier and is a band at every one of them. The first
    // implementation of collapsing found edges by walking the NODES, and a
    // collapsed stage has none, so all 26 of these vanished. Both endpoints
    // resolve through the STEP, and a step in a band attaches to the band.
    const { w, tasks } = wideStage(13)
    const { container } = card(w, tasks, true)
    expect(container.querySelector('.wf-band')).toBeTruthy()
    expect(container.querySelectorAll('.node')).toHaveLength(2)
    // 13 into `report` and 13 out of `plan`, none of which has a node at
    // either end drawn as a card.
    expect(container.querySelectorAll('.wf-edge')).toHaveLength(26)
  })

  /**
   * RE-POINTED, NOT WEAKENED. The claim is unchanged -- a step with a task
   * opens at the address the rest of the console uses for that task, and a
   * step without one is not a dead link. What moved is WHICH ELEMENT carries
   * the href: there were three anchors inside the node (the step id, `input &
   * output →` and `attempts →`) and all three resolved to the same drawer, so
   * the node itself is the single anchor now and there is nothing linked
   * inside it. The assertion therefore reads the node rather than an `<a>`
   * descendant of it, and it additionally pins that there is NO anchor inside
   * -- the defect this change removes would otherwise come back silently.
   */
  it('opens each step at the address the rest of the console uses for it', () => {
    const w = fanOut()
    const steps = w.steps.map((s, i) => (i === 1 ? { ...s, task_id: 'task_scan_a' } : s))
    const tasks = new Map([['task_scan_a', task('task_scan_a', 'RUNNING')]])
    const { container } = card({ ...w, steps }, tasks, true)
    // Drawn at the `details` tier rather than collapsed -- see the first case in
    // this block. The card is still the one anchor at every tier.
    expect(openEveryBand(container)).toBe(0)
    expect(container.querySelectorAll('.node')).toHaveLength(7)
    const node = nodeNamed(container, 'scan-a')
    expect(node.tagName).toBe('A')
    expect(node.getAttribute('href')).toBe('#work/task/task_scan_a')
    // The node is the ONLY target. Three anchors pointing at one page is the
    // shape this replaced; a fourth appearing inside would be the same defect.
    expect(node.querySelector('a')).toBeNull()
    // A step with no task has nothing to open, so it is not a dead link.
    const unreached = nodeNamed(container, 'report')
    expect(unreached.tagName).toBe('DIV')
    expect(unreached.hasAttribute('href')).toBe(false)
    expect(unreached.querySelector('a')).toBeNull()
  })

  /**
   * RE-POINTED ONTO THE OTHER AXIS. It was `lays the columns out in dependency
   * order, left to right` and it asserted exactly the arrangement the owner
   * asked to be rid of: "all nodes at the same stage displayed horizontally
   * not vertically ... the natural flow of the workflow should be top to
   * bottom rendered rather than left to right."
   *
   * The PROPERTY is identical and both halves survive, transposed: dependency
   * order is monotonic along the flow axis, and the members of one level share
   * a coordinate on that axis while differing on the other. Only which axis is
   * which has changed -- level now drives `top`, position within a level drives
   * `left`. Coordinates are read off `.node-slot`, which is what the layout
   * positions now that the node is an anchor with a sibling stop control.
   */
  it('lays the levels out in dependency order, top to bottom', () => {
    const { container } = card(fanOut(), new Map(), true)
    // NOTHING TO OPEN, AND THE PROPERTY IS UNCHANGED. The five parallel steps
    // are drawn at the `details` tier now rather than collapsed into a band, and
    // neither zoom nor collapsing is allowed to change WHERE they sit: the flow
    // runs top to bottom with the steps of one stage side by side across it, at
    // every tier. The owner's instruction is the layout, not the default.
    expect(openEveryBand(container)).toBe(0)
    const top = (name: string) => Number.parseFloat(slotOf(container, name).style.top)
    const left = (name: string) => Number.parseFloat(slotOf(container, name).style.left)
    expect(top('plan')).toBeLessThan(top('scan-a'))
    expect(top('scan-a')).toBeLessThan(top('report'))
    // The five parallel steps share a BAND and differ only in their position
    // across it -- which is the owner's "displayed horizontally not vertically".
    const scans = ['scan-a', 'scan-b', 'scan-c', 'scan-d', 'scan-e']
    expect(new Set(scans.map(top)).size).toBe(1)
    expect(new Set(scans.map(left)).size).toBe(5)
    // And they are in reading order across the band, not merely distinct.
    const lefts = scans.map(left)
    expect([...lefts].sort((a, b) => a - b)).toEqual(lefts)
  })
})

// ---------------------------------------------------------------------------
// 3b. A stage too wide to draw
// ---------------------------------------------------------------------------
//
// THE SINGLE LARGEST FAILURE THIS SCREEN HAD, measured on a live 30-step run
// (`wf_7e2ee6c3075d43228e5a`, widest stage 13 steps):
//
//   canvas                              1776 x 4134 px
//   visible wrapper                     1138 px wide
//   nodes clipped                       17 of 30
//   nodes fully off-screen              4
//
// The transpose is not what fixed it and was never going to: 13 nodes at
// NODE_W is 3,560px of band on any axis. What fixes it is drawing a stage
// wider than `STAGE_FITS` as ONE BAND, and the four properties below are the
// ones that make that safe rather than merely smaller.

/**
 * A fan of `n` steps between one `plan` and one `report`, with the state of
 * each fan step given -- `null` for a step the workflow has not reached.
 *
 * Shaped after the measured run rather than after the fixture: the point of
 * this section is the case the development data does not contain.
 */
function wideStage(
  n: number,
  states: readonly (TaskState | null)[] = [],
): { w: Workflow; tasks: Map<string, Task> } {
  const fan = Array.from({ length: n }, (_, i) => `scan-${i}`)
  const tasks = new Map<string, Task>()
  const steps: WorkflowStep[] = [step('plan', [])]
  fan.forEach((id, i) => {
    const state = states[i] ?? null
    if (state === null) {
      steps.push(step(id, ['plan'], { runner_profile: 'codex' }))
      return
    }
    const taskId = `task_${id}`
    const done = state === 'SUCCEEDED' || state === 'FAILED' || state === 'CANCELLED'
    tasks.set(
      taskId,
      task(taskId, state, {
        started_at: iso(-90_000),
        completed_at: done ? iso(-30_000) : null,
      }),
    )
    steps.push(step(id, ['plan'], { runner_profile: 'codex', task_id: taskId }))
  })
  steps.push(step('report', fan))
  return { w: workflow('wf_wide', steps), tasks }
}

/** Every bucket the band drew, in the order it drew them. */
function bandCounts(band: Element): string[] {
  return [...band.querySelectorAll('.wf-band-count')].map((c) => c.textContent ?? '')
}

describe('a stage too wide to draw', () => {
  pinTheClock()

  it('never draws a stage wider than the column the canvas has', () => {
    const { w, tasks } = wideStage(13)
    const { container } = card(w, tasks, true)
    const canvas = container.querySelector<HTMLElement>('.wf-canvas')!
    const drawn = Number.parseFloat(canvas.style.width)

    expect(drawn).toBeGreaterThan(0)
    expect(
      drawn,
      `a 13-step stage drew a ${drawn}px canvas into the ${CANVAS_COLUMN}px column .wf-canvas actually gets at 1440`,
    ).toBeLessThanOrEqual(CANVAS_COLUMN)

    // AND THE ASSERTION IS NOT VACUOUS, which is the half a width check
    // usually misses: the SAME fixture with the stage opened draws a 3,580px
    // canvas -- 3.4 times the column it has. So the line above goes red if the
    // stage stops being collapsed by default, if `STAGE_FITS` is raised past
    // what the column holds, or if the band is drawn at the stage's real width
    // instead of `BAND_MIN_W`.
    const opened = layoutOf(w.steps, new Set([1]))
    expect(opened.width).toBe(20 + 13 * NODE_W + 12 * SIB_GAP)
    expect(opened.width).toBeGreaterThan(CANVAS_COLUMN)

    // Every node that IS drawn is inside the canvas that was drawn for it.
    // A canvas narrow enough to fit with nodes hanging out of it would satisfy
    // the line above and be the same defect.
    const slots = [...container.querySelectorAll<HTMLElement>('.node-slot')]
    expect(slots.length).toBeGreaterThan(0)
    for (const slot of slots) {
      const right = Number.parseFloat(slot.style.left) + Number.parseFloat(slot.style.width)
      expect(right, `${slot.textContent?.slice(0, 20)} ends at ${right} in a ${drawn}px canvas`)
        .toBeLessThanOrEqual(drawn)
    }
  })

  it('says what is in the band, by state', () => {
    const states: (TaskState | null)[] = [
      ...(Array(8).fill('RUNNING') as TaskState[]),
      ...(Array(3).fill('SUCCEEDED') as TaskState[]),
      null,
      null,
    ]
    const { w, tasks } = wideStage(13, states)
    const { container } = card(w, tasks, true)
    const band = container.querySelector('.wf-band')!

    expect(band.querySelector('.wf-band-n')!.textContent).toBe('13 steps')
    expect(bandCounts(band)).toEqual(['8 running', '3 succeeded', '2 not started'])
    // The census the band could not fit is still reachable without a mouse.
    expect(band.getAttribute('aria-label')).toContain(
      '13 steps in this stage: 8 running, 3 succeeded, 2 not started.',
    )
    // The 13 cards this replaces are NOT in the document. A band that summarised
    // a stage it had also rendered would be an extra row, not a fix.
    expect(container.querySelectorAll('.node')).toHaveLength(2)
  })

  it('never hides a failure, and marks the band that holds one', () => {
    const states = Array(13).fill('SUCCEEDED') as (TaskState | null)[]
    states[7] = 'FAILED'
    states[11] = 'CANCELLED'
    const { w, tasks } = wideStage(13, states)
    const { container } = card(w, tasks, true)
    const band = container.querySelector('.wf-band')!

    // FIRST, not merely present. `.wf-band-counts` clips rather than wraps --
    // it has to, or the band's height would depend on how many states a stage
    // happens to be in and `layoutOf` could not place the stage under it -- so
    // the bucket at the END of the line is the one that can be lost. A failure
    // is never at the end of the line.
    expect(bandCounts(band).slice(0, 2)).toEqual(['1 failed', '1 cancelled'])
    // Visually distinguishable WITHOUT being expanded and without being read:
    // somebody scanning for what broke must not have to open four bands.
    expect(band.className).toContain('has-failure')
    // And not by colour alone.
    expect(band.querySelector('.wf-band-count.is-bad .ctl-dot.is-bad')).toBeTruthy()
    expect(band.getAttribute('aria-label')).toContain('1 failed and 1 cancelled.')

    // THE MODIFIER MEANS SOMETHING ONLY IF A CLEAN STAGE DOES NOT CARRY IT.
    const clean = wideStage(13, Array(13).fill('SUCCEEDED') as TaskState[])
    const b = card(clean.w, clean.tasks, true)
    const cleanBand = b.container.querySelector('.wf-band')!
    expect(cleanBand.className).not.toContain('has-failure')
    expect(cleanBand.getAttribute('aria-label')).toContain(
      'No step in this stage has failed or been cancelled.',
    )
  })

  it('refuses to call a stage clean when its states were not read', () => {
    // Every fan step carries a task id and the task read returned nothing for
    // any of them -- the partial-read case this whole screen is built around.
    const { w } = wideStage(13, Array(13).fill('SUCCEEDED') as TaskState[])
    const { container } = card(w, new Map(), true)
    const band = container.querySelector('.wf-band')!
    const label = band.getAttribute('aria-label')!

    expect(bandCounts(band)).toEqual(['13 not read'])
    expect(band.className).toContain('has-unread')
    expect(band.className).not.toContain('has-failure')
    expect(label).toContain('cannot be said to be free of failures')
    // THE WHOLE RULE, IN ONE LINE. An unread census is not a clean one, and a
    // band that said so would be this console's central defect in its most
    // compact possible form.
    expect(label, 'a band with no readable states claimed nothing had failed').not.toContain(
      'No step in this stage has failed',
    )
  })

  it('is a button with aria-expanded, and names what it controls once open', () => {
    const { w, tasks } = wideStage(13)
    const { container } = card(w, tasks, true)
    const band = () => container.querySelector<HTMLButtonElement>('.wf-band')!

    expect(band().tagName).toBe('BUTTON')
    expect(band().getAttribute('type')).toBe('button')
    expect(band().getAttribute('aria-expanded')).toBe('false')
    // Collapsed there is nothing to name: `aria-controls` pointing at an id
    // that is not in the document tells a screen reader there is somewhere to
    // go, which is worse than saying nothing.
    expect(band().hasAttribute('aria-controls')).toBe(false)
    expect(container.querySelectorAll('.node')).toHaveLength(2)

    fireEvent.click(band())
    expect(band().getAttribute('aria-expanded')).toBe('true')
    const controls = band().getAttribute('aria-controls')
    expect(controls).toBeTruthy()
    const target = document.getElementById(controls!)
    expect(target, 'aria-controls named an element that is not in the document').toBeTruthy()
    expect(target!.querySelectorAll('.node')).toHaveLength(13)

    fireEvent.click(band())
    expect(band().getAttribute('aria-expanded')).toBe('false')
    expect(container.querySelectorAll('.node')).toHaveLength(2)
  })

  it('keeps a stage open across a remount of the card', () => {
    const { w, tasks } = wideStage(13)
    const { container, rerender } = render(
      <CardHarness workflow={w} taskById={tasks} expanded cardKey={0} />,
    )
    fireEvent.click(container.querySelector<HTMLButtonElement>('.wf-band')!)
    expect(container.querySelectorAll('.node')).toHaveLength(15)

    // WHAT `reload()` DOES TO THE REAL BOARD. `WorkflowsScreen` bumps a key and
    // `Screen` remounts, taking everything held inside it with it -- which is
    // why `open` and the stage store both live ABOVE that key. Changing the
    // card's own key here remounts exactly the part that remounts in
    // production. A `WorkflowCard` that had quietly taken the expansion into a
    // `useState` of its own would close the stage here and nowhere else.
    rerender(<CardHarness workflow={w} taskById={tasks} expanded cardKey={1} />)
    expect(container.querySelector('.wf-band')!.getAttribute('aria-expanded')).toBe('true')
    expect(container.querySelectorAll('.node')).toHaveLength(15)
  })

  it('leaves a stage that fits the FULL tier alone, and keeps every field on it', () => {
    const fits = wideStage(STAGE_FITS)
    const a = card(fits.w, fits.tasks, true)
    expect(a.container.querySelector('.wf-band')).toBeNull()
    expect(a.container.querySelectorAll('.node')).toHaveLength(STAGE_FITS + 2)
    // Nothing was traded for it: `STAGE_FITS` is by definition what the full
    // tier holds, so the figures are still on every card.
    expect(autoTier(fits.w.steps)).toBe('figures')
    expect(a.container.querySelectorAll('.node-nums')).toHaveLength(STAGE_FITS + 2)

    // ONE MORE STEP AND THE FULL TIER DOES NOT HOLD IT -- and this is where
    // semantic zoom replaced collapsing. It used to be a band and two nodes.
    // The four steps are now drawn at the tier that holds four, which is what
    // `nodeWidthAt` and `stageFitsAt` say it is rather than what a fixture
    // remembers.
    const over = wideStage(STAGE_FITS + 1)
    const b = card(over.w, over.tasks, true)
    expect(autoTier(over.w.steps)).toBe('details')
    expect(b.container.querySelector('.wf-band')).toBeNull()
    expect(b.container.querySelectorAll('.node')).toHaveLength(STAGE_FITS + 3)
    // ...and the trade is stated on the canvas rather than left to be noticed.
    expect(b.container.querySelector('.wf-zoom .ctl-mark')!.textContent).toBe(
      'figures not drawn',
    )
  })

  it('counts a stage without rendering anything', () => {
    // The census is pure, so the rule it enforces is assertable without a DOM.
    const { w, tasks } = wideStage(4, ['FAILED', 'RUNNING', 'RUNNING', null])
    const stage = w.steps.filter((s) => s.step_id.startsWith('scan-'))
    const c = stageCensus(stage, tasks)

    expect(c.steps).toBe(4)
    expect(c.failed).toBe(1)
    expect(c.cancelled).toBe(0)
    expect(c.unread).toBe(0)
    expect(c.counts.map((x) => `${x.n} ${x.word}`)).toEqual([
      '1 failed',
      '2 running',
      '1 not started',
    ])
    // Two steps in the same state are one bucket, not two rows.
    expect(c.counts).toHaveLength(3)
  })
})

// ---------------------------------------------------------------------------
// 3c. Semantic zoom
// ---------------------------------------------------------------------------
//
// WHAT THIS BLOCK IS FOR, AND WHAT IT CANNOT DO.
//
// Stage collapsing made a 30-step run possible to read. It did not make the
// canvas good: a stage over the threshold is a band you have to open, and
// opening one puts you back into the 3,671px row the band stood in for. So a
// node now SHEDS FIELDS as the column runs out -- name and state at every tier,
// the profile and the duration above one, the figures only with room -- and the
// band is kept for the stage that fits no tier at all.
//
// THE FOUR CLAIMS, each one a rule this console already holds itself to:
//
//  1. EVERY WIDTH IS DERIVED, and the derivation is the assertion. NODE_W came
//     from a MEASURED character advance rather than an assumed 0.6em, so a tier
//     whose width was a round number somebody liked would be a regression even
//     if it looked right. Each case below recomputes the width from `monoW` and
//     `NODE_CHROME_W` -- the same exports the implementation uses -- and then
//     checks the DOM emitted it.
//  2. NOTHING SHRINKS. `typescale.test.ts` holds six steps and a floor, and the
//     owner has twice said this console is hard to read. Zoom drops FIELDS. No
//     element on the canvas may carry an inline font size, a transform or a
//     zoom at any tier.
//  3. A DROPPED FIELD IS LEGIBLY A ZOOM DECISION. A blank where an unmeasured
//     figure belongs would turn "nobody measured this" into "this is fine",
//     which is the single thing this UI exists to prevent. So a dropped field
//     leaves the card entirely, the canvas names what it dropped, and the
//     control puts it back.
//  4. A FAILED OR CANCELLED STEP IS NEVER INVISIBLE, at any tier, without
//     expanding or zooming anything.
//
// jsdom HAS NO LAYOUT ENGINE, WHICH BOUNDS EVERY CLAIM HERE. `getComputedStyle`
// does not resolve `var()`, `getBoundingClientRect` answers zeroes and
// `clientWidth` answers 0, so nothing below can see overlap, wrapping or real
// text measurement. Every assertion is against the numbers the components
// EMIT -- the inline `width`/`height`/`left`/`top` the layout wrote, and which
// elements exist. That is a real guarantee (a node drawn 255px wide in a 144px
// slot is caught here) and it is not rendered proof. The minimap case below
// asserts the unmeasured branch explicitly rather than pretending otherwise.

/** A workflow of `n` root steps with the given ids, for width arithmetic. */
function named(ids: readonly string[], profile = 'claude-code'): Workflow {
  return workflow(
    'wf_named',
    ids.map((id) => step(id, [], { runner_profile: profile })),
  )
}

describe('semantic zoom', () => {
  pinTheClock()

  it('reproduces the browser measurements its widths are derived from', () => {
    // THE ONE MEASUREMENT EVERYTHING ELSE IS ARITHMETIC OVER, checked against
    // the four figures other passes measured in the browser and wrote into
    // `dag.ts` before `MONO_ADVANCE_EM` existed. If this drifts, every width
    // below is measuring a font nobody has looked at.
    expect(MONO_ADVANCE_EM * 14).toBeCloseTo(8.43, 2)
    // TO THE PIXEL, which is the claim -- `Math.round` rather than
    // `toBeCloseTo`, because two of these land within 0.47px of the recorded
    // figure and a tolerance written as "0 digits" is 0.5px by definition. A
    // guard whose margin is 0.03px is one that goes red on a rounding change
    // rather than on a wrong font.
    //
    // `measureText('no attempt yet')` -- 14 characters, 118.0px at 14px.
    expect(Math.round(monoW(14, 14))).toBe(118)
    // `21.4k in · 3.2k out` -- 19 characters, measured at 160px.
    expect(Math.round(monoW(19, 14))).toBe(160)
    // `99.9k in · 99.9k out` -- 20 characters, measured at 169px.
    expect(Math.round(monoW(20, 14))).toBe(169)
    // `claude-code` at --t-micro -- 11 characters, measured at 79px.
    expect(Math.round(monoW(11, 12))).toBe(79)
    // It is NEAR 0.6em and is not 0.6em, which is the whole reason it is a
    // measurement. A rewrite to the round number would fail this.
    expect(MONO_ADVANCE_EM).not.toBe(0.6)
  })

  it('derives every tier width from the advance and the workflow, not from a literal', () => {
    // The rows, restated from the sheet they are declared in: `.ctl-dot` is 8px,
    // `--ctl-s2` is 8px, `.node-num`'s label column is 46px. The character
    // bounds are the longest strings each row can hold -- `dead_lettered` (13),
    // `duration unread` (15) beside it, `running 365d 23h` (16) on a row of its
    // own, `99.9k in · 99.9k out` (20).
    const stateRow = 8 + 8 + monoW(13, 12)
    const stateAndDurRow = stateRow + 8 + monoW(15, 12)
    const durRow = monoW(16, 12)
    const figuresRow = 46 + 8 + monoW(20, 14)

    // NODE_W IS THE FULL TIER'S ROWS OVER THE CHROME, and it is 255 rather than
    // the 248 that shipped because the chrome now counts the node's 4px of
    // border and because nothing had measured the state-and-duration row.
    expect(NODE_W).toBe(Math.ceil(NODE_CHROME_W + Math.max(stateAndDurRow, figuresRow)))
    expect(NODE_CHROME_W).toBe(12 * 2 + 3 + 1)
    // The figures row is what 248 was chosen for and it did not clear it: this
    // is the 2.6px by which `99.9k in · 99.9k out` ellipsed.
    expect(figuresRow).toBeGreaterThan(248 - NODE_CHROME_W)
    expect(figuresRow).toBeLessThanOrEqual(NODE_W - NODE_CHROME_W)

    const w = named(['plan', 'scan-0', 'report'], 'claude-code')
    const nameW = monoW('scan-0'.length, 14)
    const profileW = monoW('claude-code'.length, 12)

    expect(nodeWidthAt('names', w.steps)).toBe(
      Math.ceil(NODE_CHROME_W + Math.max(nameW, stateRow)),
    )
    expect(nodeWidthAt('details', w.steps)).toBe(
      Math.ceil(NODE_CHROME_W + Math.max(nameW, stateRow, durRow, profileW)),
    )
    expect(nodeWidthAt('figures', w.steps)).toBe(NODE_W)

    // A TIER THAT KEEPS FEWER FIELDS IS NEVER WIDER. Two tiers CAN come out the
    // same width -- when the step name was what set it, no further zoom helps,
    // and that is information rather than a bug -- but the order may not invert.
    expect(nodeWidthAt('names', w.steps)).toBeLessThanOrEqual(nodeWidthAt('details', w.steps))
    expect(nodeWidthAt('details', w.steps)).toBeLessThanOrEqual(nodeWidthAt('figures', w.steps))

    // AND THE WIDTH FOLLOWS THE WORKFLOW'S OWN STRINGS. `.node-id` has no
    // `text-overflow` and no `white-space`, so a name that does not fit WRAPS --
    // onto a card whose height `layoutOf` has already committed to, overlapping
    // the node beneath it. A fixed width would be a bet that nobody names a step
    // something long.
    const long = named(['a-very-long-step-name-indeed'])
    expect(nodeWidthAt('names', long.steps)).toBe(
      Math.ceil(NODE_CHROME_W + monoW('a-very-long-step-name-indeed'.length, 14)),
    )
    expect(nodeWidthAt('names', long.steps)).toBeGreaterThan(nodeWidthAt('names', w.steps))
    // The full tier pays for the `opens the PR` tag too -- 96px of it -- which
    // is why the reduced tiers drop the tag.
    expect(nodeWidthAt('figures', long.steps)).toBe(
      Math.ceil(NODE_CHROME_W + monoW('a-very-long-step-name-indeed'.length, 14) + 96),
    )
    // A long RUNNER PROFILE widens `details` and not `names`, because `names`
    // does not draw it. That is the ladder doing its job rather than three names
    // for one width.
    const chatty = named(['plan'], 'claude-code-opus-4-1-max')
    expect(nodeWidthAt('details', chatty.steps)).toBeGreaterThan(
      nodeWidthAt('names', chatty.steps),
    )
  })

  it('emits, on every slot, exactly the width and height its tier claims', () => {
    // THE HALF A WIDTH FUNCTION CANNOT PROVE ON ITS OWN. `nodeWidthAt` can be
    // perfect and `.node-slot` can still be given `NODE_W`, which is what it
    // was given before this pass -- a `details` node 111px wider than its slot,
    // overlapping the sibling beside it. So the DOM is read back.
    const cases: ReadonlyArray<readonly [number, ZoomTier]> = [
      [STAGE_FITS, 'figures'],
      [6, 'details'],
    ]
    for (const [n, tier] of cases) {
      const { w, tasks } = wideStage(n)
      expect(autoTier(w.steps), `${n} steps should land at ${tier}`).toBe(tier)
      const { container, unmount } = card(w, tasks, true)
      const expectedW = nodeWidthAt(tier, w.steps)
      const slots = [...container.querySelectorAll<HTMLElement>('.node-slot')]
      expect(slots).toHaveLength(n + 2)
      for (const slot of slots) {
        expect(Number.parseFloat(slot.style.width), `${slot.textContent?.slice(0, 12)}`).toBe(
          expectedW,
        )
      }
      // `plan` is a root, so its height is the tier's own with no dependency
      // line added -- the figure `nodeHeightAt` sums from the rows the tier
      // draws, which is 224 with the four figures on it and 140 without.
      expect(Number.parseFloat(slotOf(container, 'plan').style.height)).toBe(
        nodeHeightAt(tier),
      )
      // And the whole canvas is inside the column, which is the point of all of
      // it. Non-vacuous: the same fixture at the full tier is not.
      const canvas = container.querySelector<HTMLElement>('.wf-canvas')!
      expect(Number.parseFloat(canvas.style.width)).toBeLessThanOrEqual(CANVAS_COLUMN)
      unmount()
    }
    // The non-vacuity, stated as arithmetic rather than as a fixture: six full
    // nodes do not fit, which is why the tier moved.
    expect(PAD * 2 + 6 * NODE_W + 5 * SIB_GAP).toBeGreaterThan(CANVAS_COLUMN)
  })

  it('puts the tier boundary exactly where the derived threshold puts it', () => {
    // THE BOUNDARY IS THE ARITHMETIC AND NOTHING ELSE. Six steps fit the
    // `details` tier and seven fit no tier, and both halves are computed here
    // from the same exports the implementation uses -- so moving NODE_W, the
    // state-word bound or SIB_GAP moves this case with them instead of leaving
    // a stale literal behind, which is exactly how `DEP_CHARS_PER_LINE` went
    // wrong once already.
    const six = wideStage(6)
    const seven = wideStage(7)
    const dw = nodeWidthAt('details', six.w.steps)
    const nw = nodeWidthAt('names', seven.w.steps)
    expect(PAD * 2 + 6 * dw + 5 * SIB_GAP).toBeLessThanOrEqual(CANVAS_COLUMN)
    expect(PAD * 2 + 7 * dw + 6 * SIB_GAP).toBeGreaterThan(CANVAS_COLUMN)
    expect(PAD * 2 + 7 * nw + 6 * SIB_GAP).toBeGreaterThan(CANVAS_COLUMN)
    expect(stageFitsAt(dw)).toBe(6)
    expect(stageFitsAt(nw)).toBeLessThan(7)

    // Six: drawn, at `details`.
    expect(autoTier(six.w.steps)).toBe('details')
    const a = card(six.w, six.tasks, true)
    expect(a.container.querySelector('.wf-band')).toBeNull()
    expect(a.container.querySelectorAll('.node')).toHaveLength(8)

    // Seven: no tier holds it, so the zoom STAYS AT `figures` and the band does
    // the work. Zooming out here would cost every node in the workflow its
    // figures and still collapse the stage -- paying twice for nothing.
    expect(autoTier(seven.w.steps)).toBe('figures')
    const b = card(seven.w, seven.tasks, true)
    expect(b.container.querySelector('.wf-band')).toBeTruthy()
    expect(b.container.querySelectorAll('.node')).toHaveLength(2)
    expect(b.container.querySelector('.wf-zoom .ctl-mark')).toBeNull()

    // And thirteen -- the measured run's widest stage -- is a band at every
    // tier, which is the claim stage collapsing exists for and which semantic
    // zoom does not replace.
    for (const tier of ['figures', 'details', 'names'] as const) {
      const { w, tasks } = wideStage(13)
      const c = card(w, tasks, true, tier)
      expect(c.container.querySelector('.wf-band'), tier).toBeTruthy()
      expect(c.container.querySelectorAll('.node'), tier).toHaveLength(2)
      c.unmount()
    }
  })

  it('drops the FIELD and never the value, and says on the canvas that it did', () => {
    // wideStage with no states: every fan step has no task, so its duration is
    // an ABSENCE -- the case that must not become a blank or a zero.
    const { w, tasks } = wideStage(5)

    const full = card(wideStage(STAGE_FITS).w, wideStage(STAGE_FITS).tasks, true)
    // Silent at the full tier. A mark saying "nothing is hidden" on every canvas
    // that fits is the noise §8.4 took off this screen twice.
    expect(full.container.querySelector('.wf-zoom .ctl-mark')).toBeNull()
    full.unmount()

    const details = card(w, tasks, true, 'details')
    const dNode = nodeNamed(details.container, 'scan-0')
    // THE DURATION IS STILL THERE AND STILL SAYS THE ABSENCE IN WORDS.
    expect(dNode.querySelector('.node-dur')!.textContent).toBe('not started')
    expect(dNode.querySelector('.node-meta')!.textContent).toBe('codex')
    // The figures are GONE FROM THE CARD rather than blank. An empty `.node-num`
    // would be the defect: a cell with no text where `not sampled` belongs says
    // the figure is fine.
    expect(dNode.querySelector('.node-nums')).toBeNull()
    expect(dNode.querySelectorAll('.node-num')).toHaveLength(0)
    const dMark = details.container.querySelector('.wf-zoom .ctl-mark')!
    expect(dMark.textContent).toBe('figures not drawn')
    expect(dMark.getAttribute('aria-label')).toContain(
      'decision about the zoom and not a fact about the steps',
    )
    details.unmount()

    const names = card(w, tasks, true, 'names')
    const nNode = nodeNamed(names.container, 'scan-0')
    expect(nNode.querySelector('.node-dur')).toBeNull()
    expect(nNode.querySelector('.node-meta')).toBeNull()
    expect(nNode.querySelector('.node-nums')).toBeNull()
    // The name and the state are in every tier.
    expect(nNode.querySelector('.node-id')!.textContent).toBe('scan-0')
    expect(nNode.querySelector('.node-state')!.textContent).toBe('not started')
    const nMark = names.container.querySelector('.wf-zoom .ctl-mark')!
    expect(nMark.textContent).toBe('profile, duration and figures not drawn')
    // NAMED, NOT COUNTED. "3 fields hidden" is a number a reader has to go and
    // check; these are the fields.
    expect(nMark.getAttribute('aria-label')).toContain('profile, duration and figures')
  })

  it('never hides a failed or cancelled step, at the smallest tier either', () => {
    const states: (TaskState | null)[] = ['FAILED', 'SUCCEEDED', 'SUCCEEDED', 'CANCELLED', null]
    const { w, tasks } = wideStage(5, states)
    const { container } = card(w, tasks, true, 'names')

    const failed = nodeNamed(container, 'scan-0')
    // THREE CHANNELS, NONE OF THEM COLOUR ALONE, at the tier that draws least:
    // the card's own accent class, the mark in its tone, and the word.
    expect(failed.className).toContain('bad')
    expect(failed.querySelector('.ctl-dot.is-bad')).toBeTruthy()
    expect(failed.querySelector('.node-state')!.textContent).toBe('failed')

    const cancelled = nodeNamed(container, 'scan-3')
    expect(cancelled.querySelector('.node-state')!.textContent).toBe('cancelled')

    // AND THE TIER REALLY IS THE SMALLEST ONE, or the assertions above are
    // passing at a zoom that happens to draw everything.
    expect(failed.querySelector('.node-meta')).toBeNull()
    expect(failed.querySelector('.node-dur')).toBeNull()
    expect(failed.querySelector('.node-nums')).toBeNull()
    expect(container.querySelector('.wf-zoom .ctl-mark')!.textContent).toBe(
      'profile, duration and figures not drawn',
    )
  })

  it('shrinks no type and scales no canvas, at any tier', () => {
    // THE HALF OF "FIT TO WIDTH" THIS IMPLEMENTATION REFUSES. The ux plan's
    // option 1 says the canvas scales; scaling shrinks type, `typescale.test.ts`
    // holds a 12px floor under six steps, and the owner has twice said this
    // console is hard to read. A node scaled to fit is texture, not a node -- so
    // the tiers drop fields and nothing on this canvas is ever transformed.
    for (const tier of ['figures', 'details', 'names'] as const) {
      const { w, tasks } = wideStage(5)
      const { container, unmount } = card(w, tasks, true, tier)
      const drawn = [...container.querySelectorAll<HTMLElement>('.wf-canvas, .node-slot, .node, .node *')]
      expect(drawn.length).toBeGreaterThan(10)
      for (const el of drawn) {
        expect(el.style.fontSize, `${tier}: ${el.className} carries an inline font size`).toBe('')
        expect(el.style.transform, `${tier}: ${el.className} is transformed`).toBe('')
        expect(el.style.getPropertyValue('zoom'), `${tier}: ${el.className} is zoomed`).toBe('')
      }
      unmount()
    }
  })

  it('offers the zoom as four real buttons, and putting a field back works', () => {
    const { w, tasks } = wideStage(6)
    const { container } = card(w, tasks, true)
    const seg = container.querySelector('.wf-zoom-seg')!
    const buttons = () => [...seg.querySelectorAll('button')]

    // KEYBOARD REACHABLE BY CONSTRUCTION: four real `<button type="button">`s in
    // the one segmented primitive this console uses, so the minimap is not the
    // only way to do anything. Built from `ZOOM_TIERS`, so a tier added to
    // `dag.ts` cannot arrive without a way to choose it.
    expect(buttons().map((b) => b.textContent)).toEqual(['Auto', 'Figures', 'Details', 'Names'])
    for (const b of buttons()) {
      expect(b.getAttribute('type')).toBe('button')
      expect(b.hasAttribute('disabled')).toBe(false)
      // Each one says what it does, in fields rather than in sizes.
      expect(b.getAttribute('title')!.length).toBeGreaterThan(30)
    }
    expect(
      buttons()
        .filter((b) => b.getAttribute('aria-pressed') === 'true')
        .map((b) => b.textContent),
    ).toEqual(['Auto'])
    // `Auto` names what it resolved to, so the automatic choice is not a black
    // box a reader has to infer from the cards.
    expect(buttons()[0]!.getAttribute('title')).toContain('Details')

    // ASKING FOR THE FIGURES BACK GETS THEM, and collapsing backstops the ask:
    // six full nodes do not fit, so the stage becomes a band again and the two
    // nodes that remain carry their figures.
    expect(container.querySelectorAll('.node-nums')).toHaveLength(0)
    fireEvent.click(buttons()[1]!)
    expect(
      buttons()
        .filter((b) => b.getAttribute('aria-pressed') === 'true')
        .map((b) => b.textContent),
    ).toEqual(['Figures'])
    expect(container.querySelector('.wf-band')).toBeTruthy()
    expect(container.querySelectorAll('.node-nums')).toHaveLength(2)
    expect(container.querySelector('.wf-zoom .ctl-mark')).toBeNull()
  })

  it('maps the canvas only when the canvas does not fit, and marks what is off it', () => {
    const states = Array(13).fill('SUCCEEDED') as (TaskState | null)[]
    states[6] = 'FAILED'
    const { w, tasks } = wideStage(13, states)
    const { container } = card(w, tasks, true)

    // COLLAPSED, THE CANVAS FITS, so there is nothing to orient in and no map.
    // A minimap of a canvas you can see all of is chrome for its own sake.
    expect(container.querySelector('.wf-minimap')).toBeNull()

    // Opened, it is 3,671px against a 1,054px column, and the map appears.
    expect(openEveryBand(container)).toBe(1)
    const map = container.querySelector<HTMLElement>('.wf-minimap')!
    expect(map).toBeTruthy()
    // INFORMATION, NOT A CONTROL. A `<button>` whose only meaningful activation
    // is "the x coordinate you clicked at" is a button a keyboard cannot use,
    // and announcing one would promise a route that is not there. The route is
    // the wrapper's own scroll and the tab order through the nodes.
    expect(map.getAttribute('role')).toBe('img')
    expect(map.tagName).not.toBe('BUTTON')
    expect(map.getAttribute('aria-label')).toContain('3671 pixels wide')
    // One rectangle per drawn node, plus the band, so the map is of the canvas
    // rather than of the viewport.
    expect(map.querySelectorAll('.wf-mini-node')).toHaveLength(15)
    // A FAILURE IS FINDABLE ON THE MAP TOO, including inside the stage the map
    // exists because you cannot see all of.
    expect(map.querySelector('.wf-mini-node.is-bad')).toBeTruthy()
    expect(map.querySelector('.wf-mini-band.is-bad')).toBeTruthy()

    // THE jsdom LIMIT, ASSERTED RATHER THAN PAPERED OVER. There is no layout
    // engine here, so `clientWidth` is 0 and the viewport rectangle is
    // deliberately NOT drawn -- a rectangle over the whole canvas would claim
    // everything is on screen, which is the same class of lie as a blank cell
    // where an unmeasured figure belongs. The accessible name says so too.
    expect(map.querySelector('.wf-mini-view')).toBeNull()
    expect(map.getAttribute('aria-label')).toContain('has not been measured')
  })
})

// ---------------------------------------------------------------------------
// 4. Spend
// ---------------------------------------------------------------------------

function spending(costs: (number | null)[]): { w: Workflow; tasks: Map<string, Task> } {
  const steps = costs.map((_, i) => step(`s${i}`, i === 0 ? [] : ['s0'], { task_id: `t${i}` }))
  const tasks = new Map<string, Task>()
  costs.forEach((c, i) => {
    tasks.set(
      `t${i}`,
      task(`t${i}`, 'SUCCEEDED', {
        started_at: iso(-60_000),
        completed_at: iso(-30_000),
        result_summary: c === null ? {} : { runner: { usage: { total_cost_usd: c } } },
      }),
    )
  })
  return { w: workflow('wf_spend', steps), tasks }
}

describe('what a workflow has cost', () => {
  it('says "not reported" when nothing reported one, and prints no zero', () => {
    const { w, tasks } = spending([null, null])
    const { container } = card(w, tasks)
    const cell = container.querySelector('.wf-spend')!
    expect(cell.textContent).toBe('not reported')
    expect(cell.textContent, 'an unreported cost rendered a figure').not.toMatch(/\d/)
    expect(cell.className).toContain('absent')
    expect(cell.getAttribute('title')).toContain('not $0.00')
  })

  it('prints a REPORTED zero as a number, because that one was measured', () => {
    const { w, tasks } = spending([0, 0])
    const { container } = card(w, tasks)
    const cell = container.querySelector('.wf-spend')!
    expect(cell.textContent).toContain('$0.0000')
    expect(cell.className).not.toContain('absent')
  })

  it('never shows a partial total without its coverage', () => {
    const { w, tasks } = spending([0.0642, null, null])
    const { container } = card(w, tasks)
    const cell = container.querySelector('.wf-spend')!
    expect(cell.textContent).toContain('$0.0642')
    expect(cell.querySelector('.wf-spend-cov')!.textContent).toBe('1/3')
    expect(cell.getAttribute('title')).toContain('floor rather than the total')
  })
})

// ---------------------------------------------------------------------------
// 5. The stylesheet, resolved -- not grepped
// ---------------------------------------------------------------------------

describe('the shipped stylesheet', () => {
  pinTheClock()

  it('draws an absent figure differently from a measured one', () => {
    // The em-dash rule as a RENDERED property. If the two treatments resolve
    // to the same computed style, the row says "not reported" in exactly the
    // voice it says "$0.0642" -- and the distinction this product is built on
    // survives only in the words.
    const style = withStyles()
    const absent = card(spending([null, null]).w, spending([null, null]).tasks)
    const absentCell = absent.container.querySelector('.wf-spend')!
    const measured = card(spending([0, 0]).w, spending([0, 0]).tasks)
    const measuredCell = measured.container.querySelector('.wf-spend')!

    expect(getComputedStyle(absentCell).fontStyle).toBe('italic')
    expect(getComputedStyle(measuredCell).fontStyle).toBe('normal')
    expect(getComputedStyle(absentCell).color).not.toBe(getComputedStyle(measuredCell).color)
    style.remove()
  })

  it('does the same for a step with no duration', () => {
    const style = withStyles()
    const { w, tasks } = timings()
    // `details`, where the duration line is drawn for every kind (see `how
    // long a step has taken`).
    const { container } = card(w, tasks, true, 'details')
    const none = nodeNamed(container, 'publish').querySelector('.node-dur')!
    const ran = nodeNamed(container, 'done').querySelector('.node-dur')!
    expect(getComputedStyle(none).fontStyle).toBe('italic')
    expect(getComputedStyle(ran).fontStyle).toBe('normal')
    style.remove()
  })

  it('leaves the collapsed row un-uppercased inside a heading that uppercases', () => {
    // B17's neighbourhood. The bar is still an <h2> so the workflow keeps its
    // place in the document outline; the button inside it turns the heading's
    // uppercase back off, and the `.id` rule keeps the identifier lowercase
    // whichever way that goes.
    const style = withStyles()
    const { container } = card(chain())
    const h2 = container.querySelector('h2')!
    const bar = container.querySelector('.wf-bar')!
    // NOT asserted: that the h2 itself uppercases. It did when this was
    // written, and the type-scale work then removed the small caps from every
    // panel title deliberately -- "a panel title is --t-title in --text, not
    // small caps". Pinning the uppercase here would pin a treatment that was
    // taken out on purpose, and would go red the moment it was taken out
    // again. What must hold either way is that the bar and the identifier are
    // not uppercased, whatever the heading above them does.
    expect(getComputedStyle(h2).textTransform).not.toBe('uppercase')
    expect(getComputedStyle(bar).textTransform).toBe('none')
    expect(getComputedStyle(container.querySelector('.id')!).textTransform).toBe('none')
    style.remove()
  })
})

// ---------------------------------------------------------------------------
// 6. The shape function itself, over the cases the board will meet
// ---------------------------------------------------------------------------

describe('shapeOf', () => {
  it('classifies the shapes a reader needs told apart', () => {
    expect(shapeOf(chain().steps).kind).toBe('chain')
    expect(shapeOf(fanOut().steps).kind).toBe('diamond')
    expect(shapeOf([step('only', [])]).kind).toBe('single')
    expect(shapeOf([]).kind).toBe('empty')
    expect(
      shapeOf([step('a', []), step('b', []), step('c', ['a', 'b'])]).kind,
    ).toBe('fan-in')
    expect(shapeOf([step('a', []), step('b', ['a']), step('c', ['a'])]).kind).toBe('fan-out')
  })

  it('survives a dependency cycle rather than hanging on it', () => {
    // The scheduler rejects a cycle at submission, so this should be
    // impossible -- but a board that spins forever on malformed data is worse
    // than one that draws it flat.
    const s = shapeOf([step('a', ['b']), step('b', ['a'])])
    expect(s.steps).toBe(2)
    expect(s.widths.length).toBeGreaterThan(0)
  })
})

// ---------------------------------------------------------------------------
// 7. The visual QA pass of 2026-09-25 (epics #82 and #83)
// ---------------------------------------------------------------------------
//
// Each case below is one box from that pass, measured against the live board
// while a 30-step workflow ran and after it ended. Every one of them was
// committed RED against the code it describes before the fix landed, so each
// is shown to catch the defect it names rather than merely to pass.

/** A rollup with these counts over `total` steps, derived and complete. */
function ended(
  workflow_id: string,
  total: number,
  counts: Record<string, number>,
  over: Partial<Workflow> = {},
): Workflow {
  return workflow(
    workflow_id,
    Array.from({ length: total }, (_, i) => step(`s-${i}`, [])),
    {
      state: 'FAILED',
      stored_state: 'FAILED',
      rollup: {
        state: 'FAILED',
        complete: true,
        reason: 'a_step_failed',
        counts,
        unreadable_steps: [],
        unstarted_steps: [],
        steps_read: total,
      },
      ...over,
    },
  )
}

describe('the QA pass: the collapsed row', () => {
  pinTheClock()

  it('WF-1: accounts for every way a step ends, not only success and failure', () => {
    // The measured row: `9/30 done · 4 failed` over a workflow where the other
    // seventeen had been cancelled or dead-lettered -- seventeen steps the line
    // whose job is to account for thirty simply did not mention.
    const w = ended('wf_ended', 30, { SUCCEEDED: 9, FAILED: 4, CANCELLED: 15, DEAD_LETTERED: 2 })
    const { container } = card(w)
    const text = container.querySelector('.wf-progress-text')!.textContent!
    expect(text).toContain('9/30 done')
    expect(text).toContain('4 failed')
    expect(text, 'the cancelled steps are missing from the census').toContain('15 cancelled')
    expect(text, 'the dead-lettered steps are missing from the census').toContain('2 dead_lettered')
    // The accessible name accounts for all thirty as well.
    const why = container.querySelector('.wf-meter')!.getAttribute('aria-label')!
    expect(why).toContain('15 cancelled')
    expect(why).toContain('2 dead_lettered')
    // And a count that is zero is not printed as a clause.
    const clean = card(ended('wf_clean', 3, { SUCCEEDED: 3 }))
    expect(clean.container.querySelector('.wf-progress-text')!.textContent).toBe('3/3 done')
  })

  it('WF-15: says a cancel was requested only while the workflow has not ended', () => {
    const flags = (w: Workflow) => card(w).container.querySelector('.wf-flags')!.textContent
    // Finished cancelled a day ago; the flag is never cleared, and the tag rode
    // on the row as if something were still pending.
    expect(
      flags(ended('wf_over', 3, { CANCELLED: 3 }, { cancel_requested: true, rollup: {
        state: 'CANCELLED', complete: true, reason: 'cancelled', counts: { CANCELLED: 3 },
        unreadable_steps: [], unstarted_steps: [], steps_read: 3,
      } })),
      'a finished workflow still reads "cancel requested"',
    ).toBe('')
    // Still running: the request is real and still in flight.
    expect(flags(workflow('wf_live', chain().steps, { cancel_requested: true }))).toBe('cancel requested')
    // A rollup that could not read every step has not shown it is over, so the
    // request stays said.
    expect(
      flags(workflow('wf_unread', chain().steps, { cancel_requested: true, rollup: {
        state: 'UNKNOWN', complete: false, reason: 'step_read_budget_exhausted', counts: {},
        unreadable_steps: ['build'], unstarted_steps: [], steps_read: 2,
      } })),
    ).toBe('cancel requested')
  })

  it('WF-16: folds whole runner chips into the count rather than letting the column cut one', () => {
    const steps = [
      ...Array.from({ length: 20 }, (_, i) => step(`cc-${i}`, [], { runner_profile: 'claude-code' })),
      ...Array.from({ length: 6 }, (_, i) => step(`br-${i}`, [], { runner_profile: 'browser' })),
      ...Array.from({ length: 4 }, (_, i) => step(`mk-${i}`, [], { runner_profile: 'mock' })),
    ]
    const chips = (room: number) => {
      // THE COLUMN'S WIDTH, stubbed, because jsdom has no layout: everything
      // else keeps answering 0, exactly as it does for every other test here.
      const spy = vi.spyOn(Element.prototype, 'clientWidth', 'get').mockImplementation(function (this: Element) {
        return this.classList.contains('wf-mix') ? room : 0
      })
      const { container, unmount } = card(workflow('wf_mix', steps))
      const mix = container.querySelector('.wf-mix')!
      const named = [...mix.querySelectorAll('.wf-chip:not(.more)')].map((c) => c.firstChild?.textContent)
      const more = mix.querySelector('.wf-chip.more')?.textContent ?? null
      const title = mix.getAttribute('title')
      unmount()
      spy.mockRestore()
      return { named, more, title }
    }
    // 160px: `claude-code ×20` and `+2` fit; a second whole chip does not. The
    // old row drew two chips and `+1` regardless, and the column cut the
    // second mid-word.
    const narrow = chips(160)
    expect(narrow.named, 'a chip the column cannot hold was drawn anyway').toEqual(['claude-code'])
    expect(narrow.more).toBe('+2')
    // Room for two: two named and the third counted, which is the row's promise.
    expect(chips(400)).toMatchObject({ named: ['claude-code', 'browser'], more: '+1' })
    // Every profile stays reachable whatever was folded.
    expect(narrow.title).toBe('Runner profiles: claude-code ×20, browser ×6, mock ×4')
  })
})

describe('foldMix', () => {
  const mix = profileMix([
    ...Array.from({ length: 20 }, (_, i) => step(`cc-${i}`, [], { runner_profile: 'claude-code' })),
    ...Array.from({ length: 6 }, (_, i) => step(`br-${i}`, [], { runner_profile: 'browser' })),
    ...Array.from({ length: 4 }, (_, i) => step(`mk-${i}`, [], { runner_profile: 'mock' })),
  ])

  it('keeps the plain promise, two named and the rest counted, when the column was never measured', () => {
    const { shown, rest } = foldMix(mix, null)
    expect(shown.map((m) => m.profile)).toEqual(['claude-code', 'browser'])
    expect(rest).toBe(1)
  })

  it('fits whole chips and an exact count at every width, and never names more than two', () => {
    // A SWEEP, with its floor stated: every width from nothing to far more than
    // enough, so "fits" is shown across the boundaries rather than at the one
    // width the case above happens to use.
    const widths = Array.from({ length: 81 }, (_, i) => i * 5)
    let visited = 0
    for (const room of widths) {
      const { shown, rest } = foldMix(mix, room)
      visited += 1
      expect(shown.length + rest, `${room}px lost a profile`).toBe(mix.length)
      expect(shown.length, `${room}px named more than two`).toBeLessThanOrEqual(2)
      const drawn = [...shown.map(mixChipW), ...(rest > 0 ? [moreChipW(rest)] : [])]
      const w = drawn.reduce((t, x) => t + x, 0) + Math.max(0, drawn.length - 1) * 4
      // Only the count alone is allowed to overflow, and only when nothing fits.
      if (shown.length > 0) expect(w, `${room}px drew chips wider than the column`).toBeLessThanOrEqual(room)
    }
    expect(visited).toBe(81)
    // And more room never names FEWER.
    const named = widths.map((room) => foldMix(mix, room).shown.length)
    for (let i = 1; i < named.length; i++) expect(named[i]!).toBeGreaterThanOrEqual(named[i - 1]!)
  })
})

describe('the QA pass: the canvas', () => {
  pinTheClock()

  it('WF-2: does not paint a stage of cancelled and succeeded steps as a failure', () => {
    const states = Array(13).fill('SUCCEEDED') as (TaskState | null)[]
    states[3] = 'CANCELLED'
    states[9] = 'CANCELLED'
    const { w, tasks } = wideStage(13, states)
    const { container } = card(w, tasks, true)
    const band = container.querySelector('.wf-band')!
    // The cancellation is still counted, first, in its own tone ...
    expect(bandCounts(band)[0]).toBe('2 cancelled')
    expect(band.getAttribute('aria-label')).toContain('0 failed and 2 cancelled.')
    // ... and it does not take the failure rule and tint.
    expect(band.className, 'a stage somebody cancelled is painted as broken').not.toContain('has-failure')
    // Nor does its band on the map.
    expect(openEveryBand(container)).toBe(1)
    const map = container.querySelector('.wf-minimap')!
    expect(map.querySelector('.wf-mini-band')).toBeTruthy()
    expect(map.querySelector('.wf-mini-band.is-bad'), 'the map paints the cancelled stage as broken').toBeNull()
  })

  it('WF-3: paints every edge halo before any stroke, so no halo erases an arrowhead', () => {
    // SVG paints in document order. One group of halo-then-stroke per edge put
    // a later edge's halo over an earlier edge's arrowhead wherever two
    // converged -- the five arrows into `report` erased by their siblings.
    const { container } = card(fanOut(), new Map(), true)
    const paint = [...container.querySelectorAll('svg.wf-edges .wf-edge-halo, svg.wf-edges .wf-edge')]
    const isHalo = paint.map((p) => p.classList.contains('wf-edge-halo'))
    expect(isHalo.filter(Boolean)).toHaveLength(10)
    expect(isHalo.filter((h) => !h)).toHaveLength(10)
    expect(isHalo.lastIndexOf(true), 'a halo is painted after a stroke').toBeLessThan(isHalo.indexOf(false))
    // A halo is clearance, not a mark: one mark per dependency is unchanged.
    for (const halo of container.querySelectorAll('.wf-edge-halo')) {
      expect(halo.closest('[data-edge]'), 'a halo is inside an edge mark').toBeNull()
    }
    expect(container.querySelectorAll('[data-edge]')).toHaveLength(10)
  })

  it('WF-20: prints a run once at the Figures tier, and keeps a wait beside the state', () => {
    const { w, tasks } = timings()
    const { container } = card(w, tasks, true, 'figures')
    const ran = (name: string) =>
      [...nodeNamed(container, name).querySelectorAll('.node-num')]
        .find((n) => n.querySelector('dt')?.textContent === 'ran')
        ?.querySelector('dd')?.textContent
    // `ran 1m 8s` on the state line over `1m 8s` in the figures was the same
    // duration twice on one card. The figure keeps it.
    for (const name of ['done', 'live', 'publish']) {
      expect(nodeNamed(container, name).querySelector('.node-dur'), `${name} prints its duration twice`).toBeNull()
    }
    expect(ran('done')).toBe('1m 8s')
    expect(ran('live')).toBe('1m 8s so far')
    expect(ran('publish')).toBe('not started')
    // A WAIT is not a run, and the `ran` figure cannot say it, so it stays.
    expect(nodeNamed(container, 'waiting').querySelector('.node-dur')!.textContent).toBe('queued 2m 33s')
    expect(nodeNamed(container, 'held').querySelector('.node-dur')!.textContent).toBe('parked 7m 11s')
  })
})

describe('the QA pass: the table', () => {
  pinTheClock()

  it('AG-15: draws an attempt count past its ceiling as past it, and says by how much', () => {
    const w = workflow('wf_over', [
      step('retried', [], { task_id: 't_over' }),
      step('spent', [], { task_id: 't_spent' }),
      step('fine', [], { task_id: 't_fine' }),
    ])
    const tasks = new Map<string, Task>([
      ['t_over', task('t_over', 'FAILED', { attempt_count: 4, max_attempts: 3, started_at: iso(-90_000), completed_at: iso(-30_000) })],
      ['t_spent', task('t_spent', 'FAILED', { attempt_count: 3, max_attempts: 3, started_at: iso(-90_000), completed_at: iso(-30_000) })],
      ['t_fine', task('t_fine', 'RUNNING', { attempt_count: 1, max_attempts: 3, started_at: iso(-90_000) })],
    ])
    const { container } = card(w, tasks, true)
    fireEvent.click(within(container.querySelector<HTMLElement>('.wf-viewbar')!).getByText('Table'))
    const attempts = (id: string) =>
      container.querySelector(`.wf-table tr[data-step="${id}"] td[data-col="attempts"] .wf-cell`)!
    expect(attempts('retried').textContent).toBe('4 of 3')
    expect(attempts('retried').className, '4 of 3 is drawn like 1 of 3').toContain('is-over')
    expect(attempts('retried').getAttribute('aria-label')).toContain('1 over the ceiling')
    // STRICTLY past it. Using every attempt it was allowed is how a failing
    // step ordinarily ends, not a count the ceiling failed to hold.
    expect(attempts('spent').className).not.toContain('is-over')
    expect(attempts('fine').className).not.toContain('is-over')
  })
})
