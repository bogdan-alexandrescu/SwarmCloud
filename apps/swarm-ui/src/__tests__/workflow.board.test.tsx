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
//  1. The list row tells a fan-out from a chain (moved to workflow.v2.test.tsx
//     with the rebrand, 2026-10-01, when the board's own row was removed).
//  2. A step that HAS NOT STARTED shows an absence, never `0s`. And the other
//     half of the same rule, which is the half that gets lost while fixing the
//     first: a step that really did take zero seconds shows the digit `0`.
//  3. An expanded node still carries the NAME, the STATUS and the RUNNER
//     PROFILE -- the three facts it has always carried.
//  4. Spend that nobody reported reads "not reported", never `$0.00`; a
//     reported zero reads as a number (on the list row: workflow.v2.test.tsx).

import STYLES from '../styles.css?raw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import { WorkflowCard, WorkflowsScreen } from '../Workflows'
import {
  CANVAS_COLUMN,
  CANVAS_HEIGHT,
  LEVEL_GAP,
  MONO_ADVANCE_EM,
  NODE_CHROME_W,
  NODE_W,
  PAD,
  PANEL_COLUMN,
  SIB_GAP,
  STAGE_FITS,
  autoTier,
  depUnits,
  edgeKinds,
  edgePath,
  foldMix,
  heightOf,
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
  type DagLayout,
  type ZoomTier,
} from '../dag'
import type { Task, TaskDispatch, TaskState, Workflow, WorkflowStep } from '../types'
import { cascade, splitTop, type CascadeEnv } from './cssgate'
import { colour, resolveSheet, resolveVars, tokenTables, type RGBA } from './spaceprobe'

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
  cardKey = 0,
  zoom = 'auto',
}: {
  workflow: Workflow
  taskById: Map<string, Task> | null
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
      zoom={choice}
      onZoom={(_id, next) => setChoice(next)}
      // `ready` with a null usage is "the attempt read landed and this board
      // was outside its sample", which is the state these cases are about --
      // not `reading`, which would put every figure behind a placeholder and
      // make the assertions below pass for the wrong reason.
      usage={{ kind: 'ready', usage: null }}
      openStages={stages}
      onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
      reload={noop}
    />
  )
}

function card(
  w: Workflow,
  taskById: Map<string, Task> | null = new Map(),
  zoom: 'auto' | ZoomTier = 'auto',
) {
  return render(
    <CardHarness workflow={w} taskById={taskById} zoom={zoom} />,
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
    const band = root.querySelector<HTMLButtonElement>('.wf-band-toggle[aria-expanded="false"]')
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
// 1. The list lands without a canvas
// ---------------------------------------------------------------------------

describe('the list', () => {
  it('V2: /workflows lands on the full-width list, one row per workflow and no canvas', async () => {
    // Workflows V2 (owner's pick, 2026-10-01): the console's /workflows is the
    // list; a workflow's graph is on its own page.
    render(<WorkflowsScreen />)
    await screen.findByText('wf_audit_01', {}, { timeout: 4000 })
    const rows = document.querySelectorAll('.wfl-table tbody tr')
    expect(rows.length).toBeGreaterThan(0)
    expect(document.querySelector('.wf-canvas')).toBeNull()
    // Each row links to that workflow's page.
    const row = document.querySelector('.wfl-table tr[data-workflow="wf_audit_01"]')
    expect(row, 'no row for wf_audit_01').toBeTruthy()
    expect(row!.querySelector('.wfl-name > a')?.getAttribute('href')).toBe('/workflows/wf_audit_01')
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
 * layout's inline `left`/`top`. The node became the link, and a `<button>` may
 * not live inside an `<a>` -- so the stop control became its sibling. Since
 * WF-7 the node is itself a button (it picks the step into the inspector), and
 * a button may not live inside a button either, so the shape holds: `.node-slot`
 * is the parent that holds both and is what `layoutOf` positions.
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
    const { container } = card(w, tasks, 'details')
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
    const { container } = card(w, tasks, 'details')
    const dur = nodeNamed(container, 'instant').querySelector('.node-dur')!
    expect(dur.textContent).toBe('ran 0s')
    expect(dur.className).toContain('is-ran')
  })

  it('keeps waiting, running, parked and finished as four different things', () => {
    const { w, tasks } = timings()
    const { container } = card(w, tasks, 'details')
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
    const { container } = card(w, new Map(), 'details')
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
    const { container } = card({ ...w, steps }, tasks)
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
    const { container } = card(fanOut(), new Map())
    // On the canvas: the key's three samples (#108) are edges too, and drawn
    // with the same class on purpose.
    const edges = container.querySelectorAll('.wf-edges .wf-edge')
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
    const { container } = card(w, tasks)
    expect(container.querySelector('.wf-band')).toBeTruthy()
    expect(container.querySelectorAll('.node')).toHaveLength(2)
    // 13 into `report` and 13 out of `plan`, none of which has a node at
    // either end drawn as a card.
    expect(container.querySelectorAll('.wf-edges .wf-edge')).toHaveLength(26)
  })

  /**
   * RE-POINTED AGAIN, BY THE OWNER'S DECISION ON WF-7 (epic #83). The node was
   * the link: one anchor to `#work/task/<id>`, so clicking a step on the Graph
   * left the workflow for the Work › Agents drawer, while the Timeline and the
   * Table picked the same step into the inspector. redesign-v2 §2.3 says
   * "Selecting any node in any mode fills the inspector. Nothing navigates."
   * The decision: the node is a SELECTION, it fills the inspector in place,
   * and the inspector carries `open agent →` to the drawer.
   *
   * WHAT IS STILL PINNED from the case this replaces: the address is the one
   * the rest of the console uses for the task, there is exactly one way to it,
   * and a step with no task offers no dead link.
   */
  it('WF-7: fills the inspector in place when a node is clicked, and the inspector links to the run', async () => {
    const w = fanOut()
    const steps = w.steps.map((s, i) => (i === 1 ? { ...s, task_id: 'task_scan_a' } : s))
    const tasks = new Map([['task_scan_a', task('task_scan_a', 'RUNNING')]])
    // A REAL STAGE STORE, NOT A FROZEN ONE. Picking `scan-a` opens the
    // inspector beside the canvas (#330 item 5), which narrows the column the
    // five scans have to fit; beside that narrower column even Names cannot
    // draw all five on one row, so the stage the pick landed in folds into a
    // band. WF-10's own effect in `WorkflowCard` opens it straight back up by
    // calling `onToggleStage` -- but only if something is listening. A frozen
    // `openStages={{}}` with a no-op handler can never observe that call, so
    // the band would stay closed and `scan-a` would vanish from the canvas
    // the moment it was picked -- not what this case is about. `card()`'s own
    // `CardHarness` is exactly this store; it is inlined here because this
    // case also needs `loadAttempts`, which `card()` does not take.
    function Harness() {
      const [stages, setStages] = useState<Record<string, boolean>>({})
      return (
        <WorkflowCard
          workflow={{ ...w, steps }}
          taskById={tasks}
          usage={{ kind: 'ready', usage: null }}
          openStages={stages}
          onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
          loadAttempts={async () => ({ status: 'empty', fetchedAt: T0 })}
          reload={noop}
        />
      )
    }
    const { container } = render(<Harness />)
    // Drawn at the `details` tier rather than collapsed -- see the first case in
    // this block.
    expect(container.querySelectorAll('.node')).toHaveLength(7)
    const before = window.location.href
    const node = nodeNamed(container, 'scan-a')
    // A CONTROL, NOT A LINK: nothing on the card navigates.
    expect(node.tagName, 'the node is still a link that leaves the workflow').toBe('BUTTON')
    expect(node.getAttribute('type')).toBe('button')
    expect(node.hasAttribute('href')).toBe(false)
    expect(node.querySelector('a')).toBeNull()
    expect(node.getAttribute('aria-pressed')).toBe('false')
    expect(container.querySelector('.wf-inspect')).toBeNull()

    fireEvent.click(node)
    expect(window.location.href, 'clicking a node navigated').toBe(before)
    expect(nodeNamed(container, 'scan-a').getAttribute('aria-pressed')).toBe('true')
    const inspector = container.querySelector<HTMLElement>('.wf-inspect')
    expect(inspector, 'clicking a node did not fill the inspector').toBeTruthy()
    expect(inspector!.querySelector('.wf-inspect-id')!.textContent).toBe('scan-a')
    // THE ONE WAY TO THE RUN, at the address the rest of the console uses.
    const open = inspector!.querySelector<HTMLAnchorElement>('a.wf-inspect-run')
    expect(open, 'the inspector carries no link to the run').toBeTruthy()
    expect(open!.textContent).toBe('Open agent →')
    expect(open!.getAttribute('href')).toBe('#work/task/task_scan_a')
    // Let the attempt read the inspector started settle inside the test.
    await waitFor(() =>
      expect(inspector!.querySelector('[data-scrub="attempt"]')!.textContent).not.toContain('reading'),
    )

    // A step with no task is pickable too -- it has facts to show -- and its
    // inspector offers nothing to open, so there is no dead link.
    fireEvent.click(nodeNamed(container, 'report'))
    const unreached = container.querySelector<HTMLElement>('.wf-inspect')!
    expect(unreached.querySelector('.wf-inspect-id')!.textContent).toBe('report')
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
    const { container } = card(fanOut(), new Map())
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

/**
 * `wideStage` without its joining `report` step: `plan` and the fan, TWO
 * levels. Figures cards are 246px tall, so three levels of them are 870px --
 * taller than `CANVAS_HEIGHT`, and `autoTier` zooms a three-level graph out
 * for its height (#330 item 4). A case about what the full tier's WIDTH holds
 * is asked on two levels, which fit the height at every tier.
 */
function fanOnly(n: number): { w: Workflow; tasks: Map<string, Task> } {
  const { w, tasks } = wideStage(n)
  return { w: workflow('wf_wide', w.steps.filter((s) => s.step_id !== 'report')), tasks }
}

/** Every bucket the band drew, in the order it drew them. */
function bandCounts(band: Element): string[] {
  return [...band.querySelectorAll('.wf-band-count')].map((c) => c.textContent ?? '')
}

describe('a stage too wide to draw', () => {
  pinTheClock()

  it('never draws a stage wider than the column the canvas has', () => {
    const { w, tasks } = wideStage(13)
    const { container } = card(w, tasks)
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
    const { container } = card(w, tasks)
    const band = container.querySelector('.wf-band')!

    expect(band.querySelector('.wf-band-n')!.textContent).toBe('13 steps')
    expect(bandCounts(band)).toEqual(['8 running', '3 succeeded', '2 not started'])
    // The census the band could not fit is still reachable without a mouse.
    expect(band.querySelector('.wf-band-toggle')!.getAttribute('aria-label')).toContain(
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
    const { container } = card(w, tasks)
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
    // And not by colour alone: the brand's failed mark (the solid diamond),
    // in the failure hue (marks.tsx; rebrand 2026-10-01).
    expect(band.querySelector('.wf-band-count.is-bad [data-mark="failed"][data-hue="bad"]')).toBeTruthy()
    expect(band.querySelector('.wf-band-toggle')!.getAttribute('aria-label')).toContain('1 failed and 1 cancelled.')

    // THE MODIFIER MEANS SOMETHING ONLY IF A CLEAN STAGE DOES NOT CARRY IT.
    const clean = wideStage(13, Array(13).fill('SUCCEEDED') as TaskState[])
    const b = card(clean.w, clean.tasks)
    const cleanBand = b.container.querySelector('.wf-band')!
    expect(cleanBand.className).not.toContain('has-failure')
    expect(cleanBand.querySelector('.wf-band-toggle')!.getAttribute('aria-label')).toContain(
      'No step in this stage has failed or been cancelled.',
    )
  })

  it('refuses to call a stage clean when its states were not read', () => {
    // Every fan step carries a task id and the task read returned nothing for
    // any of them -- the partial-read case this whole screen is built around.
    const { w } = wideStage(13, Array(13).fill('SUCCEEDED') as TaskState[])
    const { container } = card(w, new Map())
    const band = container.querySelector('.wf-band')!
    const label = band.querySelector('.wf-band-toggle')!.getAttribute('aria-label')!

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
    const { container } = card(w, tasks)
    // THE DISCLOSURE IS THE BAND'S FIRST ROW since the wide-stage pick: the
    // band holds count and chip buttons of its own, and a button may not hold
    // a button (wide-workflows.html A).
    const band = () => container.querySelector<HTMLButtonElement>('.wf-band > .wf-band-toggle')!

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
      <CardHarness workflow={w} taskById={tasks} cardKey={0} />,
    )
    fireEvent.click(container.querySelector<HTMLButtonElement>('.wf-band-toggle')!)
    expect(container.querySelectorAll('.node')).toHaveLength(15)

    // WHAT `reload()` DOES TO THE REAL BOARD. `WorkflowsScreen` bumps a key and
    // `Screen` remounts, taking everything held inside it with it -- which is
    // why `open` and the stage store both live ABOVE that key. Changing the
    // card's own key here remounts exactly the part that remounts in
    // production. A `WorkflowCard` that had quietly taken the expansion into a
    // `useState` of its own would close the stage here and nowhere else.
    rerender(<CardHarness workflow={w} taskById={tasks} cardKey={1} />)
    expect(container.querySelector('.wf-band-toggle')!.getAttribute('aria-expanded')).toBe('true')
    expect(container.querySelectorAll('.node')).toHaveLength(15)
  })

  it('leaves a stage that fits the FULL tier alone, and keeps every field on it', () => {
    const fits = fanOnly(STAGE_FITS)
    const a = card(fits.w, fits.tasks)
    expect(a.container.querySelector('.wf-band')).toBeNull()
    expect(a.container.querySelectorAll('.node')).toHaveLength(STAGE_FITS + 1)
    // Nothing was traded for it: `STAGE_FITS` is by definition what the full
    // tier holds, so the figures are still on every card.
    expect(autoTier(fits.w.steps)).toBe('figures')
    expect(a.container.querySelectorAll('.node-nums')).toHaveLength(STAGE_FITS + 1)
    // THE SAME STAGE UNDER A THIRD LEVEL IS ZOOMED OUT FOR ITS HEIGHT, not its
    // width (#330 item 4): three levels of Figures cards do not fit one screen.
    const deep = wideStage(STAGE_FITS)
    expect(layoutOf(deep.w.steps, undefined, 'figures').height).toBeGreaterThan(CANVAS_HEIGHT)
    expect(autoTier(deep.w.steps)).toBe('details')

    // ONE MORE STEP AND THE FULL TIER DOES NOT HOLD IT -- and this is where
    // semantic zoom replaced collapsing. It used to be a band and two nodes.
    // The four steps are now drawn at the tier that holds four, which is what
    // `nodeWidthAt` and `stageFitsAt` say it is rather than what a fixture
    // remembers.
    const over = wideStage(STAGE_FITS + 1)
    const b = card(over.w, over.tasks)
    expect(autoTier(over.w.steps)).toBe('details')
    expect(b.container.querySelector('.wf-band')).toBeNull()
    expect(b.container.querySelectorAll('.node')).toHaveLength(STAGE_FITS + 3)
    // ...and the trade is stated on the canvas rather than left to be noticed.
    expect(b.container.querySelector('.wf-zoom .wf-zoom-note')!.textContent).toBe(
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
    const cases: ReadonlyArray<readonly [number, ZoomTier, typeof wideStage]> = [
      [STAGE_FITS, 'figures', fanOnly],
      [6, 'details', wideStage],
    ]
    for (const [n, tier, make] of cases) {
      const { w, tasks } = make(n)
      expect(autoTier(w.steps), `${n} steps should land at ${tier}`).toBe(tier)
      const { container, unmount } = card(w, tasks)
      const expectedW = nodeWidthAt(tier, w.steps)
      const slots = [...container.querySelectorAll<HTMLElement>('.node-slot')]
      expect(slots).toHaveLength(w.steps.length)
      for (const slot of slots) {
        expect(Number.parseFloat(slot.style.width), `${slot.textContent?.slice(0, 12)}`).toBe(
          expectedW,
        )
      }
      // `plan` is a root, so its height is the tier's own with no dependency
      // line added -- the figure `nodeHeightAt` sums from the rows the tier
      // draws, which is 246 with the four figures on it and 140 without.
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
    const a = card(six.w, six.tasks)
    expect(a.container.querySelector('.wf-band')).toBeNull()
    expect(a.container.querySelectorAll('.node')).toHaveLength(8)

    // Seven: no tier holds it on one row, and a level never wraps onto a
    // second one (owner decision 2026-09-30: a wrapped level reads as two
    // dependency levels). So it stays a band at every tier -- exactly as
    // thirteen and forty do below -- and opening it draws every card at that
    // tier's real width rather than folding them into extra rows.
    expect(autoTier(seven.w.steps)).toBe('figures')
    const b = card(seven.w, seven.tasks)
    expect(b.container.querySelector('.wf-band')).toBeTruthy()
    expect(b.container.querySelectorAll('.node')).toHaveLength(2)
    b.unmount()

    // Thirteen -- the measured run's widest stage -- is a band at every tier;
    // nothing here can widen enough to draw thirteen cards in one row without
    // wrapping, which this canvas may not do.
    for (const tier of ['figures', 'details', 'names'] as const) {
      const { w, tasks } = wideStage(13)
      const c = card(w, tasks, tier)
      expect(c.container.querySelector('.wf-band'), tier).toBeTruthy()
      expect(c.container.querySelectorAll('.node'), tier).toHaveLength(2)
      c.unmount()
    }

    // AND THE BAND STILL DOES ITS JOB PAST ONE SCREEN. Forty steps stay one
    // band at every tier too -- the claim stage collapsing exists for.
    const forty = wideStage(40)
    expect(autoTier(forty.w.steps)).toBe('figures')
    const d = card(forty.w, forty.tasks)
    expect(d.container.querySelector('.wf-band')).toBeTruthy()
    expect(d.container.querySelectorAll('.node')).toHaveLength(2)
  })

  it('drops the FIELD and never the value, and says on the canvas that it did', () => {
    // wideStage with no states: every fan step has no task, so its duration is
    // an ABSENCE -- the case that must not become a blank or a zero.
    const { w, tasks } = wideStage(5)

    const full = card(fanOnly(STAGE_FITS).w, fanOnly(STAGE_FITS).tasks)
    // Silent at the full tier. A mark saying "nothing is hidden" on every canvas
    // that fits is the noise §8.4 took off this screen twice.
    expect(full.container.querySelector('.wf-zoom .wf-zoom-note')).toBeNull()
    full.unmount()

    const details = card(w, tasks, 'details')
    const dNode = nodeNamed(details.container, 'scan-0')
    // THE DURATION IS STILL THERE AND STILL SAYS THE ABSENCE IN WORDS.
    expect(dNode.querySelector('.node-dur')!.textContent).toBe('not started')
    expect(dNode.querySelector('.node-meta')!.textContent).toBe('codex')
    // The figures are GONE FROM THE CARD rather than blank. An empty `.node-num`
    // would be the defect: a cell with no text where `not sampled` belongs says
    // the figure is fine.
    expect(dNode.querySelector('.node-nums')).toBeNull()
    expect(dNode.querySelectorAll('.node-num')).toHaveLength(0)
    const dMark = details.container.querySelector('.wf-zoom .wf-zoom-note')!
    expect(dMark.textContent).toBe('figures not drawn')
    expect(dMark.getAttribute('aria-label')).toContain(
      'decision about the zoom and not a fact about the steps',
    )
    details.unmount()

    const names = card(w, tasks, 'names')
    const nNode = nodeNamed(names.container, 'scan-0')
    expect(nNode.querySelector('.node-dur')).toBeNull()
    expect(nNode.querySelector('.node-meta')).toBeNull()
    expect(nNode.querySelector('.node-nums')).toBeNull()
    // The name and the state are in every tier.
    expect(nNode.querySelector('.node-id')!.textContent).toBe('scan-0')
    expect(nNode.querySelector('.node-state')!.textContent).toBe('not started')
    const nMark = names.container.querySelector('.wf-zoom .wf-zoom-note')!
    expect(nMark.textContent).toBe('profile, duration and figures not drawn')
    // NAMED, NOT COUNTED. "3 fields hidden" is a number a reader has to go and
    // check; these are the fields.
    expect(nMark.getAttribute('aria-label')).toContain('profile, duration and figures')
  })

  it('never hides a failed or cancelled step, at the smallest tier either', () => {
    const states: (TaskState | null)[] = ['FAILED', 'SUCCEEDED', 'SUCCEEDED', 'CANCELLED', null]
    const { w, tasks } = wideStage(5, states)
    const { container } = card(w, tasks, 'names')

    const failed = nodeNamed(container, 'scan-0')
    // THREE CHANNELS, NONE OF THEM COLOUR ALONE, at the tier that draws least:
    // the card's own accent class, the mark in its tone, and the word.
    expect(failed.className).toContain('bad')
    expect(failed.querySelector('[data-mark="failed"][data-hue="bad"]')).toBeTruthy()
    expect(failed.querySelector('.node-state')!.textContent).toBe('failed')

    const cancelled = nodeNamed(container, 'scan-3')
    expect(cancelled.querySelector('.node-state')!.textContent).toBe('cancelled')

    // AND THE TIER REALLY IS THE SMALLEST ONE, or the assertions above are
    // passing at a zoom that happens to draw everything.
    expect(failed.querySelector('.node-meta')).toBeNull()
    expect(failed.querySelector('.node-dur')).toBeNull()
    expect(failed.querySelector('.node-nums')).toBeNull()
    expect(container.querySelector('.wf-zoom .wf-zoom-note')!.textContent).toBe(
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
      const { container, unmount } = card(w, tasks, tier)
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
    const { container } = card(w, tasks)
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
    expect(container.querySelector('.wf-zoom .wf-zoom-note')).toBeNull()
  })

  it('maps the canvas only when the canvas does not fit, and marks what is off it', () => {
    const states = Array(13).fill('SUCCEEDED') as (TaskState | null)[]
    states[6] = 'FAILED'
    const { w, tasks } = wideStage(13, states)
    const { container } = card(w, tasks)

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
// 5. The stylesheet, resolved -- not grepped
// ---------------------------------------------------------------------------

describe('the shipped stylesheet', () => {
  pinTheClock()

  // The spend half of this rule moved to workflow.v2.test.tsx with the list
  // row that now draws spend (2026-10-01).
  it('draws a step with no duration differently from a measured one', () => {
    const style = withStyles()
    const { w, tasks } = timings()
    // `details`, where the duration line is drawn for every kind (see `how
    // long a step has taken`).
    const { container } = card(w, tasks, 'details')
    const none = nodeNamed(container, 'publish').querySelector('.node-dur')!
    const ran = nodeNamed(container, 'done').querySelector('.node-dur')!
    expect(getComputedStyle(none).fontStyle).toBe('italic')
    expect(getComputedStyle(ran).fontStyle).toBe('normal')
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

/** The census the open card states whole, as its `progress` fact (`CensusFact`). */
function censusOf(root: ParentNode): string {
  const fact = [...root.querySelectorAll('.wf-census .ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'progress')
  expect(fact, 'the open card does not state the census').toBeTruthy()
  return (fact!.textContent ?? '').slice('progress'.length)
}

describe('the QA pass: the census', () => {
  pinTheClock()

  // RE-POINTED with the rebrand (2026-10-01): the board's row and its meter
  // are gone, and the census sentence is the open card's `progress` fact.
  it('WF-1: accounts for every way a step ends, not only success and failure', () => {
    // The measured row: `9/30 done · 4 failed` over a workflow where the other
    // seventeen had been cancelled or dead-lettered -- seventeen steps the line
    // whose job is to account for thirty simply did not mention.
    const w = ended('wf_ended', 30, { SUCCEEDED: 9, FAILED: 4, CANCELLED: 15, DEAD_LETTERED: 2 })
    const text = censusOf(card(w).container)
    expect(text).toContain('9/30 done')
    expect(text).toContain('4 failed')
    expect(text, 'the cancelled steps are missing from the census').toContain('15 cancelled')
    expect(text, 'the dead-lettered steps are missing from the census').toContain('2 dead_lettered')
    // And a count that is zero is not printed as a clause.
    expect(censusOf(card(ended('wf_clean', 3, { SUCCEEDED: 3 })).container)).toBe('3/3 done')
  })

  /**
   * THE CENSUS READS WHOLE WITHOUT A POINTER (owner decision 2026-09-26, on
   * #223). A `title` is whole on hover only, and a phone has no hover, so the
   * open card states the census as a fact, whole, at every width: §7.3's rule
   * for a cut value is whole somewhere a reader can get to without a pointer.
   */
  describe('the census reads whole without a pointer', () => {
    const WIDE: CascadeEnv = { width: 1440 }
    const PHONE: CascadeEnv = { width: 390 }
    const LONG = '10/30 done · 1 failed · 19 cancelled'
    const won = (el: Element, prop: string | readonly string[], env: CascadeEnv): string | null => {
      const r = cascade(STYLES, el, prop, env)
      expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
      return r.winner?.value ?? null
    }

    // MUTATION: take the census out of the open card; hide it at a width; or
    // cut it (one line, an ellipsis).
    it('states the whole census in the open card, at 1440 and at 390', () => {
      const { container } = card(ended('wf_long', 30, { SUCCEEDED: 10, FAILED: 1, CANCELLED: 19 }), new Map())
      const body = container.querySelector('.wf-body')
      expect(body, 'the card did not open').not.toBeNull()
      const fact = [...body!.querySelectorAll('.ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'progress')
      expect(fact, 'the open card does not state the census').toBeTruthy()
      expect((fact!.textContent ?? '').slice('progress'.length), 'the open card cuts or rewords the census').toBe(LONG)
      // Every box from the fact up to the open body.
      const boxes: Element[] = []
      for (let el: Element | null = fact!; el !== null && el !== body!.parentElement; el = el.parentElement) boxes.push(el)
      let asked = 0
      for (const env of [WIDE, PHONE]) {
        for (const el of boxes) {
          const what = `${env.width}: \`${el.tagName.toLowerCase()}${el.className ? `.${el.className.split(' ').join('.')}` : ''}\``
          expect(won(el, 'display', env), `${what} hides the census`).not.toBe('none')
          expect(won(el, 'white-space', env) ?? 'normal', `${what} holds the census to one line`).not.toBe('nowrap')
          expect(won(el, 'text-overflow', env) ?? 'clip', `${what} cuts the census`).not.toBe('ellipsis')
          asked += 1
        }
      }
      expect(asked).toBe(boxes.length * 2)
    })
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
    const { container } = card(w, tasks)
    const band = container.querySelector('.wf-band')!
    // The cancellation is still counted, first, in its own tone ...
    expect(bandCounts(band)[0]).toBe('2 cancelled')
    expect(band.querySelector('.wf-band-toggle')!.getAttribute('aria-label')).toContain('0 failed and 2 cancelled.')
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
    const { container } = card(fanOut(), new Map())
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
    const { container } = card(w, tasks, 'figures')
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
    const { container } = card(w, tasks)
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

// ---------------------------------------------------------------------------
// 8. The owner's decisions on the QA pass (epic #83), the workflows lane
// ---------------------------------------------------------------------------
//
// Each box below was decided by the owner on 2026-09-25 and each case was
// committed RED against the code it describes before the change landed.

type Theme = 'dark' | 'light'
const THEMES: readonly Theme[] = ['dark', 'light']
const TOKENS = tokenTables(STYLES)
const SHEETS: Readonly<Record<Theme, string>> = {
  dark: resolveSheet(STYLES, 'dark'),
  light: resolveSheet(STYLES, 'light'),
}

/** A token's colour in one theme, resolved from the shipped `:root` blocks. */
function tokenColour(name: string, theme: Theme): RGBA {
  const v = TOKENS[theme].get(name)
  expect(v, `${name} is not declared`).toBeDefined()
  const c = colour(resolveVars(v!, TOKENS[theme]))
  expect(c, `${name} does not resolve to a colour`).not.toBeNull()
  return c!
}

/**
 * The colour the cascade gives `el` for `props`, in one theme at 1440, through
 * `cssgate`'s `cascade` over the resolved sheet -- not `getComputedStyle`,
 * which orders rules by source position alone (design-system.md §14.1). A
 * shorthand (`border: 1px solid #828d9b`) carries its colour as one of its
 * words. Null when no rule declares any of `props`.
 */
function paintOf(el: Element, props: readonly string[], theme: Theme): RGBA | null {
  const r = cascade(SHEETS[theme], el, props, { width: 1440, theme })
  if (r.winner === null) return null
  for (const word of r.winner.value.split(/\s+(?![^(]*\))/)) {
    if (word === '') continue
    const c = colour(word)
    if (c !== null) return c
  }
  return null
}

const sameColour = (a: RGBA | null, b: RGBA): boolean =>
  a !== null && Math.abs(a.r - b.r) < 0.5 && Math.abs(a.g - b.g) < 0.5 && Math.abs(a.b - b.b) < 0.5

describe('the owner’s decisions: the open card', () => {
  pinTheClock()

  it('WF-13: says what the run opens in the card’s own short phrase, and leaves the sentence to the forms', () => {
    const dispatched = (strategy: TaskDispatch['strategy']): TaskDispatch => ({
      strategy,
      carrier: 'checkpoints',
      role: null,
      integrates: [],
    })
    const opens = (strategy: TaskDispatch['strategy']): string | null => {
      const w = workflow('wf_dispatch', [step('a', [], { task_id: 't_a' }), step('b', ['a'], { task_id: 't_b' })])
      const tasks = new Map<string, Task>([
        ['t_a', task('t_a', 'RUNNING', { started_at: iso(-60_000), dispatch: dispatched(strategy) })],
        ['t_b', task('t_b', 'QUEUED', { dispatch: dispatched(strategy) })],
      ])
      const { container, unmount } = card(w, tasks)
      const li = [...container.querySelectorAll('.wf-dispatch li.ctl-fact')].find(
        (el) => el.querySelector('b')?.textContent === 'opens',
      )
      const text = li === undefined ? null : (li.textContent ?? '').slice('opens'.length)
      unmount()
      return text
    }
    // `dispatch collect · opens no pull request and pushes nothing` -- a phrase
    // that reads after its key, where it read "opens No pull request. Nothing
    // is pushed." The forms keep that sentence (dispatch.test.ts).
    expect(opens('collect')).toBe('no pull request and pushes nothing')
    expect(opens('integrate')).toBe('up to 1 pull request, for all steps')
    expect(opens('direct-pr')).toBe('up to 2 pull requests')
  })

  it('WF-14: says what the zoom leaves out as a view choice, in faint type, with no data mark', () => {
    // Under a chosen tier ...
    const { w, tasks } = wideStage(5)
    const { container } = card(w, tasks, 'details')
    const note = container.querySelector<HTMLElement>('.wf-zoom .wf-zoom-note')
    expect(note, 'the zoom notice is not a .wf-zoom-note').toBeTruthy()
    expect(note!.textContent).toBe('figures not drawn')
    // NOT A DATA MARK. `.ctl-mark.is-partial` says a READ came back partial;
    // a zoom is a choice about the view, and the amber one-sided dash made the
    // reader's own choice look like something the platform failed to report.
    expect(note!.classList.contains('ctl-mark'), 'the zoom notice is still a data mark').toBe(false)
    expect(note!.querySelector('.ctl-mark'), 'a data mark is still inside the zoom notice').toBeNull()
    expect(container.querySelector('.wf-zoom .ctl-mark')).toBeNull()
    // The explanatory sentence is still its accessible name, and its title.
    expect(note!.getAttribute('role')).toBe('status')
    expect(note!.getAttribute('aria-label')).toContain('decision about the zoom and not a fact about the steps')
    expect(note!.getAttribute('title')).toBe(note!.getAttribute('aria-label'))
    // ... and under Auto, which picks `details` for a six-wide stage on its own.
    const six = wideStage(6)
    const auto = card(six.w, six.tasks)
    expect(auto.container.querySelector('.wf-zoom .wf-zoom-note')?.textContent).toBe('figures not drawn')

    // FAINT, AND NOTHING ELSE: --text-faint in both themes, no border, no dash.
    for (const theme of THEMES) {
      expect(
        sameColour(paintOf(note!, ['color'], theme), tokenColour('--text-faint', theme)),
        `${theme}: the zoom note is not --text-faint`,
      ).toBe(true)
      const rule = cascade(
        SHEETS[theme],
        note!,
        ['border', 'border-style', 'border-left', 'border-left-style', 'border-right', 'border-right-style', 'border-bottom', 'border-bottom-style'],
        { width: 1440, theme },
      )
      expect(rule.winner, `${theme}: the zoom note draws a rule`).toBeNull()
    }
  })

  it('WF-19: breaks a dependency line only between its units, never inside a step id', () => {
    expect(typeof depUnits, 'dag.ts exports no depUnits').toBe('function')
    // A filename with a space in it, and a hyphenated id -- the two things a
    // browser breaks a line at.
    const file = 'aaaaaaaaaaaaaaaaaaaa bbbb.md'
    const merge = step('merge', ['plan', 'check-3'], { input_from: { plan: file } })
    const w = workflow('wf_deps', [step('plan', []), step('check-3', []), merge])
    const { container } = card(w, new Map())
    const line = nodeNamed(container, 'merge').querySelector<HTMLElement>('.node-dep')!
    const items = [...line.querySelectorAll('.node-dep-item')].map((el) => el.textContent)
    // ONE LIST FOR THE MARKUP AND THE HEIGHT: what is drawn is what was measured.
    expect(items, 'the dependency line is not drawn as units').toEqual(depUnits(merge).map((u) => u.text))
    expect(items).toEqual(['plan', `(${file}),`, 'check-3'])
    // Still one line of text naming every parent whole, and its title unchanged.
    expect(line.textContent).toBe(`↑ plan (${file}), check-3`)
    expect(line.getAttribute('title')).toBe('depends on plan, check-3')
  })

  it('WF-19: counts a unit with a space in it as one token, as the browser now draws it', () => {
    // At the full tier a line holds 31 columns. `(aaaaaaaaaaaaaaaaaaaa bbbb.md),`
    // is exactly 31: split on its space, its first half fitted after `plan` and
    // the height came out a line short of the box the browser draws.
    const spaced = step('merge', ['plan', 'fencing'], { input_from: { plan: 'aaaaaaaaaaaaaaaaaaaa bbbb.md' } })
    const solid = step('merge', ['plan', 'fencing'], { input_from: { plan: 'aaaaaaaaaaaaaaaaaaaa_bbbb.md' } })
    expect(heightOf(spaced), 'a space inside a filename changed the measured height').toBe(heightOf(solid))
    expect(heightOf(spaced)).toBeGreaterThan(heightOf(step('merge', ['plan', 'fencing'])))
  })

  it('WF-19: gives a unit longer than a line the whole line, so nothing shares a line with it', () => {
    // FIX-UP, #160 review finding 3. An inline-block whose text is wider than
    // the line is shrink-to-fit to the WHOLE line: it cannot sit after `↑ ` or
    // after a unit before it, and nothing after it can share its last line.
    // `depLines` let the next unit onto that last line, so a long file unit
    // followed by another parent came out a line short -- 18px of dependency
    // text in the stop strip, the overlap `heightOf` exists to prevent.
    //
    // `details`, which Auto picks for a six-wide stage: short names, so a line
    // holds (144 - 28) / 7.22 = 16 columns and `(cc-checkpoint.md),` is 19.
    const merge = step('merge', ['plan', 'fencing'], { input_from: { plan: 'cc-checkpoint.md' } })
    const w = workflow('wf_long_unit', [step('plan', []), step('fencing', []), merge])
    const dw = nodeWidthAt('details', w.steps)
    const perLine = Math.floor((dw - NODE_CHROME_W) / monoW(1, 12))
    expect(depUnits(merge).map((u) => u.text)).toEqual(['plan', '(cc-checkpoint.md),', 'fencing'])
    expect('(cc-checkpoint.md),'.length, 'the fixture no longer has a unit longer than a line').toBeGreaterThan(perLine)
    // The lines the model counts, read back out of the height: `--ctl-s1` (4)
    // above the list, 18 a line (12 x 1.45, rounded up), as the sheet declares.
    const lines = (s: WorkflowStep) => (heightOf(s, 'details', dw) - nodeHeightAt('details') - 4) / 18
    // `↑ plan` / `(cc-checkpoint.md),` on two lines of its own / `fencing`.
    expect(lines(merge), 'the unit after a line-wide one was packed onto its last line').toBe(
      1 + Math.ceil('(cc-checkpoint.md),'.length / perLine) + 1,
    )
    // LAST, it changes nothing: nothing follows it to be displaced.
    const last = step('merge', ['fencing', 'plan'], { input_from: { plan: 'cc-checkpoint.md' } })
    expect(lines(last)).toBe(1 + Math.ceil('(cc-checkpoint.md)'.length / perLine))
    // FIRST -- a parent this workflow does not contain, so its id was never
    // measured into the node's width -- it cannot sit after the `↑ ` either.
    const stray = step('merge', ['a-parent-this-workflow-lacks'])
    expect('a-parent-this-workflow-lacks'.length).toBeGreaterThan(perLine)
    expect(lines(stray), 'a line-wide first unit was counted as fitting after the arrow').toBe(
      1 + Math.ceil('a-parent-this-workflow-lacks'.length / perLine),
    )
  })

  it('WF-5: says a figure is the result’s on a line of its own that the node counts, and never clips the figure', () => {
    // FIX-UP, #160 review finding 2. `from result` shared the value's column,
    // which is budgeted for a 20-character figure and nothing else: beside a
    // token figure the note showed as `…`, and past 173px the figure itself was
    // CLIPPED with no mark (`text-overflow: clip`), the F1 class NODE_W was set
    // to prevent. The note now has a line the node reserves for it.
    const tasks = new Map<string, Task>([
      [
        't_work',
        task('t_work', 'SUCCEEDED', {
          started_at: iso(-90_000),
          completed_at: iso(-30_000),
          result_summary: { runner: { usage: { total_cost_usd: 0.5, input_tokens: 123_400, output_tokens: 45_600 } } },
        }),
      ],
    ])
    const w = workflow('wf_src', [step('work', [], { task_id: 't_work' }), step('idle', [])])
    // Outside the sample (the harness's `usage: null`), at the Figures tier.
    const { container } = card(w, tasks)
    const node = nodeNamed(container, 'work')
    const row = (label: string) => {
      const r = [...node.querySelectorAll<HTMLElement>('.node-num')].find((el) => el.querySelector('dt')?.textContent === label)
      expect(r, `no ${label} figure on the node`).toBeTruthy()
      return r!
    }
    // THE FIGURE, WHOLE, and nothing else in its slot.
    expect(row('cost').querySelector('dd')!.textContent).toBe('$0.50')
    expect(row('tokens').querySelector('dd')!.textContent).toBe('123.4k in · 45.6k out')
    expect(node.querySelector('.node-num .wf-src'), 'the note still shares the figure’s column').toBeNull()

    // THE NOTE, in the one spelling, naming the figures it is about.
    const src = node.querySelector<HTMLElement>('.node-src')
    expect(src, 'the node draws no source line').toBeTruthy()
    expect(src!.textContent).toBe('cost · tokens from result')
    expect(src!.querySelector('.wf-src')?.textContent).toBe('from result')
    // RESERVED on a node with nothing to say, so no card changes height when
    // the attempt read lands and turns a figure into the result's.
    const idle = nodeNamed(container, 'idle').querySelector<HTMLElement>('.node-src')
    expect(idle, 'the source line is not reserved on every Figures-tier node').toBeTruthy()
    expect(idle!.textContent).toBe('')

    // COUNTED IN THE HEIGHT: 10 + 42 of padding, 2 of border, `.node-id` 21,
    // three micro rows (`.node-line`, `.node-meta`, `.node-src`) at 17.4,
    // `.node-nums` 8 + 4 x 22 + 3 x 2, and four `--ctl-s1` gaps between five
    // children.
    const figuresH = Math.ceil(10 + 42 + 2 + 14 * 1.5 + 3 * (12 * 1.45) + (8 + 4 * (14 * 1.5 + 1) + 3 * 2) + 4 * 4)
    expect(nodeHeightAt('figures'), 'the node does not count its source line').toBe(figuresH)
    expect(Number.parseFloat(slotOf(container, 'work').style.height)).toBe(figuresH)
    // AND IN THE WIDTH: the longest line it can print fits the content box,
    // and so does the widest figure beside its label.
    expect(monoW('cost · tokens from result'.length, 12)).toBeLessThanOrEqual(NODE_W - NODE_CHROME_W)
    expect(46 + 8 + monoW(20, 14)).toBeLessThanOrEqual(NODE_W - NODE_CHROME_W)

    // A FIGURE THAT OUTGROWS ITS COLUMN ELLIPSES; it is never cut silently.
    for (const theme of THEMES) {
      for (const label of ['cost', 'tokens']) {
        const r = cascade(SHEETS[theme], row(label).querySelector('dd')!, 'text-overflow', { width: 1440, theme })
        expect(r.winner?.value, `${theme}: the ${label} figure is clipped without a mark`).toBe('ellipsis')
      }
    }
  })

  it('WF-9: opens a canvas wider than its column on the start node, and again when a stage is opened', () => {
    // THE VIEWPORT, stubbed, because jsdom has no layout: the wrapper is a
    // 390px phone's 358px column, and it scrolls as far as the canvas is wide.
    // Everything else keeps answering 0, so the rail and the canvas both sit at
    // the wrapper's own origin here.
    const PHONE = 358
    const width = vi.spyOn(Element.prototype, 'clientWidth', 'get').mockImplementation(function (this: Element) {
      return this.classList.contains('wf-canvas-wrap') ? PHONE : 0
    })
    const scroll = vi.spyOn(Element.prototype, 'scrollWidth', 'get').mockImplementation(function (this: Element) {
      if (!this.classList.contains('wf-canvas-wrap')) return 0
      const canvas = this.querySelector<HTMLElement>('.wf-canvas')
      return canvas === null ? 0 : Number.parseFloat(canvas.style.width)
    })
    try {
      const seen = (root: ParentNode, name: string) => {
        const wrap = root.querySelector<HTMLElement>('.wf-canvas-wrap')!
        const slot = slotOf(root, name)
        const left = Number.parseFloat(slot.style.left)
        return { left, right: left + Number.parseFloat(slot.style.width), from: wrap.scrollLeft, to: wrap.scrollLeft + PHONE }
      }
      // KEEP CENTRING: a five-wide fan at `details` is ~850px and its start
      // node sits in the middle of it -- past the right edge of the column
      // when the wrapper opens at scrollLeft 0.
      const fan = card(fanOut(), new Map())
      const a = seen(fan.container, 'plan')
      expect(a.left, 'the canvas opened on an empty lane, with the start node off screen').toBeGreaterThanOrEqual(a.from)
      expect(a.right).toBeLessThanOrEqual(a.to)
      fan.unmount()

      // A thirteen-wide stage: collapsed, the start node is off a phone's
      // column too; opened, the canvas is 3,671px and the node moves to its
      // middle.
      const { w, tasks } = wideStage(13)
      const wide = card(w, tasks)
      const b = seen(wide.container, 'plan')
      expect(b.left).toBeGreaterThanOrEqual(b.from)
      expect(b.right).toBeLessThanOrEqual(b.to)
      expect(openEveryBand(wide.container)).toBe(1)
      const c = seen(wide.container, 'plan')
      expect(c.left, 'opening a stage left the start node off screen').toBeGreaterThanOrEqual(c.from)
      expect(c.right).toBeLessThanOrEqual(c.to)
    } finally {
      width.mockRestore()
      scroll.mockRestore()
    }
  })

  it('WF-10: marks the stage band that holds the picked step, and opens it to that step', () => {
    const { w, tasks } = wideStage(13)
    function Picked({ picked }: { picked: string }) {
      const [stages, setStages] = useState<Record<string, boolean>>({})
      return (
        <WorkflowCard
          workflow={w}
          taskById={tasks}
          usage={{ kind: 'ready', usage: null }}
          openStages={stages}
          onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
          picked={picked}
          onPick={noop}
          loadAttempts={async () => ({ status: 'empty', fetchedAt: T0 })}
          reload={noop}
        />
      )
    }
    const held = render(<Picked picked="scan-7" />)
    const band = held.container.querySelector<HTMLElement>('.wf-band')!
    expect(band.className, 'the band holding the picked step is not marked').toContain('holds-picked')
    expect(band.querySelector('.wf-band-pick')?.textContent).toBe('scan-7')
    expect(band.querySelector('.wf-band-toggle')!.getAttribute('aria-label')).toContain('scan-7')
    // OPENED TO IT: the stage is drawn, and the picked step is outlined in it.
    expect(band.querySelector('.wf-band-toggle')!.getAttribute('aria-expanded'), 'the band holding the picked step stayed shut').toBe('true')
    expect(nodeNamed(held.container, 'scan-7').className).toContain('is-picked')
    held.unmount()

    // A band that does not hold the picked step is neither marked nor opened.
    const other = render(<Picked picked="plan" />)
    const quiet = other.container.querySelector<HTMLElement>('.wf-band')!
    expect(quiet.className).not.toContain('holds-picked')
    expect(quiet.querySelector('.wf-band-pick')).toBeNull()
    expect(quiet.querySelector('.wf-band-toggle')!.getAttribute('aria-expanded')).toBe('false')
  })
})

// ---------------------------------------------------------------------------
// WF-4: an edge that skips a level
// ---------------------------------------------------------------------------

/**
 * Points along a path `edgePath` writes, sampled: `M`, then `C` and `L`
 * segments (and `V` / `H`, should the router ever use them).
 */
function along(d: string): { x: number; y: number }[] {
  const out: { x: number; y: number }[] = []
  let at = { x: 0, y: 0 }
  const STEPS = 24
  for (const seg of d.match(/[MCLVH][^MCLVH]*/g) ?? []) {
    const cmd = seg[0]
    const n = seg
      .slice(1)
      .trim()
      .split(/[\s,]+/)
      .filter((t) => t !== '')
      .map(Number)
    const line = (to: { x: number; y: number }) => {
      for (let i = 1; i <= STEPS; i++) {
        const t = i / STEPS
        out.push({ x: at.x + (to.x - at.x) * t, y: at.y + (to.y - at.y) * t })
      }
      at = to
    }
    if (cmd === 'M') {
      at = { x: n[0]!, y: n[1]! }
      out.push(at)
    } else if (cmd === 'L') line({ x: n[0]!, y: n[1]! })
    else if (cmd === 'V') line({ x: at.x, y: n[0]! })
    else if (cmd === 'H') line({ x: n[0]!, y: at.y })
    else if (cmd === 'C') {
      const [x1, y1, x2, y2, x, y] = n as [number, number, number, number, number, number]
      const p0 = at
      for (let i = 1; i <= STEPS; i++) {
        const t = i / STEPS
        const u = 1 - t
        out.push({
          x: u * u * u * p0.x + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x,
          y: u * u * u * p0.y + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y,
        })
      }
      at = { x, y }
    }
  }
  return out
}

/** Every drawn box an edge may not pass under: every node card, and every
 *  COLLAPSED band -- opaque, and painted over the edges. */
function cardsOf(l: DagLayout): { name: string; x: number; y: number; w: number; h: number }[] {
  return [
    ...l.nodes.map((n) => ({ name: n.step.step_id, x: n.x, y: n.y, w: l.nodeW, h: n.h })),
    ...l.bands.filter((b) => !b.expanded).map((b) => ({ name: `band ${b.level}`, x: b.x, y: b.y, w: b.w, h: b.h })),
  ]
}

/** Every edge, sampled, and not one point of it strictly inside a card or a
 *  band, or outside the canvas. Returns how many points were checked. */
function assertNothingPassesUnder(l: DagLayout): number {
  let checked = 0
  for (const e of l.edges) {
    for (const p of along(edgePath(e))) {
      checked += 1
      for (const b of cardsOf(l)) {
        const under = p.x > b.x + 1 && p.x < b.x + b.w - 1 && p.y > b.y + 1 && p.y < b.y + b.h - 1
        expect(under, `${e.from} -> ${e.to} passes under ${b.name} at (${p.x.toFixed(1)}, ${p.y.toFixed(1)})`).toBe(false)
      }
      expect(p.x, `${e.from} -> ${e.to} runs off the canvas`).toBeGreaterThanOrEqual(0)
      expect(p.x, `${e.from} -> ${e.to} runs off the canvas`).toBeLessThanOrEqual(l.width)
    }
  }
  return checked
}

describe('the owner’s decisions: edges that skip a level (WF-4)', () => {
  it('routes an edge that skips a level around the cards between, each on a lane of its own', () => {
    // `plan -> d` and `seed -> d` both skip level 1, whose middle card `b` sits
    // on the straight line between them: drawn as one cubic each, they ran
    // behind `b` and read as edges INTO it.
    const l = layoutOf([
      step('plan', []),
      step('seed', []),
      step('a', ['plan']),
      step('b', ['plan']),
      step('c', ['plan']),
      step('d', ['a', 'b', 'c', 'plan', 'seed']),
    ])
    expect(l.edges.filter((e) => e.to === 'd')).toHaveLength(5)
    expect(assertNothingPassesUnder(l)).toBeGreaterThan(100)

    // ITS OWN LANE: where the two cross level 1, they never share a line.
    const top = l.levelTop[1]!
    const bottom = l.levelTop[2]! - LEVEL_GAP
    const crossing = (from: string) =>
      along(edgePath(l.edges.find((e) => e.from === from && e.to === 'd')!)).filter((p) => p.y > top && p.y < bottom)
    const plan = crossing('plan')
    const seed = crossing('seed')
    expect(plan.length).toBeGreaterThan(0)
    expect(seed.length).toBeGreaterThan(0)
    for (const p of plan) for (const q of seed) expect(Math.abs(p.x - q.x), 'two skip edges share a lane').toBeGreaterThanOrEqual(4)
  })

  it('routes a collapsed stage’s edges past the next level’s cards, so a direct dependency is not hidden behind one', () => {
    // THE MEASURED CASE: `synthesis` depends directly on six of a 13-step
    // stage and on three rollups. The stage is a band; its six edges to
    // `synthesis` ran straight down the canvas's middle, collinear with the
    // band's edges into `rollup-b` and behind that card, so the six direct
    // dependencies did not appear at all.
    const impl = Array.from({ length: 13 }, (_, i) => `impl-${i + 1}`)
    const l = layoutOf([
      step('plan', []),
      ...impl.map((id) => step(id, ['plan'])),
      step('rollup-a', impl.slice(0, 4)),
      step('rollup-b', impl.slice(4, 9)),
      step('rollup-c', impl.slice(9)),
      step('synthesis', [...impl.slice(0, 6), 'rollup-a', 'rollup-b', 'rollup-c']),
    ])
    expect(l.bands.filter((b) => !b.expanded)).toHaveLength(1)
    const direct = l.edges.filter((e) => e.to === 'synthesis' && e.from.startsWith('impl-'))
    expect(direct).toHaveLength(6)
    expect(assertNothingPassesUnder(l)).toBeGreaterThan(500)
    // The six share one path, as every edge out of a collapsed band does
    // (`edgeKinds` paints them as one edge) -- and that path is drawn.
    expect(new Set(direct.map((e) => edgePath(e))).size).toBe(1)
  })
})

// ---------------------------------------------------------------------------
// 10. The settlements of 2026-09-25 (epics #83 and #87), the stragglers lane
// ---------------------------------------------------------------------------
//
// Two rulings the owner delegated and settled on the epics after the decision
// PRs merged. Each case was committed RED against main before the change.

describe('the settlements: what a healthy step does not draw', () => {
  pinTheClock()

  /**
   * CH-17's RULING, ON WORKFLOWS (settled on #87, 2026-09-25). #182 found two
   * healthy greens neither hue ruling had reached: the row's state word
   * `.wf-state.ok` in `--ok-ink`, and the graph node's 3px `.node.ok` rule in
   * `--ok`. A healthy state is a fact, not a verdict, so both go neutral, like
   * the chip and the dot: the word takes the row's `--text-dim`, and the rule
   * the node's own `--text-faint`. The dot beside each still says succeeded.
   * The row's word went with the board's row (rebrand, 2026-10-01); the node
   * rule is what is left to hold. THE NEUTRAL IS NOW THE BRAND'S: the rebrand
   * tints a step in the vocabulary of its state (`.node.t-live` teal,
   * `.t-park` violet, `.t-bad` red, `.t-neu` grey), so a succeeded step's edge
   * is the grey `--s-neu` and not `--text-faint`. The ruling is unchanged: a
   * healthy step draws no hue of any state.
   *
   * MUTATION: put the green back on the node rule.
   */
  it('CH-17: a healthy step’s node rule carries no state hue', () => {
    const tasks = new Map<string, Task>([
      ['t_a', task('t_a', 'SUCCEEDED', { started_at: iso(-120_000), completed_at: iso(-60_000) })],
    ])
    const w = workflow('wf_ok', [step('a', [], { task_id: 't_a' })], {
      state: 'SUCCEEDED',
      stored_state: 'SUCCEEDED',
      rollup: {
        state: 'SUCCEEDED',
        complete: true,
        reason: 'all_steps_succeeded',
        counts: { SUCCEEDED: 1 },
        unreadable_steps: [],
        unstarted_steps: [],
        steps_read: 1,
      },
    })
    const { container } = card(w, tasks)
    const node = container.querySelector('.node.ok')
    expect(node, 'a succeeded step no longer carries .node.ok; this check is vacuous').not.toBeNull()
    const HUES = [
      '--ok', '--ok-ink', '--info', '--info-ink', '--warn', '--warn-ink', '--bad', '--bad-ink', '--paused',
      '--s-live', '--s-park', '--s-bad', '--s-warn',
    ]
    for (const theme of THEMES) {
      const rule = paintOf(node!, ['border-left-color', 'border-left', 'border-color', 'border'], theme)
      expect(
        sameColour(rule, tokenColour('--s-neu', theme)),
        `${theme}: the node's healthy rule is not the brand neutral --s-neu`,
      ).toBe(true)
      for (const h of HUES) {
        expect(sameColour(rule, tokenColour(h, theme)), `${theme}: the healthy node rule is painted ${h}`).toBe(false)
      }
    }
  })
})

// ---------------------------------------------------------------------------
// #105: a failed step says why, on the node, on the band and on the row
// ---------------------------------------------------------------------------

describe('#105: a failure’s cause is on the canvas, not only in the inspector', () => {
  pinTheClock()

  const COLLISION = (f: string) => `input collision: ${f} is staged by two parents\nparents: plan, scan-0`

  /** A root, a thirteen-wide stage (a band at every tier) with four of its
   *  steps failed on one cause and one on another, and a join. */
  function broken(): { w: Workflow; tasks: Map<string, Task> } {
    const tasks = new Map<string, Task>()
    const fan = Array.from({ length: 13 }, (_, i) => `scan-${i}`)
    tasks.set('t_plan', task('t_plan', 'SUCCEEDED', { started_at: iso(-500_000), completed_at: iso(-400_000) }))
    const steps: WorkflowStep[] = [step('plan', [], { task_id: 't_plan' })]
    fan.forEach((id, i) => {
      const failed = i < 5
      tasks.set(
        `t_${id}`,
        task(`t_${id}`, failed ? 'FAILED' : 'SUCCEEDED', {
          started_at: iso(-300_000),
          completed_at: iso(-200_000),
          last_error: !failed ? null : i === 4 ? 'exit 1: the agent crashed' : COLLISION(`${id}.md`),
        }),
      )
      steps.push(step(id, ['plan'], { task_id: `t_${id}` }))
    })
    tasks.set('t_join', task('t_join', 'FAILED', { started_at: iso(-100_000), completed_at: iso(-50_000), last_error: COLLISION('join.md') }))
    steps.push(step('join', fan, { task_id: 't_join' }))
    const w = workflow('wf_broken', steps, {
      rollup: {
        state: 'RUNNING',
        complete: true,
        reason: 'steps_hold_capacity',
        counts: { SUCCEEDED: 9, FAILED: 6 },
        unreadable_steps: [],
        unstarted_steps: [],
        steps_read: steps.length,
      },
    })
    return { w, tasks }
  }

  it('prints the error’s first line on a failed node, whole in its title and on focus, and measures it', () => {
    const { w, tasks } = broken()
    const { container } = card(w, tasks)
    const node = nodeNamed(container, 'join')
    const note = node.querySelector<HTMLElement>('.node-note')
    expect(note, 'a failed node shows no cause').toBeTruthy()
    expect(note!.textContent).toBe('input collision: join.md is staged by two parents')
    expect(note!.getAttribute('title')).toBe(COLLISION('join.md'))
    // ON FOCUS: the card's description is the whole error, never truncated.
    const described = node.getAttribute('aria-describedby')
    expect(described, 'the focused card does not describe its error').toBeTruthy()
    const full = container.querySelector<HTMLElement>(`[id="${described}"]`)
    expect(full?.textContent).toBe(COLLISION('join.md'))
    expect(full!.classList.contains('node-note-full')).toBe(true)
    // OUTSIDE THE BUTTON: shown on focus inside it, the error joined the
    // button's name and a screen reader heard it twice.
    expect(node.contains(full), 'the full note is inside the card, so it is read as its name too').toBe(false)
    expect(node.textContent).not.toContain(COLLISION('join.md').split('\n')[1] ?? '\u0000')
    // COUNTED: the slot is the height `heightOf` gives a node with that line.
    const layout = layoutOf(w.steps, new Set<number>(), autoTier(w.steps), new Set(['join']))
    const placed = layout.nodes.find((n) => n.step.step_id === 'join')!
    expect(Number.parseFloat(slotOf(container, 'join').style.height)).toBe(placed.h)
    expect(placed.h).toBe(heightOf(w.steps[w.steps.length - 1]!, layout.tier, layout.nodeW, true))
    expect(placed.h).toBeGreaterThan(heightOf(w.steps[w.steps.length - 1]!, layout.tier, layout.nodeW))
    // A node that succeeded carries no line and is not made taller for one.
    expect(nodeNamed(container, 'plan').querySelector('.node-note')).toBeNull()
    expect(Number.parseFloat(slotOf(container, 'plan').style.height)).toBe(nodeHeightAt(layout.tier))
  })

  it('keeps the line to one line in the sheet, and shows the whole error on focus', () => {
    const rule = (sel: string) => {
      const at = STYLES.indexOf(`\n${sel} {`)
      expect(at, `no ${sel} rule`).toBeGreaterThan(-1)
      return STYLES.slice(at, STYLES.indexOf('}', at))
    }
    const note = rule('.node-note')
    expect(note).toMatch(/white-space:\s*nowrap/)
    expect(note).toMatch(/text-overflow:\s*ellipsis/)
    expect(rule('.node-note-full')).toMatch(/display:\s*none/)
    expect(STYLES).toMatch(/\.node:focus-visible ~ \.node-note-full\s*\{[^}]*display:\s*block/)
  })

  it('names the cause on a band holding failures, and in its accessible name', () => {
    const { w, tasks } = broken()
    const { container } = card(w, tasks)
    const band = container.querySelector<HTMLElement>('.wf-band')
    expect(band, 'the thirteen-wide stage is not a band; this case is vacuous').toBeTruthy()
    const cause = band!.querySelector<HTMLElement>('.wf-band-cause')
    expect(cause, 'a band holding failures says nothing about why').toBeTruthy()
    // The commonest cause, as its first failure wrote it, and a count of the rest.
    expect(cause!.textContent).toBe('input collision: scan-0.md is staged by two parents (+1 other cause)')
    expect(cause!.getAttribute('title')).toContain(COLLISION('scan-0.md'))
    expect(cause!.getAttribute('title')).toContain('scan-4: exit 1: the agent crashed')
    expect(band!.querySelector('.wf-band-toggle')!.getAttribute('aria-label')).toContain('input collision: scan-0.md is staged by two parents')
    // A clean band has no cause line.
    const clean = card(wideStage(13, Array(13).fill('SUCCEEDED')).w, wideStage(13, Array(13).fill('SUCCEEDED')).tasks)
    expect(clean.container.querySelector('.wf-band')).toBeTruthy()
    expect(clean.container.querySelector('.wf-band-cause')).toBeNull()
  })

  it('groups the workflow’s failures by a normalised cause, in the census', () => {
    const { w, tasks } = broken()
    expect(censusOf(card(w, tasks).container)).toBe('9/15 done · 5 failed: input collision · 1 failed: exit 1')
    // Without the task read there is no cause to group, and the count stands alone.
    expect(censusOf(card(w, null).container)).toBe('9/15 done · 6 failed')
  })
})

// ---------------------------------------------------------------------------
// #330: the workflow's name in the inspector, and the graph's details panel
// ---------------------------------------------------------------------------

/** A two-step chain whose step tasks carry the spec's label as `metadata.unit`. */
function labelled(label: string | null): { w: Workflow; tasks: Map<string, Task> } {
  const w = workflow('wf_5e5ad3b6f7da4299a839', [
    step('implement', [], { task_id: 't_impl' }),
    step('review', ['implement'], { task_id: 't_rev' }),
  ])
  const metadata = label === null ? { origin: 'swarm-mcp' } : { origin: 'swarm-mcp', unit: label }
  const tasks = new Map<string, Task>([
    ['t_impl', task('t_impl', 'SUCCEEDED', { started_at: iso(-300_000), completed_at: iso(-200_000), metadata })],
    ['t_rev', task('t_rev', 'RUNNING', { started_at: iso(-100_000), metadata })],
  ])
  return { w, tasks }
}

describe('#330: the label names the workflow', () => {
  pinTheClock()

  it('names the workflow by its label in the inspector head too', () => {
    const { w, tasks } = labelled('workflows-board')
    const { container } = render(
      <WorkflowCard
        workflow={w}
        taskById={tasks}
        usage={{ kind: 'ready', usage: null }}
        openStages={{}}
        onToggleStage={noop}
        loadAttempts={async () => ({ status: 'empty', fetchedAt: T0 })}
        reload={noop}
      />,
    )
    fireEvent.click(nodeNamed(container, 'review'))
    const head = container.querySelector<HTMLElement>('.wf-inspect-head')!
    const wf = head.querySelector<HTMLElement>('.wf-inspect-wf')
    expect(wf, 'the inspector head does not name the workflow').toBeTruthy()
    expect([...wf!.children].map((c) => c.textContent)).toEqual(['workflows-board', 'wf_5e5ad3b6f7da4299a839'])
    // Without a label the head carries the id alone.
    const bare = render(
      <WorkflowCard
        workflow={w}
        taskById={labelled(null).tasks}
        usage={{ kind: 'ready', usage: null }}
        openStages={{}}
        onToggleStage={noop}
        loadAttempts={async () => ({ status: 'empty', fetchedAt: T0 })}
        reload={noop}
      />,
    )
    fireEvent.click(nodeNamed(bare.container, 'review'))
    const bareWf = bare.container.querySelector<HTMLElement>('.wf-inspect-head .wf-inspect-wf')!
    expect(bareWf.querySelector('.wf-inspect-label')).toBeNull()
    expect(bareWf.textContent).toBe('wf_5e5ad3b6f7da4299a839')
  })
})

describe('#330: the graph’s details panel opens on the right', () => {
  pinTheClock()

  function opened() {
    const { w, tasks } = labelled('workflows-board')
    const r = render(
      <WorkflowCard
        workflow={w}
        taskById={tasks}
        usage={{ kind: 'ready', usage: null }}
        openStages={{}}
        onToggleStage={noop}
        loadAttempts={async () => ({ status: 'empty', fetchedAt: T0 })}
        reload={noop}
      />,
    )
    const node = nodeNamed(r.container, 'review')
    node.focus()
    fireEvent.click(node)
    return { ...r, node }
  }

  it('renders the panel in a right-hand column beside the graph, not under it', () => {
    const { container } = opened()
    const split = container.querySelector<HTMLElement>('.wf-split')
    expect(split, 'the graph is not in a split').toBeTruthy()
    expect(split!.className).toContain('has-panel')
    const cols = [...split!.children] as HTMLElement[]
    expect(cols.map((c) => c.className)).toEqual(['wf-split-main', 'wf-panel'])
    // The graph is in the left column and the inspector in the right.
    expect(cols[0]!.querySelector('.wf-canvas')).toBeTruthy()
    expect(cols[1]!.querySelector('.wf-inspect')).toBeTruthy()
    expect(cols[0]!.querySelector('.wf-inspect'), 'the inspector is still under the canvas').toBeNull()
    expect(cols[1]!.getAttribute('aria-label')).toBe('Step details')

    // AND THE SHIPPED SHEET PUTS IT THERE: two columns at 1440, the second
    // a fixed-width track; the panel sticks and scrolls on its own.
    const at = (el: Element, prop: string, width: number) =>
      cascade(SHEETS.dark, el, prop, { width, theme: 'dark' }).winner?.value ?? null
    expect(at(split!, 'display', 1440)).toBe('grid')
    const tracks = splitTop(at(split!, 'grid-template-columns', 1440) ?? '', ' ').filter((t) => t !== '')
    expect(tracks).toHaveLength(2)
    expect(tracks[0]).toBe('minmax(0, 1fr)')
    expect(at(cols[1]!, 'position', 1440)).toBe('sticky')
    expect(at(cols[1]!, 'overflow-y', 1440)).toBe('auto')
    // At phone width it is a bottom sheet instead.
    expect(at(split!, 'display', 390)).toBe('block')
    expect(at(cols[1]!, 'position', 390)).toBe('fixed')
    expect(at(cols[1]!, 'bottom', 390)).toBe('0')
    expect(at(cols[1]!, 'overflow-y', 390)).toBe('auto')

    // Closed, the graph has the whole width back.
    fireEvent.click(container.querySelector<HTMLButtonElement>('.wf-inspect-close')!)
    expect(container.querySelector('.wf-panel')).toBeNull()
    expect(container.querySelector('.wf-split')!.className).toBe('wf-split')
  })

  it('takes focus on opening, closes on Escape, and gives focus back to the node', () => {
    const { container, node } = opened()
    const panel = container.querySelector<HTMLElement>('.wf-panel')!
    expect(panel.contains(document.activeElement), 'focus did not move into the panel').toBe(true)
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(container.querySelector('.wf-panel'), 'Escape did not close the panel').toBeNull()
    expect(container.querySelector('.wf-inspect')).toBeNull()
    expect(document.activeElement, 'focus did not return to the node').toBe(nodeNamed(container, 'review'))
    expect(node.getAttribute('aria-pressed')).toBe('false')
  })

  it('gives focus back to the node from the close control too', () => {
    const { container } = opened()
    fireEvent.click(container.querySelector<HTMLButtonElement>('.wf-panel .wf-inspect-close')!)
    expect(document.activeElement).toBe(nodeNamed(container, 'review'))
  })
})

// #330 ITEM 4: A 3-STEP CHAIN AND A 20-STEP FAN-OUT FIT A 1440x900 VIEWPORT.
// `CANVAS_COLUMN` is the canvas's measured width at that viewport and
// `CANVAS_HEIGHT` the height left under the open card's own chrome; the graph
// at its Auto tier fits inside both, with every card still saying its step,
// its state and its one-line cause.
describe('#330 item 4: smaller Graph nodes', () => {
  /** plan -> `n` shards -> join. Every step joined to a task, so every card
   *  draws a state and the running ones a stop control; one shard FAILED with
   *  an error, so its card draws a note. */
  function fan(n: number, join = true): { w: Workflow; tasks: Map<string, Task> } {
    const tasks = new Map<string, Task>()
    const at = (id: string, state: TaskState, err: string | null = null) => {
      tasks.set(`t_${id}`, task(`t_${id}`, state, { started_at: iso(-90_000), completed_at: state === 'FAILED' ? iso(-30_000) : null, last_error: err }))
      return `t_${id}`
    }
    const shards = Array.from({ length: n }, (_, i) => `shard-${String(i + 1).padStart(2, '0')}`)
    const steps: WorkflowStep[] = [step('plan', [], { task_id: at('plan', 'SUCCEEDED') })]
    shards.forEach((id, i) =>
      steps.push(step(id, ['plan'], { task_id: i === 4 ? at(id, 'FAILED', 'exit 1: the agent crashed on shard five') : at(id, 'RUNNING') })),
    )
    if (join) steps.push(step('join', shards))
    return { w: workflow('wf_fan', steps), tasks }
  }
  function chain(): { w: Workflow; tasks: Map<string, Task> } {
    const tasks = new Map<string, Task>([
      ['t_a', task('t_a', 'SUCCEEDED', { started_at: iso(-300_000), completed_at: iso(-200_000) })],
      ['t_b', task('t_b', 'FAILED', { started_at: iso(-200_000), completed_at: iso(-100_000), last_error: 'exit 1: tests failed' })],
    ])
    const steps = [
      step('design', [], { task_id: 't_a' }),
      step('implement', ['design'], { task_id: 't_b' }),
      step('review', ['implement']),
    ]
    return { w: workflow('wf_chain', steps), tasks }
  }
  const everyone = (w: Workflow) => new Set(w.steps.map((s) => s.step_id))
  /** The canvas fits the 1440x900 box, with no card noted and with every one. */
  function fitsOneScreen(w: Workflow, column = CANVAS_COLUMN): DagLayout {
    let checked = 0
    for (const noted of [new Set<string>(), everyone(w)]) {
      const tier = autoTier(w.steps, column, noted, column === CANVAS_COLUMN)
      const l = layoutOf(w.steps, undefined, tier, noted, column)
      expect(l.width, `${w.workflow_id} at ${tier}`).toBeLessThanOrEqual(column)
      expect(l.height, `${w.workflow_id} at ${tier}`).toBeLessThanOrEqual(CANVAS_HEIGHT)
      expect(l.bands, 'nothing is folded into a band').toHaveLength(0)
      expect(l.nodes).toHaveLength(w.steps.length)
      checked += 1
    }
    expect(checked).toBe(2)
    return layoutOf(w.steps, undefined, autoTier(w.steps, column, everyone(w)), everyone(w), column)
  }
  /** No two cards on the canvas overlap. */
  function noOverlap(l: DagLayout): void {
    let pairs = 0
    for (const a of l.nodes)
      for (const b of l.nodes) {
        if (a === b) continue
        const apart = a.x + l.nodeW <= b.x || b.x + l.nodeW <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y
        expect(apart, `${a.step.step_id} overlaps ${b.step.step_id}`).toBe(true)
        pairs += 1
      }
    expect(pairs).toBeGreaterThan(0)
  }

  it('shrinks the Names card, in the layout and in the stylesheet by the same numbers', () => {
    // 6 above + 34 stop strip + 2 border, then the name (21), the state (17.4)
    // and one --ctl-s1 between them. It was 97 on the full tier's chrome.
    expect(nodeHeightAt('names')).toBe(Math.ceil(6 + 34 + 2 + 14 * 1.5 + 12 * 1.45 + 4))
    expect(nodeHeightAt('names')).toBeLessThan(Math.ceil(10 + 42 + 2 + 14 * 1.5 + 12 * 1.45 + 4))
    const { w, tasks } = fan(18)
    const { container } = card(w, tasks, 'names')
    // EIGHTEEN SHARDS DO NOT FIT ONE ROW EVEN AT NAMES (a level never wraps,
    // owner decision 2026-09-30), so the stage is a band, closed, until it is
    // opened -- exactly like `wideStage`'s bands elsewhere in this file. This
    // case is about the CSS a Names-tier card resolves to, not about banding,
    // so open it the same way `openEveryBand` does before reading a card off
    // the canvas.
    expect(openEveryBand(container)).toBe(1)
    const node = nodeNamed(container, 'shard-01')
    expect(node.classList.contains('zoom-names')).toBe(true)
    expect(cascade(STYLES, node, 'padding', { width: 1440 }).winner?.value).toBe('6px 12px 34px')
    const stop = slotOf(container, 'shard-01').querySelector('.node-stop')!
    expect(stop, 'a running card keeps its stop control at Names').toBeTruthy()
    expect(cascade(STYLES, stop, 'bottom', { width: 1440 }).winner?.value).toBe('4px')
    // The slot is given exactly the height the tier claims.
    expect(Number.parseFloat(slotOf(container, 'plan').style.height)).toBe(nodeHeightAt('names'))
  })

  it('fits a 3-step chain on one screen, at the tier that keeps the most fields', () => {
    const { w, tasks } = chain()
    // Non-vacuous: at Figures three cards and two gaps are taller than the screen.
    expect(layoutOf(w.steps, undefined, 'figures').height).toBeGreaterThan(CANVAS_HEIGHT)
    const l = fitsOneScreen(w)
    expect(l.tier).toBe('details')
    // Beside the open details panel too: a chain is one card wide.
    fitsOneScreen(w, CANVAS_COLUMN - PANEL_COLUMN)
    const { container } = card(w, tasks)
    expect(container.querySelectorAll('.node')).toHaveLength(3)
    for (const id of ['design', 'implement', 'review']) {
      const n = nodeNamed(container, id)
      expect(n.querySelector('.node-name')!.textContent).toBe(id)
      expect(n.querySelector('.node-state')!.textContent).not.toBe('')
    }
    // The failed step's one-line cause is on its card, and the whole of it is
    // the line's title and the card's description on focus.
    const note = nodeNamed(container, 'implement').querySelector('.node-note')!
    expect(note.getAttribute('title')).toContain('exit 1: tests failed')
    expect(slotOf(container, 'implement').querySelector('.node-note-full')!.textContent).toContain('exit 1: tests failed')
  })

  it('keeps a 12-sibling level on one row instead of wrapping it (owner decision 2026-09-30)', () => {
    // A WRAPPED LEVEL READS AS TWO DEPENDENCY LEVELS, which this canvas may
    // not draw -- so semantic zoom sheds detail tiers first, and the canvas
    // scrolls sideways second; it never reflows a level onto a second row.
    // Opened (`new Set([1])`) so the level actually draws its cards instead of
    // collapsing into its band -- the math under test is the same either way.
    const { w } = fan(12, false)
    const l = layoutOf(w.steps, new Set([1]), 'names')
    const shardNodes = l.nodes.filter((n) => n.level === 1)
    expect(shardNodes).toHaveLength(12)
    // ONE ROW: every sibling shares the level's own y.
    expect(new Set(shardNodes.map((n) => n.y)).size, 'the level split onto more than one row').toBe(
      1,
    )
    // AND THE PITCH IS ARITHMETIC: x is `pos * (nodeW + SIB_GAP)`, not a
    // row/column pair, so it never resets partway through the level.
    const byPos = [...shardNodes].sort((a, b) => a.pos - b.pos)
    for (let i = 1; i < byPos.length; i++) {
      expect(byPos[i]!.x - byPos[i - 1]!.x).toBe(l.nodeW + SIB_GAP)
    }
    // NON-VACUOUS: twelve does not fit `CANVAS_COLUMN` in one row at Names --
    // the narrowest tier -- so the only way every assertion above holds is
    // that the level really did stay one (wider-than-the-column) row.
    expect(stageFitsAt(l.nodeW)).toBeLessThan(12)
    expect(l.width).toBeGreaterThan(CANVAS_COLUMN)
  })

  it("scrolls the canvas horizontally, instead of wrapping, when an opened stage is too wide even at Names", () => {
    // NAMES IS THE NARROWEST TIER (#330 item 4). A stage still too wide for
    // one row there stays a band until the reader opens it, and open it is
    // drawn at its real width -- which may run past `column` -- rather than
    // wrapped into rows. `.wf-canvas-wrap` is `overflow-x: auto` for exactly
    // this (WF-9 already proves it scrolls an opened band; this is the Names
    // case #330 item 4 added).
    const { w, tasks } = fan(13, false)
    expect(autoTier(w.steps)).toBe('figures')
    const l = layoutOf(w.steps, new Set([1]), 'names')
    expect(l.wide[1]).toBe(true)
    expect(l.width, 'thirteen cards at Names still overflow the column').toBeGreaterThan(
      CANVAS_COLUMN,
    )
    noOverlap(l)

    const { container } = card(w, tasks, 'names')
    expect(openEveryBand(container)).toBe(1)
    const wrap = container.querySelector<HTMLElement>('.wf-canvas-wrap')!
    const r = cascade(STYLES, wrap, ['overflow-x', 'overflow'], { width: 1440 })
    expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
    expect(r.winner?.value).toBe('auto')
    const canvas = container.querySelector<HTMLElement>('.wf-canvas')!
    expect(Number.parseFloat(canvas.style.width)).toBeGreaterThan(CANVAS_COLUMN)
    expect(container.querySelectorAll('.node')).toHaveLength(14)
  })

  it('stays a band at every tier when a fan-out never fits one row, and opening it still shows every card', () => {
    // A FAN-OUT in this file's own words (`shapeOf`): one step opening into
    // nineteen, twenty steps -- too wide for one row at any tier, so it is one
    // band and one node everywhere rather than wrapped into rows at Names.
    const { w, tasks } = fan(19, false)
    expect(w.steps).toHaveLength(20)
    expect(shapeOf(w.steps).kind).toBe('fan-out')
    for (const tier of ['figures', 'details', 'names'] as const) {
      const l = layoutOf(w.steps, undefined, tier)
      expect(l.bands, tier).toHaveLength(1)
      expect(l.nodes, tier).toHaveLength(1)
      // Collapsed, every edge into the stage is one path at every tier -- none
      // of them draws it in full without being opened.
      const kinds = edgeKinds(l, new Map())
      const into = l.edges.filter((e) => e.from === 'plan')
      expect(new Set(into.map((e) => kinds.get(`${e.from}->${e.to}`))).size, tier).toBe(1)
    }
    expect(autoTier(w.steps)).toBe('figures')

    const { container } = card(w, tasks)
    expect(openEveryBand(container)).toBe(1)
    expect(container.querySelectorAll('.node')).toHaveLength(20)
    for (const n of container.querySelectorAll('.node')) {
      expect(n.querySelector('.node-name')!.textContent).not.toBe('')
      expect(n.querySelector('.node-state')!.textContent).not.toBe('')
    }
    const note = nodeNamed(container, 'shard-05').querySelector('.node-note')!
    expect(note.getAttribute('title')).toContain('exit 1: the agent crashed on shard five')
    // Opened, the canvas is wider than the column and scrolls to show it,
    // rather than wrapping the stage to stay inside it.
    const canvas = container.querySelector<HTMLElement>('.wf-canvas')!
    expect(Number.parseFloat(canvas.style.width)).toBeGreaterThan(CANVAS_COLUMN)
  })

  it('lays the graph out beside the open panel, and never folds the picked step into a band', () => {
    // THREE FITS THE FULL COLUMN AT `figures` (`STAGE_FITS`) BUT NOT THE
    // COLUMN LEFT BESIDE THE PANEL AT `figures` -- so opening the panel would
    // fold this stage into a band if `autoTier` did not zoom to a narrower
    // tier first (#330 item 5). Computed from `stageFitsAt` and `NODE_W`
    // rather than asserted as a literal, so the case stays a genuine boundary
    // if either moves.
    const narrowColumn = CANVAS_COLUMN - PANEL_COLUMN
    expect(STAGE_FITS).toBe(3)
    expect(stageFitsAt(NODE_W, narrowColumn), 'the case needs a narrower tier to fit beside the panel').toBeLessThan(
      3,
    )
    const { w, tasks } = fan(3, false)
    const { container } = card(w, tasks)
    expect(container.querySelector('.wf-band')).toBeNull()
    fireEvent.click(nodeNamed(container, 'shard-01'))
    expect(container.querySelector('.wf-split.has-panel .wf-panel')).toBeTruthy()
    // The graph narrowed to the column left beside the panel and zoomed to a
    // tier that still draws every card, rather than folding the picked step's
    // stage into a band because the panel opened.
    const canvas = container.querySelector<HTMLElement>('.wf-canvas')!
    expect(Number.parseFloat(canvas.style.width)).toBeLessThanOrEqual(narrowColumn)
    expect(container.querySelector('.wf-band')).toBeNull()
    expect(container.querySelectorAll('.node')).toHaveLength(4)
    expect(nodeNamed(container, 'shard-01').getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('.wf-canvas')!.closest('.wf-split-main')).toBeTruthy()
    // Closed, the graph has the whole column back.
    fireEvent.keyDown(container.querySelector('.wf-panel')!, { key: 'Escape' })
    expect(container.querySelector('.wf-panel')).toBeNull()
    expect(Number.parseFloat(container.querySelector<HTMLElement>('.wf-canvas')!.style.width)).toBeGreaterThan(
      narrowColumn,
    )
  })
})
