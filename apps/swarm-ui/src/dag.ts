/**
 * THE WORKFLOW GRAPH, AS GEOMETRY. No React, no DOM, no fetch.
 *
 * WHY THIS FILE EXISTS. The Workflows screen used to lay its steps out in rows
 * by dependency depth and draw ONE separator bar between one row and the next.
 * That separator is the same mark whether five independent steps join into a
 * sixth or five steps run strictly one after another, so the view could not
 * express the single question it exists to answer -- what depends on what.
 * Measured on 2026-09-22 against `wf_5e5ad3b6f7da4299a839`: five parallel steps
 * and a join rendered as five boxes, one `THEN`, one box. A chain of five
 * renders identically.
 *
 * So the unit here is the EDGE, not the level. `edgesOf` produces one edge per
 * (parent, child) pair -- five parents means five edges, and a chain of five
 * means four. Nothing collapses them and nothing shares a mark between two
 * pairs, because a shared mark is exactly how the distinction was lost.
 *
 * THREE THINGS THIS MODULE MUST NOT DO:
 *
 *  1. Drop an edge. A `depends_on` entry naming a step this workflow does not
 *     contain is a real statement about the graph and the scheduler would
 *     reject it at submission, so it is REPORTED (`dangling`), never filtered
 *     away into a graph that looks complete.
 *  2. Invent a level. Depth is 1 + max(depth of parents) and a cycle -- which
 *     the scheduler rejects, so it should be unreachable -- terminates at 0
 *     rather than recursing, because a UI that hangs on malformed data is
 *     worse than one that draws it flat.
 *  3. Describe a shape it has not measured. `summary` is generated from the
 *     in- and out-degrees actually present, so "several join into one" cannot
 *     appear over a chain.
 *
 * Geometry is kept OUT of the shape. Node boxes are measured from the rendered
 * DOM by the screen, because a node's height depends on its text -- the
 * dependency list wraps rather than ellipsing, which is the other half of the
 * same defect -- and a layout that assumed a fixed height would put every edge
 * in the wrong place the moment a step id grew. `edgePath` takes two measured
 * boxes and returns a path; it is the only part of drawing that belongs here.
 */

import type { WorkflowStep } from './types'

/** One dependency, exactly as the step declared it. */
export interface DagEdge {
  /** The `depends_on` entry: the step that must finish first. */
  from: string
  /** The step that declared it. */
  to: string
}

/** The named shapes a workflow graph can have, in the order they are tested. */
export type DagKind =
  | 'single'
  | 'parallel'
  | 'chain'
  | 'diamond'
  | 'fan-in'
  | 'fan-out'
  | 'graph'

export interface DagShape {
  /** Steps by dependency depth, ordered within a level to reduce crossings. */
  levels: WorkflowStep[][]
  /** ONE PER (parent, child) PAIR. Never merged, never deduplicated away. */
  edges: DagEdge[]
  /** Edges whose parent is not a step of this workflow. Reported, not dropped. */
  dangling: DagEdge[]
  stepCount: number
  /** How many steps sit on the busiest level. */
  widest: number
  /** Number of levels. A chain of five has five; five in parallel have one. */
  depth: number
  /** The largest number of parents any one step declares. */
  maxInDegree: number
  /** The largest number of children any one step has. */
  maxOutDegree: number
  /** The step with the most parents, when there is one with more than one. */
  joinStep: string | null
  /** The step with the most children, when there is one with more than one. */
  forkStep: string | null
  kind: DagKind
  /** A sentence a reader can tell a fan-in from a chain by, without expanding. */
  summary: string
}

/**
 * Depth of each step: 0 for a root, else 1 + max(depth of its parents).
 *
 * A parent this workflow does not contain is treated as depth -1 for the
 * purpose of placing its child -- i.e. the child stays a root -- because the
 * alternative is to move a step to a level under a node that is not drawn.
 * `dangling` carries the fact so the screen can say it out loud.
 */
function depthsOf(steps: WorkflowStep[]): Map<string, number> {
  const byId = new Map(steps.map((s) => [s.step_id, s]))
  const depth = new Map<string, number>()

  const resolve = (id: string, seen: Set<string>): number => {
    const cached = depth.get(id)
    if (cached !== undefined) return cached
    // A cycle should be impossible -- the scheduler rejects one at submission
    // (tests/unit/control_plane/test_dag_validation.py) -- but terminating is
    // still the right behaviour for data that arrived malformed anyway.
    if (seen.has(id)) return 0
    const step = byId.get(id)
    if (!step) return -1
    const known = step.depends_on.filter((p) => byId.has(p))
    if (known.length === 0) {
      depth.set(id, 0)
      return 0
    }
    seen.add(id)
    const d = 1 + Math.max(...known.map((p) => resolve(p, seen)))
    seen.delete(id)
    depth.set(id, d)
    return d
  }

  steps.forEach((s) => resolve(s.step_id, new Set()))
  return depth
}

/**
 * ONE EDGE PER (parent, child) PAIR, in declaration order.
 *
 * Exported on its own because it is the property the whole screen turns on and
 * the one a test must be able to count without rendering anything.
 */
export function edgesOf(steps: WorkflowStep[]): { edges: DagEdge[]; dangling: DagEdge[] } {
  const known = new Set(steps.map((s) => s.step_id))
  const edges: DagEdge[] = []
  const dangling: DagEdge[] = []
  for (const step of steps) {
    for (const parent of step.depends_on) {
      const edge = { from: parent, to: step.step_id }
      if (known.has(parent)) edges.push(edge)
      else dangling.push(edge)
    }
  }
  return { edges, dangling }
}

/**
 * Order the steps within a level so edges cross as little as possible.
 *
 * Barycentre: a step sits at the average position of its parents on the level
 * above. Roots and steps whose parents are all unknown keep their declared
 * order, so a workflow with no dependencies at all renders in the order it was
 * written rather than in an order this function invented.
 *
 * Ties keep declaration order -- `Array.prototype.sort` has been stable since
 * ES2019, which this bundle targets (tsconfig `target: ES2022`).
 */
function orderLevels(levels: WorkflowStep[][]): WorkflowStep[][] {
  const position = new Map<string, number>()
  const ordered: WorkflowStep[][] = []

  levels.forEach((level, index) => {
    const placed =
      index === 0
        ? level.slice()
        : level.slice().sort((a, b) => barycentre(a, position) - barycentre(b, position))
    placed.forEach((step, i) => position.set(step.step_id, i))
    ordered[index] = placed
  })

  return ordered
}

function barycentre(step: WorkflowStep, position: Map<string, number>): number {
  const known = step.depends_on.map((p) => position.get(p)).filter((v): v is number => v !== undefined)
  if (known.length === 0) return Number.MAX_SAFE_INTEGER
  return known.reduce((t, v) => t + v, 0) / known.length
}

/** The whole shape of one workflow's graph, computed once. */
export function dagShape(steps: WorkflowStep[]): DagShape {
  const depth = depthsOf(steps)
  const max = Math.max(0, ...steps.map((s) => depth.get(s.step_id) ?? 0))
  const rawLevels = Array.from({ length: steps.length === 0 ? 0 : max + 1 }, (_, lvl) =>
    steps.filter((s) => (depth.get(s.step_id) ?? 0) === lvl),
  )
  const levels = orderLevels(rawLevels)
  const { edges, dangling } = edgesOf(steps)

  const inDegree = new Map<string, number>()
  const outDegree = new Map<string, number>()
  for (const step of steps) {
    inDegree.set(step.step_id, 0)
    outDegree.set(step.step_id, 0)
  }
  for (const e of edges) {
    inDegree.set(e.to, (inDegree.get(e.to) ?? 0) + 1)
    outDegree.set(e.from, (outDegree.get(e.from) ?? 0) + 1)
  }

  const pick = (degrees: Map<string, number>): { id: string | null; n: number } => {
    let id: string | null = null
    let n = 0
    // Declaration order decides a tie, so the sentence names the same step on
    // every render rather than whichever the map happened to yield first.
    for (const step of steps) {
      const d = degrees.get(step.step_id) ?? 0
      if (d > n) {
        n = d
        id = step.step_id
      }
    }
    return { id, n }
  }

  const join = pick(inDegree)
  const fork = pick(outDegree)
  const widest = Math.max(0, ...levels.map((l) => l.length))
  const shape = {
    stepCount: steps.length,
    widest,
    depth: levels.length,
    maxInDegree: join.n,
    maxOutDegree: fork.n,
    joinStep: join.n > 1 ? join.id : null,
    forkStep: fork.n > 1 ? fork.id : null,
  }

  const kind = kindOf(shape, edges.length)
  return { levels, edges, dangling, ...shape, kind, summary: summarise(shape, kind) }
}

type Degrees = Pick<
  DagShape,
  'stepCount' | 'widest' | 'depth' | 'maxInDegree' | 'maxOutDegree' | 'joinStep' | 'forkStep'
>

function kindOf(s: Degrees, edgeCount: number): DagKind {
  if (s.stepCount <= 1) return 'single'
  if (edgeCount === 0) return 'parallel'
  if (s.maxInDegree > 1 && s.maxOutDegree > 1) return 'diamond'
  if (s.maxInDegree > 1) return 'fan-in'
  if (s.maxOutDegree > 1) return 'fan-out'
  if (s.widest === 1 && s.depth === s.stepCount) return 'chain'
  return 'graph'
}

/**
 * The collapsed mode's sentence.
 *
 * It has to carry TOPOLOGY, not a count: "6 steps" is exactly what the old
 * header said, and it is true of a chain of six and of five-joining-into-one
 * alike. So every branch below names the degree that makes the shape what it
 * is, and the two names involved.
 */
function summarise(s: Degrees, kind: DagKind): string {
  const steps = `${s.stepCount} step${s.stepCount === 1 ? '' : 's'}`
  switch (kind) {
    case 'single':
      return s.stepCount === 0 ? 'no steps' : '1 step, with nothing before it'
    case 'parallel':
      return `${steps}, all independent — nothing waits for anything`
    case 'chain':
      return `${steps} in a chain, one after another`
    case 'fan-out':
      return `${steps} — ${s.forkStep} splits into ${s.maxOutDegree}`
    case 'fan-in':
      return `${steps} — ${s.maxInDegree} join into ${s.joinStep}`
    case 'diamond':
      return `${steps} — ${s.forkStep} splits into ${s.maxOutDegree}, ${s.maxInDegree} join into ${s.joinStep}`
    case 'graph':
      return `${steps} over ${s.depth} level${s.depth === 1 ? '' : 's'}, widest ${s.widest}`
  }
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------

/** A node's position in the graph host's coordinate space, as measured. */
export interface Box {
  x: number
  y: number
  w: number
  h: number
}

/**
 * The `d` of one edge: parent's bottom centre to child's top centre.
 *
 * A cubic with vertical control handles, so a long diagonal to an outer parent
 * still leaves the node face vertically and arrives vertically -- which is what
 * makes five edges into one join legible as five rather than as a fan of
 * straight lines converging on a point.
 */
export function edgePath(from: Box, to: Box): string {
  const x1 = from.x + from.w / 2
  const y1 = from.y + from.h
  const x2 = to.x + to.w / 2
  const y2 = to.y
  // Half the vertical gap, floored so a short gap still bends rather than
  // rendering as a kinked straight line.
  const bend = Math.max(14, (y2 - y1) / 2)
  return `M ${round(x1)} ${round(y1)} C ${round(x1)} ${round(y1 + bend)}, ${round(x2)} ${round(y2 - bend)}, ${round(x2)} ${round(y2)}`
}

function round(v: number): number {
  return Math.round(v * 10) / 10
}

/**
 * The collapsed mode's sparkline: one dot per step, one line per edge, in a
 * fixed viewBox.
 *
 * Needs no measurement, so it is pure and a card can draw one per workflow
 * while scanning a board of them. It is the same edge list the full canvas
 * uses -- a fan-in of five draws five converging lines here too, at 8px tall --
 * so the collapsed view cannot disagree with the expanded one about the shape.
 */
export interface MiniMap {
  width: number
  height: number
  dots: { id: string; cx: number; cy: number }[]
  lines: { from: string; to: string; x1: number; y1: number; x2: number; y2: number }[]
}

export function miniMap(shape: DagShape, width: number, height: number): MiniMap {
  const dots: MiniMap['dots'] = []
  const at = new Map<string, { cx: number; cy: number }>()
  const rows = Math.max(1, shape.levels.length)
  // 3px of padding at each end keeps a 2.2px dot inside the box at both
  // extremes; without it the first and last levels are clipped in half.
  const pad = 3
  shape.levels.forEach((level, row) => {
    const cy = rows === 1 ? height / 2 : pad + (row * (height - 2 * pad)) / (rows - 1)
    level.forEach((step, i) => {
      const cols = level.length
      const cx = cols === 1 ? width / 2 : pad + (i * (width - 2 * pad)) / (cols - 1)
      const dot = { id: step.step_id, cx: round(cx), cy: round(cy) }
      dots.push(dot)
      at.set(step.step_id, dot)
    })
  })

  const lines: MiniMap['lines'] = []
  for (const e of shape.edges) {
    const a = at.get(e.from)
    const b = at.get(e.to)
    if (!a || !b) continue
    lines.push({ from: e.from, to: e.to, x1: a.cx, y1: a.cy, x2: b.cx, y2: b.cy })
  }

  return { width, height, dots, lines }
}
