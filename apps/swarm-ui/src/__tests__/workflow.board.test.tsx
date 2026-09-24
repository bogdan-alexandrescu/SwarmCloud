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
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'

import { WorkflowCard, WorkflowsScreen } from '../Workflows'
import {
  CANVAS_COLUMN,
  NODE_W,
  SIB_GAP,
  STAGE_FITS,
  layoutOf,
  shapeOf,
  stageCensus,
  stepDuration,
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
}: {
  workflow: Workflow
  taskById: Map<string, Task> | null
  expanded: boolean
  cardKey?: number
}) {
  const [stages, setStages] = useState<Record<string, boolean>>({})
  return (
    <WorkflowCard
      key={cardKey}
      workflow={workflow}
      taskById={taskById}
      expanded={expanded}
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

function card(w: Workflow, taskById: Map<string, Task> | null = new Map(), expanded = false) {
  return render(<CardHarness workflow={w} taskById={taskById} expanded={expanded} />)
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

describe('how long a step has taken', () => {
  pinTheClock()

  it('shows an ABSENCE for a step that has not started, and never 0s', () => {
    const { w, tasks } = timings()
    const { container } = card(w, tasks, true)
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
    const { container } = card(w, tasks, true)
    const dur = nodeNamed(container, 'instant').querySelector('.node-dur')!
    expect(dur.textContent).toBe('ran 0s')
    expect(dur.className).toContain('is-ran')
  })

  it('keeps waiting, running, parked and finished as four different things', () => {
    const { w, tasks } = timings()
    const { container } = card(w, tasks, true)
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
    const { container } = card(w, new Map(), true)
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
    // The five parallel steps are one stage over `STAGE_FITS`, so the canvas
    // lands with that stage as a band. The nodes are what this test is about,
    // so open it -- and check that there WAS one to open, or every assertion
    // below would be passing over an empty graph.
    expect(openEveryBand(container)).toBe(1)

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
    // NOT EXPANDED, DELIBERATELY. `fanOut`'s middle stage is five steps, one
    // over `STAGE_FITS`, so this renders with that stage as a band -- and all
    // ten dependencies still draw. That is the half of stage collapsing which
    // is easiest to lose: the first implementation of it found edges by walking
    // the NODES, and a collapsed stage has none, so every edge into or out of
    // it vanished. Both endpoints resolve through the step, and a step in a
    // band attaches to the band.
    const { container } = card(fanOut(), new Map(), true)
    const edges = container.querySelectorAll('.wf-edge')
    expect(edges).toHaveLength(10)
    for (const e of edges) expect(e.getAttribute('d')).toMatch(/^M [\d.]+ [\d.]+ C /)
    // Flow direction is drawn, not implied: every edge ends in an arrowhead.
    expect(container.querySelector('marker')).toBeTruthy()
    for (const e of edges) expect(e.getAttribute('marker-end')).toContain('url(#arrow-')
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
    expect(openEveryBand(container)).toBe(1)
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
    // Opened, because the property under test is about where the five parallel
    // steps SIT, and collapsing is not allowed to change that answer: an
    // expanded stage draws exactly the horizontal row it drew before this
    // feature existed. The owner's instruction is the layout, not the default.
    expect(openEveryBand(container)).toBe(1)
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

  it('leaves a stage that fits alone, and collapses the one step past it', () => {
    const fits = wideStage(STAGE_FITS)
    const a = card(fits.w, fits.tasks, true)
    expect(a.container.querySelector('.wf-band')).toBeNull()
    expect(a.container.querySelectorAll('.node')).toHaveLength(STAGE_FITS + 2)

    // ONE MORE STEP AND IT DOES NOT FIT. The threshold is a property of NODE_W
    // and the column, not of a fixture: this pins that the boundary is exactly
    // where `STAGE_FITS` says it is, so moving NODE_W moves the test with it
    // rather than leaving a stale number behind (which is how
    // DEP_CHARS_PER_LINE went wrong).
    const over = wideStage(STAGE_FITS + 1)
    const b = card(over.w, over.tasks, true)
    expect(b.container.querySelector('.wf-band')).toBeTruthy()
    expect(b.container.querySelectorAll('.node')).toHaveLength(2)
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
    const { container } = card(w, tasks, true)
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
