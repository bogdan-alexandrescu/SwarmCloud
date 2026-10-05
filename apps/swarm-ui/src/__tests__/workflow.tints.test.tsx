// #405, AS THE OWNER DECIDED IT ON 2026-10-01: ONE STATE VOCABULARY FOR A STEP.
//
// #405 asked for step tints by state (light green, light blue, cream, light
// red, grey). The owner's comment of 2026-10-01 superseded those hues with the
// brand's (workflows.html D, "Step tints"): a pale tint of the state's hue
// across the step and a stronger edge in that hue -- teal for the four states
// that hold capacity, violet for parked, red for failed and dead-lettered,
// grey for the rest -- on graph nodes, minimap nodes, table rows and timeline
// spans, from one token pair per hue, in light and dark. A step whose task was
// not read stays the dashed amber, never a state's hue.
//
// The rebrand already drew the graph NODE that way (`.node.t-<hue>`, from
// `--s-<hue>b` / `--s-<hue>`). This file holds the views it had not reached:
// the Table's rows and the Timeline's spans (the minimap is held in
// workflow.board.test.tsx, beside the map's own case), and that every view
// draws a state from the SAME pair.
//
// Colours are read through `cssgate`'s `cascade` over the shipped sheets IN
// THE BUNDLE'S ORDER (styles/workflows.css first, then styles.css) with every
// token substituted, once per theme.

import STYLES from '../styles.css?raw'
import WF_CSS from '../styles/workflows.css?raw'
import { describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AttemptRow, Task, TaskState, Workflow, WorkflowStep } from '../types'
import { STATE_MARK, type MarkHue } from '../marks'
import { cascade } from './cssgate'
import { colour, contrast, resolveSheet, resolveVars, tokenTables, type RGBA } from './spaceprobe'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadAttempts: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowCard } = await import('../Workflows')
const { lookClass, stepLook } = await import('../dag')

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const T0 = Date.parse('2026-09-24T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

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
    created_at: iso(-600),
    updated_at: iso(-60),
    started_at: null,
    completed_at: null,
    submitted_by: 'bogdan@saga.xyz',
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

function workflow(steps: WorkflowStep[]): Workflow {
  return {
    workflow_id: 'wf_tints',
    state: 'RUNNING',
    tenant_id: 'u-bogdan',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: {
      state: 'RUNNING',
      complete: true,
      reason: 'steps_hold_capacity',
      counts: { RUNNING: 1 },
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: steps.length,
    },
    created_at: iso(-600),
    updated_at: iso(-60),
    submitted_by: 'bogdan@saga.xyz',
    priority: 0,
    on_step_failure: 'CONTINUE',
    cancel_requested: false,
    steps,
  }
}

/** Every state a stored task can be in. SUBMITTED is never stored on a task (types.ts). */
const STORED: readonly TaskState[] = [
  'QUEUED',
  'READY',
  'PARKED',
  'LEASED',
  'DISPATCHED',
  'STARTING',
  'RUNNING',
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
  'DEAD_LETTERED',
]
const RAN: ReadonlySet<TaskState> = new Set(['RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED'])
const ENDED: ReadonlySet<TaskState> = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED'])

/**
 * ONE STEP PER STORED STATE, side by side under one root, plus a step with no
 * task yet (`waiting`, drawn as QUEUED) and one whose task was not in the read
 * (`lost`). A step that ran started 300s ago after a 300s wait; one that ended
 * did so 60s ago, so each has a wait span AND a run span on the Timeline.
 */
function fixture(chain = false): { w: Workflow; tasks: Map<string, Task> } {
  const tasks = new Map<string, Task>()
  const steps: WorkflowStep[] = [step('root', [])]
  let prev = 'root'
  for (const s of STORED) {
    const id = `t_${s.toLowerCase()}`
    tasks.set(
      id,
      task(id, s, {
        started_at: RAN.has(s) ? iso(-300) : null,
        completed_at: ENDED.has(s) ? iso(-60) : null,
      }),
    )
    steps.push(step(s.toLowerCase(), chain ? [prev] : [], { task_id: id }))
    prev = s.toLowerCase()
  }
  steps.push(step('waiting', ['root']))
  steps.push(step('lost', [], { task_id: 't_lost' }))
  // `root` succeeded, so `waiting` is a step whose parents are done.
  tasks.set('t_root', task('t_root', 'SUCCEEDED', { started_at: iso(-590), completed_at: iso(-500) }))
  steps[0] = step('root', [], { task_id: 't_root' })
  return { w: workflow(steps), tasks }
}

/**
 * `chain` puts each state's step one level below the last, for the Graph: side
 * by side, eleven steps are a wide stage the graph collapses into one band, and
 * a band draws no node to read a tint off.
 */
function viewCard(view: 'graph' | 'timeline' | 'table', chain = false): HTMLElement {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  const { w, tasks } = fixture(chain)
  const load = async (): Promise<Result<{ attempts: AttemptRow[] }>> => ({ status: 'empty', fetchedAt: T0 })
  const { container } = render(
    <WorkflowCard
      workflow={w}
      taskById={tasks}
      usage={{ kind: 'ready', usage: null }}
      openStages={{}}
      onToggleStage={() => {}}
      view={view}
      loadAttempts={load}
      reload={() => {}}
    />,
  )
  return container as HTMLElement
}

// ---------------------------------------------------------------------------
// Reading paint
// ---------------------------------------------------------------------------

type Theme = 'dark' | 'light'
const THEMES: readonly Theme[] = ['dark', 'light']
/** The bundle's order: main.tsx imports Workflows.tsx (and its sheet) before styles.css. */
const SHEET = `${WF_CSS}\n${STYLES}`
const TOKENS = tokenTables(SHEET)
const SHEETS: Readonly<Record<Theme, string>> = { dark: resolveSheet(SHEET, 'dark'), light: resolveSheet(SHEET, 'light') }
const HUES: readonly MarkHue[] = ['live', 'park', 'bad', 'neu']

function tokenColour(name: string, theme: Theme): RGBA {
  const v = TOKENS[theme].get(name)
  expect(v, `${name} is not declared`).toBeDefined()
  const c = colour(resolveVars(v!, TOKENS[theme]))
  expect(c, `${name} does not resolve to a colour`).not.toBeNull()
  return c!
}

/** The first word of a declared value that reads as a colour. */
function colourIn(value: string): RGBA | null {
  for (const word of value.split(/\s+(?![^(]*\))/)) {
    if (word === '') continue
    const c = colour(word)
    if (c !== null) return c
  }
  return null
}

function paintOf(el: Element, props: readonly string[], theme: Theme, pseudo: string | null = null): RGBA | null {
  const r = cascade(SHEETS[theme], el, props, { width: 1440, theme }, pseudo)
  return r.winner === null ? null : colourIn(r.winner.value)
}

const sameColour = (a: RGBA | null, b: RGBA): boolean =>
  a !== null && Math.abs(a.r - b.r) < 0.5 && Math.abs(a.g - b.g) < 0.5 && Math.abs(a.b - b.b) < 0.5

const BG = ['background', 'background-color'] as const
const EDGE = ['box-shadow'] as const

/** The tint classes on an element: exactly one is a state's, or `t-unk`. */
const tints = (el: Element) => [...el.classList].filter((c) => /^t-/.test(c))

/** Which hue's pair a painted (bg, edge) is, or null if it is no state's pair. */
function pairOf(bg: RGBA | null, edge: RGBA | null, theme: Theme): MarkHue | null {
  const hits = HUES.filter(
    (h) => sameColour(bg, tokenColour(`--s-${h}b`, theme)) && sameColour(edge, tokenColour(`--s-${h}`, theme)),
  )
  return hits.length === 1 ? hits[0]! : null
}

// ---------------------------------------------------------------------------
// One vocabulary
// ---------------------------------------------------------------------------

describe('#405: every state maps to exactly one tint, from one token pair per hue', () => {
  it('gives each state one tint class, and the four capacity-holding states the live one', () => {
    for (const s of STORED) {
      const look = stepLook({ kind: 'state', state: s, task: task('t', s) })
      expect(lookClass(look), s).toBe(`t-${STATE_MARK[s].hue}`)
    }
    // The control: the tints are NOT all one class, so "maps to exactly one"
    // could have come out as "maps to the same one".
    const used = new Set(STORED.map((s) => STATE_MARK[s].hue))
    expect([...used].sort()).toEqual(['bad', 'live', 'neu', 'park'])
    expect(STORED.filter((s) => STATE_MARK[s].hue === 'live')).toEqual(['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING'])
  })

  it('declares each pair in both themes, the edge at 3:1 on its tint and the text at 4.5:1', () => {
    for (const theme of THEMES) {
      for (const h of HUES) {
        const bg = tokenColour(`--s-${h}b`, theme)
        const edge = tokenColour(`--s-${h}`, theme)
        expect(contrast(edge, bg), `${theme} ${h}: edge on its tint`).toBeGreaterThanOrEqual(3)
        expect(contrast(tokenColour('--text', theme), bg), `${theme} ${h}: text on its tint`).toBeGreaterThanOrEqual(4.5)
      }
      // DARK IS DEEP TINTS, NOT WASHED-OUT PASTELS: each dark tint is darker
      // than its light-theme twin. The control is that the two differ at all.
      if (theme === 'dark') {
        for (const h of HUES) {
          const d = tokenColour(`--s-${h}b`, 'dark')
          const l = tokenColour(`--s-${h}b`, 'light')
          expect(contrast(d, { r: 0, g: 0, b: 0, a: 1 }), `${h}: dark tint is not deep`).toBeLessThan(
            contrast(l, { r: 0, g: 0, b: 0, a: 1 }),
          )
        }
      }
    }
  })
})

// ---------------------------------------------------------------------------
// The Table
// ---------------------------------------------------------------------------

describe('#405: the Table tints each step row in its state’s pair', () => {
  const rowOf = (root: HTMLElement, id: string) => {
    const r = root.querySelector<HTMLElement>(`.wf-table tbody tr[data-step="${id}"]`)
    expect(r, `no table row for ${id}`).not.toBeNull()
    return r!
  }

  it('tints every row: the tint across its cells and the edge down its first one, in both themes', () => {
    const root = viewCard('table')
    let visited = 0
    for (const s of STORED) {
      const row = rowOf(root, s.toLowerCase())
      const hue = STATE_MARK[s].hue
      expect(tints(row), s).toEqual([`t-${hue}`])
      const cells = [...row.querySelectorAll(':scope > td')]
      expect(cells.length, `${s}: no cells`).toBeGreaterThan(2)
      for (const theme of THEMES) {
        // A middle cell carries the tint; the first carries the tint AND the edge.
        expect(sameColour(paintOf(cells[2]!, BG, theme), tokenColour(`--s-${hue}b`, theme)), `${theme} ${s}: cell tint`).toBe(true)
        expect(pairOf(paintOf(cells[0]!, BG, theme), paintOf(cells[0]!, EDGE, theme), theme), `${theme} ${s}: first cell`).toBe(hue)
      }
      visited += 1
    }
    expect(visited).toBe(STORED.length)
    // A step with no task yet is the QUEUED ring: grey, in line.
    expect(tints(rowOf(root, 'waiting'))).toEqual(['t-neu'])
  })

  it('keeps an unread step out of every state’s hue: amber, not a guessed state', () => {
    const root = viewCard('table')
    const lost = rowOf(root, 'lost')
    expect(tints(lost)).toEqual(['t-unk'])
    expect(lost.classList.contains('is-warn')).toBe(true)
    const first = lost.querySelector(':scope > td')!
    for (const theme of THEMES) expect(pairOf(paintOf(first, BG, theme), paintOf(first, EDGE, theme), theme)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The Timeline
// ---------------------------------------------------------------------------

describe('#405: the Timeline tints each run span in its step’s pair; a wait stays hueless', () => {
  const trackOf = (root: HTMLElement, id: string) => {
    const t = root.querySelector<HTMLElement>(`.wf-tl-track[data-step="${id}"]`)
    expect(t, `no track for ${id}`).not.toBeNull()
    return t!
  }

  it('tints the run of every step that ran, in both themes', () => {
    const root = viewCard('timeline')
    let visited = 0
    for (const s of STORED.filter((x) => RAN.has(x))) {
      const run = trackOf(root, s.toLowerCase()).querySelector<HTMLElement>('.wf-tl-span.is-ran, .wf-tl-span.is-running')
      expect(run, `${s} has no run span`).not.toBeNull()
      const hue = STATE_MARK[s].hue
      expect(tints(run!), s).toEqual([`t-${hue}`])
      for (const theme of THEMES) {
        expect(pairOf(paintOf(run!, BG, theme), paintOf(run!, EDGE, theme), theme), `${theme} ${s}: run span`).toBe(hue)
      }
      visited += 1
    }
    expect(visited).toBe(RAN.size)
  })

  it('keeps the failed run’s post, in the brand red', () => {
    const root = viewCard('timeline')
    const run = trackOf(root, 'failed').querySelector<HTMLElement>('.wf-tl-span.is-ran')!
    for (const theme of THEMES) {
      expect(sameColour(paintOf(run, BG, theme, 'before'), tokenColour('--s-bad', theme)), theme).toBe(true)
    }
    // The control: a succeeded run has no post at all.
    const ok = trackOf(root, 'succeeded').querySelector<HTMLElement>('.wf-tl-span.is-ran')!
    expect(cascade(SHEETS.dark, ok, 'content', { width: 1440, theme: 'dark' }, 'before').winner).toBeNull()
  })

  it('draws a wait in no state’s hue: waiting is not the step’s verdict', () => {
    const root = viewCard('timeline')
    const waits = [...root.querySelectorAll<HTMLElement>('.wf-tl-span.is-waited, .wf-tl-span.is-waiting')]
    expect(waits.length).toBeGreaterThan(5)
    for (const w of waits) {
      expect(tints(w)).toEqual([])
      for (const theme of THEMES) expect(pairOf(paintOf(w, BG, theme), paintOf(w, EDGE, theme), theme)).toBeNull()
    }
  })
})

// ---------------------------------------------------------------------------
// The Graph: what the rebrand already shipped, held from the same pair
// ---------------------------------------------------------------------------

describe('#405: the Graph node draws the same pair as the Table and the Timeline', () => {
  it('tints each node from the pair its row and span use', () => {
    const root = viewCard('graph', true)
    const nodes = [...root.querySelectorAll<HTMLElement>('.node')]
    let visited = 0
    for (const s of STORED) {
      const node = nodes.find((n) => n.querySelector('.node-name')?.textContent === s.toLowerCase())
      if (node === undefined) continue
      const hue = STATE_MARK[s].hue
      expect(tints(node), s).toEqual([`t-${hue}`])
      for (const theme of THEMES) {
        expect(sameColour(paintOf(node, BG, theme), tokenColour(`--s-${hue}b`, theme)), `${theme} ${s}`).toBe(true)
        expect(
          sameColour(paintOf(node, ['border-left', 'border-left-color'], theme), tokenColour(`--s-${hue}`, theme)),
          `${theme} ${s}: edge`,
        ).toBe(true)
      }
      visited += 1
    }
    // A collapsed band would hide nodes and turn this into a vacuous pass.
    expect(visited).toBe(STORED.length)
  })
})
