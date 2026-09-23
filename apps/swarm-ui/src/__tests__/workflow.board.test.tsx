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
import { shapeOf, stepDuration } from '../dag'
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

function card(w: Workflow, taskById: Map<string, Task> | null = new Map(), expanded = false) {
  return render(
    <WorkflowCard
      workflow={w}
      taskById={taskById}
      expanded={expanded}
      // `ready` with a null usage is "the attempt read landed and this board
      // was outside its sample", which is the state these cases are about --
      // not `reading`, which would put every figure behind a placeholder and
      // make the assertions below pass for the wrong reason.
      usage={{ kind: 'ready', usage: null }}
      onToggle={noop}
      reload={noop}
    />,
  )
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

  it('draws NO meter over a rollup the server could not complete', () => {
    // A meter is a claim that the numbers behind it are a census. When the
    // census failed, the words survive struck through and the bar does not get
    // drawn at all -- a two-thirds-full meter over a failed read is the exact
    // substitution this console exists to refuse.
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
    expect(container.querySelector('.wf-meter')).toBeNull()
    expect(container.querySelector('.wf-progress.untrusted')!.textContent).toBe(
      '2 of 3 steps: state unread',
    )
  })

  it('opens on click and closes again', () => {
    function Harness() {
      const [open, setOpen] = useState(false)
      return (
        <WorkflowCard
          workflow={fanOut()}
          taskById={new Map()}
          expanded={open}
        usage={{ kind: 'ready', usage: null }}
          onToggle={(_id, was) => setOpen(!was)}
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

  it('opens each step at the address the rest of the console uses for it', () => {
    const w = fanOut()
    const steps = w.steps.map((s, i) => (i === 1 ? { ...s, task_id: 'task_scan_a' } : s))
    const tasks = new Map([['task_scan_a', task('task_scan_a', 'RUNNING')]])
    const { container } = card({ ...w, steps }, tasks, true)
    const link = nodeNamed(container, 'scan-a').querySelector('a')!
    expect(link.getAttribute('href')).toBe('#agents/task/task_scan_a')
    // A step with no task has nothing to open, so it is not a dead link.
    expect(nodeNamed(container, 'report').querySelector('a')).toBeNull()
  })

  it('lays the columns out in dependency order, left to right', () => {
    const { container } = card(fanOut(), new Map(), true)
    const left = (name: string) =>
      Number.parseFloat(nodeNamed(container, name).style.left)
    expect(left('plan')).toBeLessThan(left('scan-a'))
    expect(left('scan-a')).toBeLessThan(left('report'))
    // The five parallel steps share a column and differ only in row.
    const scans = ['scan-a', 'scan-b', 'scan-c', 'scan-d', 'scan-e']
    expect(new Set(scans.map(left)).size).toBe(1)
    expect(new Set(scans.map((s) => nodeNamed(container, s).style.top)).size).toBe(5)
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
