// OPTION D, PROTOTYPED: the one layout step a layered-graph engine (dagre,
// ELK, vis-network's hierarchical mode, Graphify's d3 tree) does and dag.ts
// does not -- ORDERING EACH LEVEL TO REDUCE EDGE CROSSINGS.
//
// dag.ts `layoutOf` places a level's steps in the order the workflow LISTS
// them, with one exception (`oneToOneParents`: every step of a level has its
// own distinct parent). A composer adds steps round by round, so a fan-out of
// four specs into twelve implementers draws twelve edges braided across each
// other. This file is the Sugiyama ordering pass, alone: it returns the SAME
// steps, reordered within their levels, so today's renderer can draw them
// unchanged. It moves no step to another level, adds none and drops none
// (proto.test.tsx holds each of those).
//
// NOT IMPORTED BY THE APP (see fixtures.ts).

import { levelsOf, layoutOf } from '../dag'
import type { WorkflowStep } from '../types'

type Levels = readonly (readonly WorkflowStep[])[]

/** Crossings between ADJACENT levels when each level is drawn in the given order (positions by index). */
export function orderCrossings(levels: Levels): number {
  const pos = new Map<string, number>()
  levels.forEach((lv) => lv.forEach((s, i) => pos.set(s.step_id, i)))
  let total = 0
  for (let l = 1; l < levels.length; l++) {
    const above = new Set(levels[l - 1]!.map((s) => s.step_id))
    const pairs: [number, number][] = []
    for (const s of levels[l]!) for (const d of s.depends_on) if (above.has(d)) pairs.push([pos.get(d)!, pos.get(s.step_id)!])
    for (let i = 0; i < pairs.length; i++)
      for (let j = i + 1; j < pairs.length; j++) {
        const [a1, b1] = pairs[i]!
        const [a2, b2] = pairs[j]!
        if ((a1 - a2) * (b1 - b2) < 0) total++
      }
  }
  return total
}

/**
 * Crossings in what today's renderer ACTUALLY DRAWS: `layoutOf` with every
 * stage open, counting each pair of edges between the same two adjacent levels
 * whose ends swap sides. Skip-level edges run on their own lanes and are not
 * counted; edges into one step meet at its join and cannot cross each other.
 */
export function drawnCrossings(steps: readonly WorkflowStep[]): number {
  const levels = levelsOf(steps)
  const all = new Set(levels.map((_, i) => i))
  const layout = layoutOf(steps, all, 'names')
  const levelOf = new Map(layout.nodes.map((n) => [n.step.step_id, n.level]))
  const adjacent = layout.edges.filter((e) => e.lane === null && levelOf.get(e.to)! - levelOf.get(e.from)! === 1)
  let total = 0
  for (let i = 0; i < adjacent.length; i++)
    for (let j = i + 1; j < adjacent.length; j++) {
      const a = adjacent[i]!
      const b = adjacent[j]!
      if (levelOf.get(a.from) !== levelOf.get(b.from)) continue
      if ((a.x1 - b.x1) * (a.x2 - b.x2) < 0) total++
    }
  return total
}

/** Where a step sits in its level, as a fraction 0..1, so parents on levels of different widths compare. */
function spread(levels: Levels): Map<string, number> {
  const at = new Map<string, number>()
  levels.forEach((lv) => lv.forEach((s, i) => at.set(s.step_id, lv.length === 1 ? 0.5 : i / (lv.length - 1))))
  return at
}

function mean(xs: number[]): number | null {
  return xs.length === 0 ? null : xs.reduce((a, b) => a + b, 0) / xs.length
}

/** Sort one level by the barycentre of its neighbours; a step with none keeps its place (stable). */
function sortBy(level: readonly WorkflowStep[], key: (s: WorkflowStep) => number | null): WorkflowStep[] {
  const keyed = level.map((s, i) => ({ s, i, k: key(s) ?? (level.length === 1 ? 0.5 : i / (level.length - 1)) }))
  keyed.sort((a, b) => a.k - b.k || a.i - b.i)
  return keyed.map((x) => x.s)
}

/**
 * The barycentric ordering: sweep down (each level by its parents' mean
 * position), then up (by its children's), four times, keeping the best order
 * seen. Deterministic and O(sweeps × edges); at the server's 50-step cap it is
 * well under a millisecond.
 */
export function barycentricOrder(steps: readonly WorkflowStep[], sweeps = 4): WorkflowStep[] {
  let levels: WorkflowStep[][] = levelsOf(steps).map((lv) => [...lv])
  const children = new Map<string, string[]>()
  for (const s of steps) for (const d of s.depends_on) children.set(d, [...(children.get(d) ?? []), s.step_id])
  let best = levels.map((lv) => [...lv])
  let bestCost = orderCrossings(best)
  for (let it = 0; it < sweeps && bestCost > 0; it++) {
    for (let l = 1; l < levels.length; l++) {
      const at = spread(levels)
      levels[l] = sortBy(levels[l]!, (s) => mean(s.depends_on.flatMap((d) => (at.has(d) ? [at.get(d)!] : []))))
    }
    for (let l = levels.length - 2; l >= 0; l--) {
      const at = spread(levels)
      levels[l] = sortBy(levels[l]!, (s) => mean((children.get(s.step_id) ?? []).flatMap((c) => (at.has(c) ? [at.get(c)!] : []))))
    }
    const cost = orderCrossings(levels)
    if (cost < bestCost) {
      best = levels.map((lv) => [...lv])
      bestCost = cost
    }
  }
  levels = best
  return levels.flat()
}
