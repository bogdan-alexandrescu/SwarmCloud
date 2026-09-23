// The workflow board's arithmetic: shape, layout, timing and spend.
//
// PURE ON PURPOSE. No React, no DOM, no dates read from the clock -- `now` is
// always a parameter. `Workflows.tsx` is then only pixels, and every claim this
// board makes can be asserted without rendering anything. It is the same split
// `charts/series.ts` makes for the charts, and for the same reason: the hard
// part of this screen is deciding WHAT IS KNOWN, and that decision should not
// be reachable only through a component tree.
//
// THE RULE THIS FILE EXISTS TO ENFORCE. An absent measurement must never
// become a number, and a measured zero must stay a digit. Both halves have a
// type here rather than a convention:
//
//   * `StepDuration`'s `none` arm carries NO `seconds` field, so a renderer
//     that tries to print a number for a step that never started does not
//     compile. A step with no task has no duration; `0s` would be a claim that
//     it ran instantly.
//   * `WorkflowSpend.usd` is `null` when nothing reported a cost, and a
//     reported `0` counts as MEASURED and comes back as `0`. "No attempt
//     reported a cost" and "this cost nothing" are different sentences.

import {
  TERMINAL_STATES,
  formatDuration,
  type StepState,
  type Task,
  type WorkflowStep,
  usageOf,
} from './types'

// ---------------------------------------------------------------------------
// Depth
// ---------------------------------------------------------------------------

/**
 * Depth of each step: 0 for roots, else 1 + max(depth of dependencies).
 *
 * Moved here from `Workflows.tsx` unchanged. A cycle should be impossible --
 * the scheduler rejects one at submission -- but a UI that hangs on malformed
 * data is worse than one that draws it flat, so this terminates rather than
 * trusting that.
 */
export function levelsOf(steps: readonly WorkflowStep[]): WorkflowStep[][] {
  const byId = new Map(steps.map((s) => [s.step_id, s]))
  const depth = new Map<string, number>()

  const resolve = (id: string, seen: Set<string>): number => {
    const cached = depth.get(id)
    if (cached !== undefined) return cached
    if (seen.has(id)) return 0
    const step = byId.get(id)
    if (!step || step.depends_on.length === 0) {
      depth.set(id, 0)
      return 0
    }
    seen.add(id)
    const d = 1 + Math.max(...step.depends_on.map((p) => resolve(p, seen)))
    seen.delete(id)
    depth.set(id, d)
    return d
  }

  steps.forEach((s) => resolve(s.step_id, new Set()))
  if (steps.length === 0) return []
  const max = Math.max(0, ...steps.map((s) => depth.get(s.step_id) ?? 0))
  return Array.from({ length: max + 1 }, (_, lvl) =>
    steps.filter((s) => (depth.get(s.step_id) ?? 0) === lvl),
  )
}

// ---------------------------------------------------------------------------
// Shape -- the topology, compressed enough to survive a collapsed row
// ---------------------------------------------------------------------------

/**
 * What kind of graph this is, in one word.
 *
 * THIS IS THE DEFECT THE GRAPH WORK EXISTS TO FIX, and it has to survive being
 * collapsed. Five steps that run in parallel and five steps that run one after
 * another are the same count, the same progress fraction and the same spend;
 * they are completely different runs. A reader choosing which of ten workflows
 * to open must be able to tell them apart without opening any of them.
 */
export type DagKind = 'empty' | 'single' | 'chain' | 'fan-out' | 'fan-in' | 'diamond' | 'mixed'

export interface DagShape {
  /** Steps per dependency level, in order. `[1, 5, 1]` is a fan-out and a join. */
  readonly widths: readonly number[]
  readonly kind: DagKind
  /** `1 → 5 → 1`. The one-line rendering. */
  readonly text: string
  /** A sentence for the `title`, e.g. "1 step fans out to 5, then joins into 1." */
  readonly label: string
  readonly levels: number
  readonly widest: number
  readonly steps: number
  /**
   * The step every parallel branch converges INTO, when there is exactly one.
   * Named rather than counted: "converge into 1" and "converge into synthesis"
   * cost the same line and only one of them can be acted on. null when the
   * last level holds more than one step, because then there is no single join
   * to name and inventing one would be a claim about the graph.
   */
  readonly joinStep: string | null
  /** The step the fan-out opens FROM, on the same terms. */
  readonly forkStep: string | null
}

export function shapeOf(steps: readonly WorkflowStep[]): DagShape {
  const levelList = levelsOf(steps)
  const widths = levelList.map((l) => l.length)
  const total = steps.length
  const firstLevel = levelList[0] ?? []
  const lastLevel = levelList[levelList.length - 1] ?? []
  const forkStep = firstLevel.length === 1 ? (firstLevel[0]?.step_id ?? null) : null
  const joinStep = lastLevel.length === 1 ? (lastLevel[0]?.step_id ?? null) : null
  const levels = widths.length
  const widest = widths.length === 0 ? 0 : Math.max(...widths)
  const text = widths.join(' → ')

  if (total === 0)
    return { widths, kind: 'empty', text: '—', label: 'This workflow has no steps.', levels, widest, steps: total, joinStep, forkStep }
  if (total === 1)
    return { widths, kind: 'single', text, label: 'A single step.', levels, widest, steps: total, joinStep, forkStep }
  if (widest === 1) {
    return {
      widths,
      kind: 'chain',
      text,
      label: `A chain: ${total} steps, each waiting for the one before it. Nothing here runs in parallel.`,
      levels,
      widest,
      steps: total,
      joinStep,
      forkStep,
    }
  }

  const first = widths[0] ?? 0
  const last = widths[widths.length - 1] ?? 0
  const kind: DagKind =
    first === 1 && last === 1 && levels >= 3
      ? 'diamond'
      : last === 1 && last < widest
        ? 'fan-in'
        : first === 1 && first < widest
          ? 'fan-out'
          : 'mixed'

  // The join and the fork are named where one exists. `${last}` alone said
  // "converge into 1", which is a count of something the reader cannot then
  // go and look at -- and naming the step is the whole difference between a
  // shape the collapsed row describes and a shape it merely measures.
  const into = joinStep ?? `${last}`
  const from = forkStep ?? `${first}`
  const label =
    kind === 'diamond'
      ? `A fan-out and a join: ${from} opens into ${widest} running in parallel, and they converge into ${into}.`
      : kind === 'fan-in'
        ? `A join: ${widest} steps at the widest point converge into ${into}.`
        : kind === 'fan-out'
          ? `A fan-out: ${from} opens into ${widest} running in parallel.`
          : `${total} steps over ${levels} levels, up to ${widest} in parallel.`

  return { widths, kind, text, label, levels, widest, steps: total, joinStep, forkStep }
}

// ---------------------------------------------------------------------------
// Layout -- a real canvas with real edges
// ---------------------------------------------------------------------------
//
// Flow is LEFT TO RIGHT: one column per dependency level, one row per step
// inside it. That is the direction the owner's reference reads in, and it is
// also the one that survives a wide level -- five parallel steps stack
// vertically inside a single column instead of pushing the page sideways.
//
// The numbers are computed here rather than measured in the browser so the
// layout is testable and so the first paint is not a reflow. A node is sized
// for its four facts -- name, status, runner profile, duration -- with room
// for the stop control underneath.

export const NODE_W = 212
/**
 * The height of a node with NO dependency line.
 *
 * Raised from 136 when the four run figures and the two deep links came back
 * onto the node. It is a real measurement of the fixed rows -- id, state,
 * runner profile, duration, four `node-num` rows, links -- and NOT a guess
 * with slack in it, because `heightOf` below adds the variable part exactly.
 */
export const NODE_H = 244

/** One wrapped line of the `← depends on` list, at --t-micro/--lh-micro. */
const DEP_LINE_H = 16
/** Characters of the dependency list that fit on one line inside NODE_W. */
const DEP_CHARS_PER_LINE = 34

/**
 * How tall THIS node will actually render.
 *
 * The layout positions nodes absolutely, so a node that renders taller than
 * the figure the layout used overlaps the node beneath it and every edge
 * endpoint on that column points at the wrong place. `minHeight` on the
 * element hid that: the node grew and nothing else moved. So the variable part
 * -- the dependency list, which wraps and is never truncated -- is measured
 * here, and the element is given this exact height rather than a floor.
 */
export function heightOf(step: WorkflowStep): number {
  if (step.depends_on.length === 0) return NODE_H
  const chars = step.depends_on.join(', ').length
  const lines = Math.max(1, Math.ceil(chars / DEP_CHARS_PER_LINE))
  return NODE_H + lines * DEP_LINE_H
}
export const COL_GAP = 68
export const ROW_GAP = 20
export const PAD = 10

export interface DagNode {
  /** This node's rendered height, from `heightOf`. */
  readonly h: number
  readonly step: WorkflowStep
  readonly col: number
  readonly row: number
  readonly x: number
  readonly y: number
}

export interface DagEdge {
  readonly from: string
  readonly to: string
  readonly x1: number
  readonly y1: number
  readonly x2: number
  readonly y2: number
}

export interface DagLayout {
  readonly nodes: readonly DagNode[]
  readonly edges: readonly DagEdge[]
  readonly width: number
  readonly height: number
  readonly levels: readonly (readonly WorkflowStep[])[]
}

export function layoutOf(steps: readonly WorkflowStep[]): DagLayout {
  const levels = levelsOf(steps)
  if (levels.length === 0) {
    return { nodes: [], edges: [], width: PAD * 2, height: PAD * 2, levels }
  }

  // ONE HEIGHT FOR EVERY NODE, taken from the tallest content in this
  // workflow. Sizing each node to its own content is what the element used to
  // do with `minHeight`, and it is wrong twice: the layout positioned nodes as
  // if they were all NODE_H tall, so a taller one overlapped its neighbour and
  // dragged its edge endpoints off the box -- and sizing each one exactly puts
  // the steps of a CHAIN at different tops, because a step with parents is
  // taller than the step with none in front of it. A chain must read as one
  // row. So the variable part is measured, the maximum is taken once, and
  // every node gets it.
  const nodeH = Math.max(NODE_H, ...steps.map(heightOf))
  const colHeight = (n: number) => n * nodeH + Math.max(0, n - 1) * ROW_GAP
  const tallest = Math.max(...levels.map((l) => colHeight(l.length)))

  const nodes: DagNode[] = []
  levels.forEach((level, col) => {
    // Each column is centred against the tallest one, so a single joining step
    // sits opposite the middle of the fan it joins rather than at the top of
    // an empty column -- which reads as "this belongs to the first branch".
    const top = PAD + (tallest - colHeight(level.length)) / 2
    level.forEach((step, row) => {
      nodes.push({
        step,
        col,
        row,
        h: nodeH,
        x: PAD + col * (NODE_W + COL_GAP),
        y: top + row * (nodeH + ROW_GAP),
      })
    })
  })

  const byId = new Map(nodes.map((n) => [n.step.step_id, n]))
  const edges: DagEdge[] = []
  for (const child of nodes) {
    for (const parentId of child.step.depends_on) {
      const parent = byId.get(parentId)
      // A dependency naming a step that is not in this workflow is not drawn.
      // Inventing an edge to nowhere would be a claim about a graph we cannot
      // see; the node keeps the name in its own `← depends on` line.
      if (!parent) continue
      edges.push({
        from: parentId,
        to: child.step.step_id,
        x1: parent.x + NODE_W,
        y1: parent.y + parent.h / 2,
        x2: child.x,
        y2: child.y + child.h / 2,
      })
    }
  }

  return {
    nodes,
    edges,
    width: PAD * 2 + levels.length * NODE_W + (levels.length - 1) * COL_GAP,
    height: PAD * 2 + tallest,
    levels,
  }
}

/** The cubic the edge is drawn as. Horizontal control points, so every edge
 *  leaves its parent and enters its child travelling left-to-right. */
export function edgePath(e: DagEdge): string {
  const k = Math.max(24, (e.x2 - e.x1) / 2)
  return `M ${e.x1} ${e.y1} C ${e.x1 + k} ${e.y1}, ${e.x2 - k} ${e.y2}, ${e.x2} ${e.y2}`
}

// ---------------------------------------------------------------------------
// Duration -- four states, four sentences, and never a zero for an absence
// ---------------------------------------------------------------------------

/**
 * How long a step has taken, and WHAT KIND of time that is.
 *
 * FIVE ARMS BECAUSE THERE ARE FIVE ANSWERS, and collapsing them into one
 * number is the regression this whole screen is written against:
 *
 *  - `none`    -- there is no duration at all. A step with no task has not
 *                 started; a step whose task was not in the read has timings
 *                 nobody looked at. NEITHER IS `0s`. This arm has no `seconds`
 *                 field, so a renderer cannot print a number for it.
 *  - `queued`  -- it is waiting. Time spent waiting is not time spent working.
 *  - `parked`  -- it waited, stopped, and holds nothing. Also not work.
 *  - `running` -- elapsed so far. Real, and NOT a final duration.
 *  - `ran`     -- start to finish. The only arm that is a duration.
 *
 * A measured zero survives: a step that started and finished inside the same
 * second is `ran 0s`, a digit, because that was measured.
 */
export type StepDuration =
  | { readonly kind: 'none'; readonly text: string; readonly note: string }
  | { readonly kind: 'queued'; readonly seconds: number; readonly text: string; readonly note: string }
  | { readonly kind: 'parked'; readonly seconds: number; readonly text: string; readonly note: string }
  | { readonly kind: 'running'; readonly seconds: number; readonly text: string; readonly note: string }
  | { readonly kind: 'ran'; readonly seconds: number; readonly text: string; readonly note: string }

const at = (v: string | null | undefined): number => (v ? new Date(v).getTime() : NaN)
const secs = (ms: number): number => Math.max(0, Math.round(ms / 1000))

export function stepDuration(state: StepState, now: number): StepDuration {
  if (state.kind === 'unstarted') {
    return {
      kind: 'none',
      text: 'not started',
      note: 'The workflow has not reached this step, so it has no duration. That is an absence, not 0s.',
    }
  }
  if (state.kind === 'unknown') {
    return {
      kind: 'none',
      text: 'duration unread',
      note: `Task ${state.taskId} was not in the task read, so its timings were never looked at. Unknown, not zero.`,
    }
  }

  const task: Task = state.task
  const created = at(task.created_at)
  const started = at(task.started_at)
  const completed = at(task.completed_at)

  if (Number.isFinite(completed)) {
    const from = Number.isFinite(started) ? started : created
    if (!Number.isFinite(from)) {
      return {
        kind: 'none',
        text: 'duration unread',
        note: 'This step finished, but neither a start time nor a submission time was recorded, so no span can be computed.',
      }
    }
    const s = secs(completed - from)
    return {
      kind: 'ran',
      seconds: s,
      text: `ran ${formatDuration(completed - from)}`,
      note: Number.isFinite(started)
        ? 'Final duration, from the moment it started to the moment it finished.'
        : 'Measured from submission: no start time was recorded, so this span includes the wait before it ran.',
    }
  }

  // Terminal with no completion time. The row went terminal between two polls,
  // or it was cancelled before it ever ran. Either way nothing timed it, and a
  // ticking "running 4m" for a cancelled step would be a live-looking lie.
  if (TERMINAL_STATES.has(task.state)) {
    return {
      kind: 'none',
      text: 'duration unread',
      note: `This step is ${task.state.toLowerCase()} and carries no completion time, so how long it took was never recorded.`,
    }
  }

  if (task.state === 'PARKED') {
    const parked = at(task.updated_at)
    if (!Number.isFinite(parked)) {
      return {
        kind: 'none',
        text: 'parked',
        note: 'Parked: waiting, holding no capacity and doing no work. When it parked was not recorded, so there is no figure.',
      }
    }
    return {
      kind: 'parked',
      seconds: secs(now - parked),
      text: `parked ${formatDuration(now - parked)}`,
      note: 'Time spent parked. A parked step holds no capacity and is doing no work, so this is time waited, never time worked.',
    }
  }

  if (Number.isFinite(started)) {
    return {
      kind: 'running',
      seconds: secs(now - started),
      text: `running ${formatDuration(now - started)}`,
      note: 'Elapsed since it started. It has not finished, so this is not a final duration.',
    }
  }

  if (Number.isFinite(created)) {
    return {
      kind: 'queued',
      seconds: secs(now - created),
      text: `queued ${formatDuration(now - created)}`,
      note: 'Time spent waiting. Nothing has started.',
    }
  }

  return {
    kind: 'none',
    text: 'duration unread',
    note: 'No submission time was recorded for this step, so nothing about it can be timed.',
  }
}

// ---------------------------------------------------------------------------
// Spend
// ---------------------------------------------------------------------------

/**
 * What this workflow has cost so far, over the steps whose task reported one.
 *
 * `usd === null` MEANS NOTHING REPORTED A COST. It does not mean zero, and the
 * renderer must say "not reported" rather than `$0.00`. A run reported as
 * `0` -- a mock profile does exactly that -- is MEASURED and comes back as the
 * number `0`, so it renders as a digit.
 *
 * `covered` and `joined` travel with the figure so no total can be shown
 * without its coverage. Three of six steps reporting $0.42 does not make the
 * workflow's spend $0.42; it makes it "at least $0.42, from three of six".
 */
export interface WorkflowSpend {
  /** Sum over the steps that reported. Null when none did. */
  readonly usd: number | null
  /** Steps whose task reported a finite cost. */
  readonly covered: number
  /** Steps whose task was joined at all -- the most that could have reported. */
  readonly joined: number
  readonly steps: number
}

export function workflowSpend(
  steps: readonly WorkflowStep[],
  taskById: ReadonlyMap<string, Task> | null,
): WorkflowSpend {
  let usd: number | null = null
  let covered = 0
  let joined = 0

  for (const step of steps) {
    const task = step.task_id ? (taskById?.get(step.task_id) ?? null) : null
    if (!task) continue
    joined += 1
    const usage = usageOf(task)
    const raw = usage?.['total_cost_usd']
    if (typeof raw !== 'number' || !Number.isFinite(raw)) continue
    covered += 1
    usd = (usd ?? 0) + raw
  }

  return { usd, covered, joined, steps: steps.length }
}

// ---------------------------------------------------------------------------
// Runner profiles
// ---------------------------------------------------------------------------

/**
 * Which runner profiles this workflow uses, commonest first.
 *
 * On the collapsed row this answers "what is this workflow spending my
 * subscription on" without opening it -- a board can carry a twenty-step run
 * across five profiles, and which ones they are decides whether a quota park
 * is about to matter. The profile stays on every expanded node too; this is a
 * summary of those, never a replacement for them.
 */
export interface ProfileCount {
  readonly profile: string
  readonly count: number
}

export function profileMix(steps: readonly WorkflowStep[]): ProfileCount[] {
  const counts = new Map<string, number>()
  for (const s of steps) counts.set(s.runner_profile, (counts.get(s.runner_profile) ?? 0) + 1)
  return Array.from(counts, ([profile, count]) => ({ profile, count })).sort(
    (a, b) => b.count - a.count || a.profile.localeCompare(b.profile),
  )
}
